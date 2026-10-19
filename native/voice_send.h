#pragma once
#include <vector>

// Fingerprint-locked Weixin 4.1.15.13. Static UI path:
// 5cc7fa0 -> 6bdfbc0 -> 6be0020 / 6be1240 (voice payload + duration).
struct VoiceConfig {
    TextConfig base;
    char account[128];
    uint32_t duration_ms;
    uint32_t send;
};
static_assert(sizeof(VoiceConfig) == 1824, "Unexpected voice config ABI");
static_assert(offsetof(VoiceConfig, duration_ms) == 1816, "Unexpected voice duration offset");
static_assert(offsetof(VoiceConfig, send) == 1820, "Unexpected voice mode offset");

struct VoiceOptions {
    uint32_t format = 4;
    uint32_t codec = 3;
    uint64_t timeout_ms = 30000;
    uint8_t flag = 0;
    uint8_t padding[7]{};
    SmallString extra{};
    VoiceOptions() { extra.capacity = 15; }
};
static_assert(sizeof(VoiceOptions) == 56, "Unexpected native voice options ABI");

struct VoiceTaskLease {
    VoiceConfig config;
    std::vector<unsigned char> bytes;
    explicit VoiceTaskLease(const VoiceConfig& value) : config(value) {
        _InterlockedIncrement(&live_tasks);
    }
    ~VoiceTaskLease() { _InterlockedDecrement(&live_tasks); }
};
static volatile LONG voice_admission = 0;

struct VoiceFileHandle {
    HANDLE value;
    ~VoiceFileHandle() { if (value != INVALID_HANDLE_VALUE) CloseHandle(value); }
};

static DWORD voice_read_file(VoiceTaskLease& lease) {
    QtStaticUtf16 path{};
    if (!image_file_argument(lease.config.base, &path)) return ERROR_FILE_NOT_FOUND;
    if ((path.characters[0] == L'\\' && path.characters[1] == L'\\') ||
        (path.characters[0] == L'/' && path.characters[1] == L'/')) return ERROR_BAD_PATHNAME;
    VoiceFileHandle file{CreateFileW(path.characters, GENERIC_READ, FILE_SHARE_READ, nullptr,
                                    OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, nullptr)};
    if (file.value == INVALID_HANDLE_VALUE) return GetLastError();
    LARGE_INTEGER size{};
    if (!GetFileSizeEx(file.value, &size)) return GetLastError();
    if (size.QuadPart <= 12 || size.QuadPart > 20 * 1024 * 1024) return ERROR_INVALID_DATA;
    const auto length = static_cast<size_t>(size.QuadPart);
    lease.bytes.resize(length + 1, 0); // Native std::string copy also reads its trailing NUL.
    DWORD read = 0;
    if (!ReadFile(file.value, lease.bytes.data(), static_cast<DWORD>(length), &read, nullptr))
        return GetLastError();
    if (read != length || std::memcmp(lease.bytes.data(), "\x02#!SILK_V3", 10))
        return ERROR_INVALID_DATA;
    size_t offset = 10;
    uint32_t packets = 0;
    while (offset < length) {
        if (length - offset < 2) return ERROR_INVALID_DATA;
        const uint32_t count = lease.bytes[offset] | (lease.bytes[offset + 1] << 8);
        offset += 2;
        if (!count || count > 32767 || count > length - offset) return ERROR_INVALID_DATA;
        offset += count;
        if (++packets > 3000) return ERROR_INVALID_DATA;
    }
    return packets && packets * 20 == lease.config.duration_ms ? ERROR_SUCCESS : ERROR_INVALID_DATA;
}

// Borrowed std::string layout: the voice constructor copies these bytes immediately.
// Never run a std::string destructor on this view of lease/config-owned memory.
static SmallString voice_borrow(const char* bytes, size_t length) {
    SmallString view{};
    view.length = length;
    view.capacity = length <= 15 ? 15 : length;
    if (length <= 15) std::memcpy(view.data, bytes, length + 1);
    else std::memcpy(view.data, &bytes, sizeof(bytes));
    return view;
}

