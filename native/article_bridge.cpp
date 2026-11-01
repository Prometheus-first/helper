// Read-only subscription requests through the version-locked, logged-in host.
// No credentials, reports, raw replies or HTML are written to disk.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <bcrypt.h>
#include <cstdint>
#include <cstring>
#include <algorithm>
#include <string>
#include <vector>
#include <set>
#include <limits>
#include "vendor/minhook-1.3.4/include/MinHook.h"

namespace {
constexpr size_t MAX_RESPONSE = 2 * 1024 * 1024;
constexpr size_t MAX_PARAMS = 131072;
constexpr char FEED_URL[] = "weixin://resourceid/SubscriptionDisorder/disorder.html";
constexpr char HISTORY_URL[] = "/cgi-bin/mmbiz-bin/bizattr/bizprofilev2h5";
struct SmallString { char data[16]; uint64_t length, capacity; };
struct Config {
    uint32_t size, pid, operation, timeout_ms;
    uint32_t frame, stage, status, response_size;
    uint64_t request_id, response_address;
    uint32_t response_capacity, offset_present;
    uint64_t login_root, login_context;
    char account[128], publisher[128], nonce[33], offset[8193], url[4097];
};
static_assert(sizeof(SmallString) == 32, "Unexpected client string ABI");
static_assert(sizeof(Config) == 12656, "Unexpected article config ABI");
using H5 = void (__fastcall*)(void*, void*, uint32_t, const SmallString*, void*, void*);
using Completion = void (__fastcall*)(void*, void*);
using Execute = void (__fastcall*)(void*, uint32_t, const char*);
H5 original_h5 = nullptr;
Completion original_completion = nullptr;
Execute original_execute = nullptr;
uintptr_t core = 0, host = 0;
Config request{};
Config* output = nullptr;
void* awaited_callback = nullptr;
SRWLOCK result_lock = SRWLOCK_INIT;
volatile LONG admission = 0, stopping = 1, active = 0;
bool hook_h5 = false, hook_completion = false, hook_execute = false, initialized = false;
ULONGLONG deadline = 0;

bool fingerprint(HMODULE module, uint64_t expected_size, const unsigned char* expected) {
    wchar_t path[32768]{};
    if (!module || !GetModuleFileNameW(module, path, static_cast<DWORD>(std::size(path)))) return false;
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE,
                             nullptr, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
    if (file == INVALID_HANDLE_VALUE) return false;
    LARGE_INTEGER size{};
    bool valid = GetFileSizeEx(file, &size) && static_cast<uint64_t>(size.QuadPart) == expected_size;
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    if (valid && BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) >= 0) {
        if (BCryptCreateHash(algorithm, &hash, nullptr, 0, nullptr, 0, 0) >= 0) {
            unsigned char buffer[65536]{}, digest[32]{};
            DWORD amount = 0;
            while (valid) {
                valid = ReadFile(file, buffer, sizeof(buffer), &amount, nullptr) != FALSE;
                if (!valid || !amount) break;
                valid = BCryptHashData(hash, buffer, amount, 0) >= 0;
            }
            valid = valid && BCryptFinishHash(hash, digest, sizeof(digest), 0) >= 0 &&
                std::equal(digest, digest + 32, expected);
            SecureZeroMemory(buffer, sizeof(buffer));
            BCryptDestroyHash(hash);
        } else valid = false;
        BCryptCloseAlgorithmProvider(algorithm, 0);
    } else valid = false;
    CloseHandle(file);
    return valid;
}