static void voice_write_report(const VoiceTaskLease& lease, const TestResult& report,
                               DWORD status, bool sent) {
    FILE* log = nullptr;
    const auto& config = lease.config;
    if (_wfopen_s(&log, config.base.base.log_path, L"wb") != 0 || !log) return;
    const bool success = status == ERROR_SUCCESS && !report.exception &&
        !report.native_status && !report.native_status_high;
    std::fprintf(log, "{\"operation\":\"%s\",\"message_type\":34,\"sent\":%s,"
        "\"stage\":%lu,\"exception\":%lu,\"status\":%lu,\"native_status\":%u,"
        "\"native_status_high\":%u,\"duration_ms\":%u,\"bytes\":%llu,\"completion_success\":%s,\"target\":",
        config.send ? "voice_send" : "voice_probe", sent ? "true" : "false", report.stage,
        report.exception, status, report.native_status, report.native_status_high, config.duration_ms,
        static_cast<unsigned long long>(lease.bytes.size() - 1), success ? "true" : "false");
    write_json_string(log, config.base.target);
    std::fputs(",\"text\":", log);
    write_json_string(log, config.base.base.text);
    std::fputs(",\"voice_path\":", log);
    write_json_string(log, config.base.base.text);
    std::fputs("}\n", log);
    std::fclose(log);
}

static void voice_on_task(const VoiceTaskLease& lease) {
    const VoiceConfig& config = lease.config;
    SharedMessage context{}, provider{}, manager{}, message{};
    alignas(16) unsigned char result[128]{};
    SmallString target = voice_borrow(config.base.target, std::strlen(config.base.target));
    SmallString payload = voice_borrow(reinterpret_cast<const char*>(lease.bytes.data()), lease.bytes.size() - 1);
    // Same options as the built-in voice recorder's caller at RVA 5cc80d7.
    alignas(8) unsigned char options[56]{};
    *reinterpret_cast<uint32_t*>(options) = 4;
    *reinterpret_cast<uint32_t*>(options + 4) = 3;
    *reinterpret_cast<uint64_t*>(options + 8) = 30000;
    *reinterpret_cast<uint64_t*>(options + 48) = 15;
    uint32_t duration = config.duration_ms;
    TestResult report{};
    report.native_status = 0xffffffffu;
    DWORD status = ERROR_GEN_FAILURE;
    bool sent = false;
    _InterlockedIncrement(&active_callbacks);
    __try {
        __try {
            report.stage = 60;
            using GetContext = SharedMessage* (__fastcall*)(SharedMessage*);
            using GetService = SharedMessage* (__fastcall*)(void*, SharedMessage*);
            reinterpret_cast<GetContext>(core_base + 0x47220)(&context);
            if (!emoticon_account_matches(context, config.account)) {
                status = ERROR_NOT_READY;
                __leave;
            }
            report.stage = 61;
            using Factory = SharedMessage* (__fastcall*)(SharedMessage*, const SmallString*,
                                                        const SmallString*, const uint32_t*, const void*);
            reinterpret_cast<Factory>(core_base + 0x6be0020)(&message, &target, &payload, &duration, options);
            if (!message.object || !message.control ||
                *static_cast<const uintptr_t*>(message.object) != core_base + 0x9af9078) {
                status = ERROR_INVALID_DATA;
                __leave;
            }
            auto object = static_cast<unsigned char*>(message.object);
            *reinterpret_cast<uint32_t*>(object + 0x118) = 34;
            const auto copied = reinterpret_cast<const SmallString*>(object + 0x758);
            if (copied->length != payload.length ||
                *reinterpret_cast<const uint32_t*>(object + 0x778) != duration ||
                !emoticon_account_matches(context, config.account)) {
                status = ERROR_INVALID_DATA;
                __leave;
            }
            if (!config.send) {
                report.stage = 62;
                report.native_status = 0;
                status = ERROR_SUCCESS;
                __leave;
            }
            report.stage = 63;
            reinterpret_cast<GetService>(core_base + 0x36c740)(context.object, &provider);
            if (!provider.object || !provider.control) { status = ERROR_NOT_READY; __leave; }
            reinterpret_cast<GetService>(core_base + 0x6f4d90)(provider.object, &manager);
            if (!manager.object || !manager.control || !emoticon_account_matches(context, config.account)) {
                status = ERROR_NOT_READY;
                __leave;
            }
            SharedMessage transferred = message;
            _InterlockedIncrement(reinterpret_cast<volatile LONG*>(static_cast<unsigned char*>(message.control) + 8));
            reinterpret_cast<SendFunction>(core_base + 0x178f100)(manager.object, result, &transferred, 1);
            transferred = {}; // SendFunction consumes the retained reference.
            report.native_status = *reinterpret_cast<const uint32_t*>(result);
            report.native_status_high = *reinterpret_cast<const uint32_t*>(result + 4);
            sent = !report.native_status && !report.native_status_high;
            report.stage = 64;
            status = sent ? ERROR_SUCCESS : ERROR_GEN_FAILURE;
        } __except (record_exception(GetExceptionInformation(), &report)) {
            status = ERROR_GEN_FAILURE;
        }
        guarded_release_shared(reinterpret_cast<SharedMessage*>(result + 40), &report);
        image_free_string(reinterpret_cast<SmallString*>(result + 8), &report);
        guarded_release_shared(&message, &report);
        guarded_release_shared(&manager, &report);
        guarded_release_shared(&provider, &report);
        guarded_release_shared(&context, &report);
        voice_write_report(lease, report, status, sent);
    } __finally { _InterlockedDecrement(&active_callbacks); }
}

static DWORD voice_copy_config(void* parameter, VoiceConfig* config) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    __try { *config = *static_cast<const VoiceConfig*>(parameter); }
    __except (EXCEPTION_EXECUTE_HANDLER) { return ERROR_INVALID_PARAMETER; }
    return ERROR_SUCCESS;
}

static DWORD voice_dispatch(void* parameter, bool send) {
    if (live_tasks || live_completions) return ERROR_BUSY;
    VoiceConfig config{};
    DWORD status = voice_copy_config(parameter, &config);
    if (status) return status;
    const size_t path_length = strnlen_s(config.base.base.text, sizeof(config.base.base.text));
    const size_t target_length = strnlen_s(config.base.target, sizeof(config.base.target));
    const size_t account_length = strnlen_s(config.account, sizeof(config.account));
    const size_t log_length = wcsnlen_s(config.base.base.log_path, std::size(config.base.base.log_path));
    if (config.base.base.size != sizeof(config) || config.base.base.pid != GetCurrentProcessId() ||
        config.send != (send ? 1u : 0u) || !config.duration_ms || config.duration_ms > 60000 ||
        config.duration_ms % 20 || !path_length || path_length >= sizeof(config.base.base.text) ||
        !target_length || target_length >= sizeof(config.base.target) ||
        !account_length || account_length >= sizeof(config.account) ||
        !log_length || log_length >= std::size(config.base.base.log_path) ||
        !valid_target(config.base.target) || !valid_target(config.account) ||
        !std::strncmp(config.account, "gh_", 3) || std::strchr(config.account, '@') ||
        !std::strcmp(config.account, "filehelper")) return ERROR_INVALID_PARAMETER;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return ERROR_NOT_SUPPORTED;
    core_base = reinterpret_cast<uintptr_t>(core);
    try {
        auto lease = std::make_shared<VoiceTaskLease>(config);
        // The export copies the entire file before returning. Queued tasks never
        // depend on a Python temporary path, even if Python times out or exits.
        status = voice_read_file(*lease);
        if (status) return status;
        std::function<void()> task = [lease]() { voice_on_task(*lease); };
        return schedule_task(config.base.base, &task);
    } catch (const std::bad_alloc&) { return ERROR_NOT_ENOUGH_MEMORY; }
    catch (...) { return ERROR_GEN_FAILURE; }
}

static DWORD voice_export(void* parameter, bool send) {
    _InterlockedIncrement(&active_callbacks);
    bool admitted = false;
    __try {
        if (_InterlockedCompareExchange(&voice_admission, 1, 0) != 0) return ERROR_BUSY;
        admitted = true;
        return voice_dispatch(parameter, send);
    } __finally {
        if (admitted) _InterlockedExchange(&voice_admission, 0);
        _InterlockedDecrement(&active_callbacks);
    }
}

extern "C" __declspec(dllexport) DWORD WINAPI WxInspectVoicePath(void* parameter) {
    return voice_export(parameter, false);
}
extern "C" __declspec(dllexport) DWORD WINAPI WxSendVoice(void* parameter) {
    return voice_export(parameter, true);
}