bool account_matches() {
    __try {
        const auto root = *reinterpret_cast<const uintptr_t*>(core + 0xbd0ce30);
        if (!root || root != request.login_root || *reinterpret_cast<const uintptr_t*>(root) != core + 0x9054b98)
            return false;
        const auto context = *reinterpret_cast<const uintptr_t*>(root + 0x68);
        if (!context || context != request.login_context || *reinterpret_cast<const uintptr_t*>(context) != core + 0x90d4f88 ||
            !(*reinterpret_cast<const unsigned char*>(context + 0x38) & 1)) return false;
        const auto identity = reinterpret_cast<const SmallString*>(context + 0x48);
        const size_t length = strnlen_s(request.account, sizeof(request.account));
        if (!length || length >= sizeof(request.account) || identity->length != length ||
                identity->capacity < length || identity->capacity > 1048576) return false;
        const char* text = identity->capacity == 15 ? identity->data : *reinterpret_cast<const char* const*>(identity->data);
        return text && !std::memcmp(text, request.account, length) &&
            *reinterpret_cast<const uintptr_t*>(core + 0xbd0ce30) == root &&
            *reinterpret_cast<const uintptr_t*>(root + 0x68) == context;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

bool copy_small(const SmallString* source, char* destination, size_t capacity, size_t* length) {
    __try {
        if (!source || source->length >= capacity || source->capacity < source->length || source->capacity > 16777216 ||
            !(source->capacity == 15 || source->capacity >= 16)) return false;
        const char* data = source->capacity == 15 ? source->data : *reinterpret_cast<const char* const*>(source->data);
        if (!data) return false;
        *length = static_cast<size_t>(source->length);
        std::memcpy(destination, data, *length); destination[*length] = 0;
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

struct Tree { uintptr_t begin, root; uint64_t count; };
struct Frame { uintptr_t manager, creation, handler; uint32_t id; Tree entries, handlers; };
// This reads only the exact builtin URI prefix, never its potentially private query.
bool feed_string(uintptr_t node) {
    __try {
        unsigned char header[24]{};
        std::memcpy(header, reinterpret_cast<const void*>(node + 0xe8), sizeof(header));
        const bool heap = (header[23] & 0x80) != 0;
        const auto length = heap ? *reinterpret_cast<const uint64_t*>(header + 8) : header[23];
        const auto capacity = heap ? *reinterpret_cast<const uint64_t*>(header + 16) & 0x7fffffffffffffffull : 22ull;
        const char* data = heap ? *reinterpret_cast<const char* const*>(header) : reinterpret_cast<const char*>(node + 0xe8);
        const size_t prefix = sizeof(FEED_URL) - 1;
        if (!data || length < prefix || length > 4096 || capacity < length || std::memcmp(data, FEED_URL, prefix)) return false;
        if (length > prefix && data[prefix] != '?') return false;
        return !std::memcmp(header, reinterpret_cast<const void*>(node + 0xe8), sizeof(header));
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}
bool tree_snapshot(uintptr_t manager, Tree* entries, Tree* handlers) {
    __try {
        std::memcpy(entries, reinterpret_cast<const void*>(manager + 0xa0), sizeof(Tree));
        std::memcpy(handlers, reinterpret_cast<const void*>(manager + 0x40), sizeof(Tree));
        return entries->count && entries->count <= 128 && handlers->count && handlers->count <= 128;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}
bool frame_impl(Frame* frame) {
    const auto manager = *reinterpret_cast<const uintptr_t*>(host + 0x5d3720);
    if (!manager || *reinterpret_cast<const uintptr_t*>(manager) != host + 0x4e71e0 ||
        *reinterpret_cast<const uintptr_t*>(host + 0x4e71e0 + 0x58) != host + 0x1cb6b0) return false;
    Tree entries{}, handlers{};
    if (!tree_snapshot(manager, &entries, &handlers)) return false;
    struct Branch { uintptr_t node, parent; uint64_t low, high; };
    std::vector<Branch> todo{{entries.root, 0, 0, 0x100000000ull}}; std::set<uintptr_t> visited;
    uintptr_t chosen = 0; uint32_t identifier = 0;
    while (!todo.empty()) {
        const auto item = todo.back(); todo.pop_back(); const auto node = item.node;
        if (!node) continue;
        if (visited.size() >= entries.count || !visited.insert(node).second) return false;
        const auto left = *reinterpret_cast<const uintptr_t*>(node), right = *reinterpret_cast<const uintptr_t*>(node + 8);
        const auto key = *reinterpret_cast<const uint32_t*>(node + 0x20);
        if ((item.parent && *reinterpret_cast<const uintptr_t*>(node + 0x10) != item.parent) || key <= item.low || key >= item.high) return false;
        if (left) todo.push_back({left, node, item.low, key}); if (right) todo.push_back({right, node, key, item.high});
        if (*reinterpret_cast<const uintptr_t*>(node + 0x28) != host + 0x4ea3e0 || !feed_string(node)) continue;
        if (!key || key > INT32_MAX || chosen) return false;
        chosen = node; identifier = key;
    }
    if (!chosen || visited.size() != entries.count) return false;
    unsigned char creation_header[48]{}, url_header[24]{};
    std::memcpy(creation_header, reinterpret_cast<const void*>(chosen), sizeof(creation_header));
    std::memcpy(url_header, reinterpret_cast<const void*>(chosen + 0xe8), sizeof(url_header));
    todo = {{handlers.root, 0, 0, 0x100000000ull}}; visited.clear(); uintptr_t handler = 0;
    while (!todo.empty()) {
        const auto item = todo.back(); todo.pop_back(); const auto node = item.node;
        if (!node) continue;
        if (visited.size() >= handlers.count || !visited.insert(node).second) return false;
        const auto left = *reinterpret_cast<const uintptr_t*>(node), right = *reinterpret_cast<const uintptr_t*>(node + 8);
        const auto key = *reinterpret_cast<const uint32_t*>(node + 0x20);
        if ((item.parent && *reinterpret_cast<const uintptr_t*>(node + 0x10) != item.parent) || key <= item.low || key >= item.high) return false;
        if (left) todo.push_back({left, node, item.low, key}); if (right) todo.push_back({right, node, key, item.high});
        if (key == identifier) {
            if (handler || !*reinterpret_cast<const uintptr_t*>(node + 0x28)) return false;
            handler = node;
        }
    }
    unsigned char handler_header[48]{};
    if (handler) std::memcpy(handler_header, reinterpret_cast<const void*>(handler), sizeof(handler_header));
    Tree after_entries{}, after_handlers{};
    if (!handler || visited.size() != handlers.count || !tree_snapshot(manager, &after_entries, &after_handlers) ||
        std::memcmp(&entries, &after_entries, sizeof(Tree)) || std::memcmp(&handlers, &after_handlers, sizeof(Tree)) ||
        *reinterpret_cast<const uintptr_t*>(host + 0x5d3720) != manager ||
        *reinterpret_cast<const uintptr_t*>(manager) != host + 0x4e71e0 ||
        *reinterpret_cast<const uintptr_t*>(host + 0x4e71e0 + 0x58) != host + 0x1cb6b0 || !feed_string(chosen) ||
        std::memcmp(creation_header, reinterpret_cast<const void*>(chosen), sizeof(creation_header)) ||
        std::memcmp(url_header, reinterpret_cast<const void*>(chosen + 0xe8), sizeof(url_header)) ||
        std::memcmp(handler_header, reinterpret_cast<const void*>(handler), sizeof(handler_header))) return false;
    *frame = {manager, chosen, handler, identifier, entries, handlers};
    return true;
}
bool find_frame(Frame* frame) {
    __try { return frame_impl(frame); }
    __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

// A bounded JSON reader is used only for matching our own request, not interpreting server data.
class Json {
    const std::string& text;
    size_t position = 0;
    void whitespace() { while (position < text.size() && (text[position] == ' ' || text[position] == '\r' || text[position] == '\n' || text[position] == '\t')) ++position; }
    bool string(std::string* result) {
        if (position >= text.size() || text[position++] != '"') return false;
        result->clear();
        while (position < text.size()) {
            unsigned char ch = text[position++];
            if (ch == '"') return true;
            if (ch < 32) return false;
            if (ch != '\\') { result->push_back(static_cast<char>(ch)); continue; }
            if (position == text.size()) return false;
            ch = text[position++];
            if (ch == '"' || ch == '\\' || ch == '/') result->push_back(static_cast<char>(ch));
            else if (ch == 'b') result->push_back('\b'); else if (ch == 'f') result->push_back('\f');
            else if (ch == 'n') result->push_back('\n'); else if (ch == 'r') result->push_back('\r'); else if (ch == 't') result->push_back('\t');
            else if (ch == 'u') {
                if (position + 4 > text.size()) return false;
                unsigned value = 0;
                for (unsigned i = 0; i < 4; ++i) {
                    const char digit = text[position++];
                    unsigned decoded = digit >= '0' && digit <= '9' ? digit - '0' : digit >= 'a' && digit <= 'f' ? digit - 'a' + 10 : digit >= 'A' && digit <= 'F' ? digit - 'A' + 10 : 99;
                    if (decoded > 15) return false; value = value * 16 + decoded;
                }
                if (value > 127) return false; result->push_back(static_cast<char>(value));
            } else return false;
        }
        return false;
    }
    bool skip(unsigned depth) {
        whitespace(); if (position >= text.size() || depth > 16) return false;
        if (text[position] == '"') { std::string unused; return string(&unused); }
        if (text[position] == '{' || text[position] == '[') {
            const char ending = text[position++] == '{' ? '}' : ']';
            bool quoted = false, escaped = false; unsigned nested = 1;
            while (position < text.size()) {
                const char ch = text[position++];
                if (quoted) { if (escaped) escaped = false; else if (ch == '\\') escaped = true; else if (ch == '"') quoted = false; continue; }
                if (ch == '"') quoted = true;
                else if (ch == '{' || ch == '[') { if (++nested > 16) return false; }
                else if (ch == '}' || ch == ']') { if (!--nested) return ch == ending; }
            }
            return false;
        }
        const size_t start = position;
        while (position < text.size() && text[position] != ',' && text[position] != '}' && text[position] != ']' && text[position] != ' ' && text[position] != '\r' && text[position] != '\n' && text[position] != '\t') ++position;
        return position > start;
    }
public:
    explicit Json(const std::string& input): text(input) {}
    bool member(const char* wanted, std::string* value, bool* quoted) {
        whitespace(); if (position >= text.size() || text[position++] != '{') return false;
        std::set<std::string> keys; bool found = false;
        while (true) {
            whitespace(); if (position < text.size() && text[position] == '}') { ++position; break; }
            std::string key; if (!string(&key) || key.size() > 256 || !keys.insert(key).second || keys.size() > 128) return false;
            whitespace(); if (position >= text.size() || text[position++] != ':') return false; whitespace();
            if (key == wanted) {
                *quoted = position < text.size() && text[position] == '"';
                if (*quoted) { if (!string(value)) return false; }
                else { const size_t start = position; if (!skip(0)) return false; *value = text.substr(start, position - start); }
                found = true;
            } else if (!skip(0)) return false;
            whitespace(); if (position >= text.size()) return false;
            if (text[position] == '}') { ++position; break; }
            if (text[position++] != ',') return false;
        }
        whitespace(); return found && position == text.size();
    }
};
bool member(const std::string& source, const char* key, const std::string& expected, bool quoted = true) {
    std::string value; bool is_string = false;
    return Json(source).member(key, &value, &is_string) && is_string == quoted && value == expected;
}
std::string quote(const char* data) {
    std::string result = "\"";
    for (const unsigned char* ch = reinterpret_cast<const unsigned char*>(data); *ch; ++ch) {
        if (*ch == '"' || *ch == '\\') { result.push_back('\\'); result.push_back(static_cast<char>(*ch)); }
        else if (*ch < 32) {
            const char hex[] = "0123456789abcdef"; result += "\\u00"; result.push_back(hex[*ch >> 4]); result.push_back(hex[*ch & 15]);
        } else result.push_back(static_cast<char>(*ch));
    }
    return result + '"';
}
bool running() {
    return !InterlockedCompareExchange(&stopping, 0, 0) && output && GetTickCount64() < deadline && account_matches();
}
void finish(const char* data, size_t length, DWORD error = 0) {
    AcquireSRWLockExclusive(&result_lock);
    __try {
        if (running() && output->stage < 4) {
            if (length >= request.response_capacity || length > MAX_RESPONSE) error = ERROR_INSUFFICIENT_BUFFER;
            if (!error) {
                std::memcpy(reinterpret_cast<void*>(request.response_address), data, length);
                *reinterpret_cast<char*>(request.response_address + length) = 0;
                output->response_size = static_cast<uint32_t>(length);
            }
            output->status = error;
            MemoryBarrier(); InterlockedExchange(reinterpret_cast<volatile LONG*>(&output->stage), 4);
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) { }
    ReleaseSRWLockExclusive(&result_lock);
}
void observe_h5(uint32_t frame, void* packet, void* callback) {
    if (request.operation != 1 || frame != request.frame || !running()) return;
    std::vector<char> storage(MAX_PARAMS + 1); size_t length = 0;
    if (!copy_small(reinterpret_cast<const SmallString*>(static_cast<char*>(packet) + 0x60), storage.data(), storage.size(), &length)) return;
    const std::string params(storage.data(), length);
    if (!member(params, "url", HISTORY_URL) || !member(params, "scope", "subscriptions") || !member(params, "cgi_cmdid", "5814", false)) return;
    std::string payload; bool quoted = false;
    if (!Json(params).member("req_json", &payload, &quoted) || !quoted ||
            !member(payload, "BizUserName", request.publisher) || !member(payload, "BizSessionID", std::to_string(request.request_id), false) ||
            !member(payload, "ActionType", "1", false) || !member(payload, "PageSize", "10", false)) return;
    std::string offset; bool offset_quoted = false;
    const bool has_offset = Json(payload).member("Offset", &offset, &offset_quoted);
    if ((request.offset_present && (!has_offset || !offset_quoted || offset != request.offset)) || (!request.offset_present && has_offset)) return;
    AcquireSRWLockExclusive(&result_lock);
    if (running() && output->stage < 4) {
        awaited_callback = callback;
        InterlockedExchange(reinterpret_cast<volatile LONG*>(&output->stage), 3);
    }
    ReleaseSRWLockExclusive(&result_lock);
    SecureZeroMemory(storage.data(), storage.size());
}
void guarded_observe_h5(uint32_t frame, void* packet, void* callback) {
    __try { observe_h5(frame, packet, callback); } __except (EXCEPTION_EXECUTE_HANDLER) { }
}
void safe_observe_h5(uint32_t frame, void* packet, void* callback) {
    try { guarded_observe_h5(frame, packet, callback); } catch (...) { }
}
void __fastcall h5_hook(void* a, void* b, uint32_t frame, const SmallString* origin, void* packet, void* callback) {
    InterlockedIncrement(&active);
    __try {
        safe_observe_h5(frame, packet, callback);
        original_h5(a, b, frame, origin, packet, callback);
    } __finally { InterlockedDecrement(&active); }
}
bool dump_json(void* value, SmallString* out) {
    __try {
        using Dump = void (__fastcall*)(SmallString*, void*);
        reinterpret_cast<Dump>(core + 0x663100)(out, value); return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}
void destroy_string(SmallString* out) {
    __try {
        using Destroy = void (__fastcall*)(SmallString*);
        reinterpret_cast<Destroy>(core + 0x2010)(out);
    } __except (EXCEPTION_EXECUTE_HANDLER) { }
}
void observe_completion(void* holder, void* value) {
    if (request.operation != 1 || !running()) return;
    AcquireSRWLockExclusive(&result_lock);
    const bool wanted = awaited_callback && awaited_callback == holder;
    if (wanted) awaited_callback = nullptr;
    ReleaseSRWLockExclusive(&result_lock);
    if (!wanted) return;
    SmallString out{};
    if (!dump_json(value, &out)) { finish(nullptr, 0, ERROR_INVALID_DATA); return; }
    std::vector<char> serialized(MAX_RESPONSE + 1); size_t length = 0;
    const bool valid = copy_small(&out, serialized.data(), serialized.size(), &length);
    destroy_string(&out);
    if (valid) finish(serialized.data(), length); else finish(nullptr, 0, ERROR_INSUFFICIENT_BUFFER);
    SecureZeroMemory(serialized.data(), serialized.size());
}
void guarded_observe_completion(void* holder, void* value) {
    __try { observe_completion(holder, value); } __except (EXCEPTION_EXECUTE_HANDLER) { }
}
void safe_observe_completion(void* holder, void* value) {
    try { guarded_observe_completion(holder, value); } catch (...) { finish(nullptr, 0, ERROR_INVALID_DATA); }
}
void __fastcall completion_hook(void* holder, void* value) {
    InterlockedIncrement(&active);
    __try {
        safe_observe_completion(holder, value);
        original_completion(holder, value);
    } __finally { InterlockedDecrement(&active); }
}
size_t bounded_cstring(const char* text, size_t limit) {
    __try { return strnlen_s(text, limit); } __except (EXCEPTION_EXECUTE_HANDLER) { return limit; }
}
void observe_execute(void* manager, uint32_t frame, const char* text) {
    if (request.operation != 2 || frame != request.frame || !running() ||
        reinterpret_cast<uintptr_t>(manager) != *reinterpret_cast<const uintptr_t*>(host + 0x5d3720)) return;
    const size_t length = bounded_cstring(text, MAX_RESPONSE + 1);
    if (length > MAX_RESPONSE) return;
    const std::string script(text, length), nonce = std::string("helper_") + request.nonce;
    if (script.find("_handleMessageFromWeixin") == std::string::npos || script.find(nonce) == std::string::npos) return;
    finish(script.data(), script.size());
}
void guarded_observe_execute(void* manager, uint32_t frame, const char* text) {
    __try { observe_execute(manager, frame, text); } __except (EXCEPTION_EXECUTE_HANDLER) { }
}
void safe_observe_execute(void* manager, uint32_t frame, const char* text) {
    try { guarded_observe_execute(manager, frame, text); } catch (...) { }
}
void __fastcall execute_hook(void* manager, uint32_t frame, const char* text) {
    InterlockedIncrement(&active);
    __try {
        safe_observe_execute(manager, frame, text);
        original_execute(manager, frame, text);
    } __finally { InterlockedDecrement(&active); }
}
bool valid_ascii(const char* text, size_t capacity, bool publisher = false) {
    const size_t length = strnlen_s(text, capacity);
    if (!length || length >= capacity || (publisher && std::strncmp(text, "gh_", 3))) return false;
    for (size_t i = 0; i < length; ++i) {
        const unsigned char c = static_cast<unsigned char>(text[i]);
        if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '-' || c == '.')) return false;
    }
    return true;
}
bool canonical_url(const char* url) {
    constexpr char prefix[] = "https://mp.weixin.qq.com/s?";
    const size_t length = strnlen_s(url, sizeof(request.url));
    if (length < sizeof(prefix) || length >= sizeof(request.url) || std::strncmp(url, prefix, sizeof(prefix)-1)) return false;
    std::string query(url + sizeof(prefix)-1); std::set<std::string> keys;
    size_t start = 0;
    while (start < query.size()) {
        const auto end = query.find('&', start), stop = end == std::string::npos ? query.size() : end;
        const auto equal = query.find('=', start);
        if (equal == std::string::npos || equal >= stop || equal + 1 == stop) return false;
        const auto key = query.substr(start, equal-start), value = query.substr(equal+1, stop-equal-1);
        if (!keys.insert(key).second || (key != "__biz" && key != "mid" && key != "idx" && key != "sn")) return false;
        for (char ch: value) {
            const bool digit = ch >= '0' && ch <= '9', hex = digit || (ch >= 'a' && ch <= 'f');
            if ((key == "mid" || key == "idx") && !digit) return false;
            if (key == "sn" && !hex) return false;
            if (key == "__biz" && !(digit || (ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') || ch == '%' || ch == '+' || ch == '/' || ch == '=' || ch == '_' || ch == '-')) return false;
        }
        if (key == "sn" && value.size() != 32) return false;
        start = stop + 1;
    }
    return keys.size() == 4;
}
std::string javascript() {
    const std::string guard = "(()=>{if(location.protocol!=='weixin:'||location.host!=='resourceid'||location.pathname!=='/SubscriptionDisorder/disorder.html')return;";
    if (request.operation == 1) {
        std::string payload = "{\"BizUserName\":" + quote(request.publisher) + ",\"ActionType\":1,\"PageSize\":10,\"BizSessionID\":" + std::to_string(request.request_id) + ",\"Scene\":207,\"UsePlainTopic\":true,\"PreLoad\":0";
        if (request.offset_present) payload += ",\"Offset\":" + quote(request.offset);
        payload += '}';
        const auto params = "{\"cgi_cmdid\":5814,\"url\":" + quote(HISTORY_URL) + ",\"scope\":\"subscriptions\",\"req_json\":" + quote(payload.c_str()) + '}';
        return guard + "if(typeof WeixinJSBridge==='object')WeixinJSBridge.invoke('H5ExtTransfer'," + params + ",()=>{});})();";
    }
    const auto nonce = std::string("helper_") + request.nonce;
    const auto packet = "{\"func\":\"openUrlWithExtraWebview\",\"params\":{\"url\":" + quote(request.url) + ",\"type\":\"Web\",\"use_system_browser\":false},\"sessionid\":" + quote(nonce.c_str()) + ",\"__msg_type\":\"call\",\"__callback_id\":" + quote(nonce.c_str()) + '}';
    return guard + "if(typeof MM_CallNative==='function')MM_CallNative('SCENE_FETCHQUEUE'," + quote(packet.c_str()) + ");})();";
}
DWORD start_impl(Config* config) {
    if (!config || config->size != sizeof(Config) || config->pid != GetCurrentProcessId()) return ERROR_INVALID_PARAMETER;
    request = *config;
    if ((request.operation != 1 && request.operation != 2) || request.timeout_ms < 100 || request.timeout_ms > 15000 ||
        request.response_capacity != MAX_RESPONSE+1 || !request.response_address || !request.request_id || request.request_id > 0xffffffffull ||
        !valid_ascii(request.account, sizeof(request.account)) || !valid_ascii(request.publisher, sizeof(request.publisher), true) ||
        strnlen_s(request.nonce, sizeof(request.nonce)) != 32 || strnlen_s(request.offset, sizeof(request.offset)) >= sizeof(request.offset) || request.offset_present > 1 ||
        (request.operation == 2 && !canonical_url(request.url))) return ERROR_INVALID_PARAMETER;
    for (char c: std::string(request.nonce)) if (!(c >= '0' && c <= '9') && !(c >= 'a' && c <= 'f')) return ERROR_INVALID_PARAMETER;
    const auto core_module = GetModuleHandleW(L"Weixin.dll"), host_module = GetModuleHandleW(L"wmpf_host_export_x64.dll");
    constexpr unsigned char core_hash[32] = {0x74,0x5c,0xdf,0x29,0x55,0xfa,0x51,0x28,0xcd,0xb0,0x0c,0x05,0x6c,0x97,0x57,0x11,0x56,0xa5,0xf1,0x07,0x40,0x6a,0x9b,0xb1,0x5a,0xc0,0x75,0x4f,0x32,0x46,0xb2,0x01};
    constexpr unsigned char host_hash[32] = {0x64,0x44,0xb9,0x87,0x61,0xa5,0xe5,0x32,0xa9,0xfa,0x5b,0x62,0x82,0x43,0xff,0x4a,0x80,0xd7,0x84,0xfe,0x76,0x15,0x00,0x4a,0xf2,0x59,0x40,0x5c,0x39,0x41,0x20,0x9b};
    if (!fingerprint(core_module, 205929008, core_hash) || !fingerprint(host_module, 6189608, host_hash)) return ERROR_REVISION_MISMATCH;
    core = reinterpret_cast<uintptr_t>(core_module); host = reinterpret_cast<uintptr_t>(host_module);
    if (!account_matches()) return ERROR_NOT_READY;
    Frame frame{}; if (!find_frame(&frame) || frame.id != request.frame) return ERROR_NOT_READY;
    const auto mh = MH_Initialize(); if (mh != MH_OK) return ERROR_BUSY; initialized = true;
    if (request.operation == 1) {
        if (MH_CreateHook(reinterpret_cast<void*>(core+0x4674510), reinterpret_cast<void*>(&h5_hook), reinterpret_cast<void**>(&original_h5)) != MH_OK) return ERROR_INVALID_FUNCTION;
        hook_h5 = true;
        if (MH_CreateHook(reinterpret_cast<void*>(core+0x1f647c0), reinterpret_cast<void*>(&completion_hook), reinterpret_cast<void**>(&original_completion)) != MH_OK) return ERROR_INVALID_FUNCTION;
        hook_completion = true;
    } else {
        if (MH_CreateHook(reinterpret_cast<void*>(host+0x1cb6b0), reinterpret_cast<void*>(&execute_hook), reinterpret_cast<void**>(&original_execute)) != MH_OK) return ERROR_INVALID_FUNCTION;
        hook_execute = true;
    }
    if (MH_EnableHook(MH_ALL_HOOKS) != MH_OK) return ERROR_INVALID_FUNCTION;
    output = config; awaited_callback = nullptr; deadline = GetTickCount64()+request.timeout_ms;
    config->response_size = config->status = 0;
    InterlockedExchange(&stopping, 0); InterlockedExchange(reinterpret_cast<volatile LONG*>(&config->stage), 2);
    Frame before{}; if (!account_matches() || !find_frame(&before) || before.id != frame.id || before.manager != frame.manager || before.creation != frame.creation || before.handler != frame.handler) return ERROR_NOT_READY;
    const auto script = javascript();
    const auto execute = original_execute ? original_execute : reinterpret_cast<Execute>(host+0x1cb6b0);
    execute(reinterpret_cast<void*>(frame.manager), frame.id, script.c_str());
    return ERROR_SUCCESS;
}
DWORD guarded_start(Config* config) {
    __try { return start_impl(config); } __except (EXCEPTION_EXECUTE_HANDLER) { return ERROR_INVALID_DATA; }
}
} // namespace

extern "C" __declspec(dllexport) DWORD WINAPI WxArticleBridgeAbi(void*) { return 1; }
extern "C" __declspec(dllexport) DWORD WINAPI WxArticleStart(void* parameter) {
    if (InterlockedCompareExchange(&admission, 1, 0)) return ERROR_BUSY;
    DWORD result = ERROR_INVALID_DATA;
    try { result = guarded_start(static_cast<Config*>(parameter)); } catch (...) { result = ERROR_INVALID_DATA; }
    return result;
}
static bool stop_owner(void* parameter) {
    __try {
        const auto config = static_cast<const Config*>(parameter);
        return config && config->size == sizeof(Config) && config->pid == GetCurrentProcessId() &&
            strnlen_s(config->nonce, sizeof(config->nonce)) == 32 &&
            !std::memcmp(config->nonce, request.nonce, sizeof(request.nonce));
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}
extern "C" __declspec(dllexport) DWORD WINAPI WxArticleStop(void* parameter) {
    if (InterlockedCompareExchange(&admission, 0, 0) && !stop_owner(parameter)) return ERROR_ACCESS_DENIED;
    InterlockedExchange(&stopping, 1);
    if (initialized) {
        const auto disabled = MH_DisableHook(MH_ALL_HOOKS);
        if (disabled != MH_OK && disabled != MH_ERROR_DISABLED) return ERROR_BUSY;
    }
    const auto until = GetTickCount64()+2000;
    while (InterlockedCompareExchange(&active, 0, 0) && GetTickCount64() < until) Sleep(1);
    if (InterlockedCompareExchange(&active, 0, 0)) return ERROR_BUSY;
    // Hooks are disabled before output/config memory is released. No callback is replaced.
    if (initialized && MH_Uninitialize() != MH_OK) return ERROR_BUSY;
    initialized = hook_h5 = hook_completion = hook_execute = false;
    AcquireSRWLockExclusive(&result_lock);
    output = nullptr; awaited_callback = nullptr;
    SecureZeroMemory(&request, sizeof(request));
    ReleaseSRWLockExclusive(&result_lock);
    InterlockedExchange(&admission, 0);
    return ERROR_SUCCESS;
}
BOOL APIENTRY DllMain(HMODULE, DWORD, LPVOID) { return TRUE; }
