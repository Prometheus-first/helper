#include "../native/text_bridge.cpp"

#include <cassert>
#include <fstream>
#include <iterator>
#include <string>

static std::string field(const std::string& value) {
    std::string encoded(1, '\x0a');
    const auto length = value.size();
    encoded.push_back(static_cast<char>((length & 0x7f) | (length >= 128 ? 0x80 : 0)));
    if (length >= 128) encoded.push_back('\x01');
    return encoded + value;
}

struct Request { uintptr_t vtable; void* list; };
struct List { uintptr_t vtable; const void** entries; int32_t count; };
struct Command {
    uintptr_t vtable;
    void* payload;
    uint32_t type;
    uint32_t cached_size;
    uint32_t presence;
};
struct Payload { uintptr_t vtable; SmallString* value; uint32_t length; uint32_t cached_size; uint32_t presence; };
struct ReturnCodes { uintptr_t vtable; const int32_t* codes; int32_t count; };
struct Response { uintptr_t vtable; void* results; int32_t result; uint32_t cached_size, presence; };
struct Exchange { int32_t error, detail; SmallString message; void* response; };

static void* __fastcall exchange_stub(void* output, void*, const void*, const void*, const void*) {
    return output;
}

static std::string read_report(const wchar_t* path) {
    std::ifstream report(path, std::ios::binary);
    return {std::istreambuf_iterator<char>(report), std::istreambuf_iterator<char>()};
}

static void test_42012_objects() {
    static_assert(offsetof(Command, type) == 0x10);
    static_assert(offsetof(Command, presence) == 0x18);
    static_assert(offsetof(Payload, length) == 0x10);
    static_assert(offsetof(Response, presence) == 0x18);
    static_assert(offsetof(Exchange, response) == 0x28);
    core_base = 0x180000000;
    core_profile_id = 4;
    const auto requested_bytes = field(field("wxid_fake_peer"));
    const auto other_bytes = field(field("wxid_other_peer"));
    auto requested = friend_borrow_string(requested_bytes.c_str());
    auto other = friend_borrow_string(other_bytes.c_str());
    Payload target_payload{core_base + 0x9116c88, &requested, static_cast<uint32_t>(requested.length), 0, 3};
    Payload other_payload{core_base + 0x9116c88, &other, static_cast<uint32_t>(other.length), 0, 3};
    Command unrelated{core_base + 0x96d13a8, &target_payload, 16, 0, 3};
    Command same_type{core_base + 0x96d13a8, &other_payload, 4, 0, 3};
    Command target{core_base + 0x96d13a8, &target_payload, 4, 0, 3};
    const void* entries[]{&unrelated, &same_type, &target};
    List list{core_base + 0x96d1428, entries, 3};
    Request request{core_base + 0x9828098, &list};

    size_t count = 0;
    const int index = friend_delete_find_command(&request, "wxid_fake_peer", count);
    assert(index == 2 && count == 3);
    int32_t codes[]{-18, 0, -30};
    ReturnCodes rows{core_base + 0x9828018, codes, 3};
    Response response{core_base + 0x9828118, &rows, 0, 0, 3};
    Exchange exchange{0, 0, {}, &response};
    int32_t code = 0;
    assert(friend_delete_read_result(&exchange, count, index, code) && code == -30);
    codes[index] = friend_delete_reply::retry_code;
    assert(!friend_delete_read_result(&exchange, count, index, code));
    codes[index] = 0;
    assert(friend_delete_read_result(&exchange, count, index, code) && code == 0);
    exchange.error = -1;
    assert(!friend_delete_read_result(&exchange, count, index, code));
    exchange.error = 0;
    response.presence = 1;
    assert(!friend_delete_read_result(&exchange, count, index, code));
    response.presence = 3;
    --target_payload.length;
    assert(friend_delete_find_command(&request, "wxid_fake_peer", count) == -1);
    ++target_payload.length;
    target.presence = 1;
    assert(friend_delete_find_command(&request, "wxid_fake_peer", count) == -1);
    target.presence = 3;

    // Exercise the production hook against offline exchange replies and verify
    // that the target's status alone produces one report for each generation.
    wchar_t temporary[MAX_PATH]{};
    assert(GetTempPathW(MAX_PATH, temporary) && wcsnlen_s(temporary, MAX_PATH) < MAX_PATH);
    assert(GetTempFileNameW(temporary, L"wfd", 0, friend_delete_pending.report));
    strcpy_s(friend_delete_pending.wxid, "wxid_fake_peer");
    original_oplog_exchange = exchange_stub;
    friend_delete_pending.generation = 1;
    friend_delete_pending.expires = GetTickCount64() + 10000;
    codes[index] = friend_delete_reply::retry_code;
    assert(friend_delete_exchange(&exchange, nullptr, &request, nullptr, nullptr) == &exchange);
    assert(read_report(friend_delete_pending.report).empty() && friend_delete_pending.expires);
    codes[index] = 0;
    friend_delete_exchange(&exchange, nullptr, &request, nullptr, nullptr);
    assert(read_report(friend_delete_pending.report) ==
        "{\"operation\":\"friend_delete\",\"replied\":true,\"native_code\":0}\n");
    assert(!friend_delete_pending.expires);
    codes[index] = -30;
    friend_delete_exchange(&exchange, nullptr, &request, nullptr, nullptr);
    assert(read_report(friend_delete_pending.report).find("\"native_code\":0") != std::string::npos);
    ++friend_delete_pending.generation;
    friend_delete_pending.expires = GetTickCount64() + 10000;
    friend_delete_exchange(&exchange, nullptr, &request, nullptr, nullptr);
    assert(read_report(friend_delete_pending.report) ==
        "{\"operation\":\"friend_delete\",\"replied\":true,\"native_code\":-30}\n");
    assert(!friend_delete_pending.expires && friend_delete_reply_callbacks == 0);
    assert(DeleteFileW(friend_delete_pending.report));

    entries[0] = &target;
    assert(friend_delete_find_command(&request, "wxid_fake_peer", count) == -1);
    request.vtable = 0;
    assert(friend_delete_find_command(&request, "wxid_fake_peer", count) == -1);
}

int main() {
    using namespace friend_delete_reply;
    const std::string target = "wxid_test_peer";
    const std::string long_target(127, 'a');
    assert(matches_contact(field(field(target)), target));
    assert(matches_contact(field(field(long_target)), long_target));
    assert(!matches_contact(field(field(target)), "wxid_other_peer"));
    assert(!matches_contact(field(target), target));
    assert(!matches_contact(field(field(target)) + '\0', target));
    assert(!matches_contact(field(field(target)).substr(0, 5), target));

    constexpr int32_t codes[]{-12, 0, retry_code};
    int32_t code = 1;
    assert(terminal_code(0, 0, 0, codes, 3, 3, 1, code) && code == 0);
    assert(terminal_code(0, 0, 0, codes, 3, 3, 0, code) && code == -12);
    assert(!terminal_code(0, 0, 0, codes, 3, 3, 2, code));
    assert(!terminal_code(1, 0, 0, codes, 3, 3, 1, code));
    assert(!terminal_code(0, 1, 0, codes, 3, 3, 1, code));
    assert(!terminal_code(0, 0, 1, codes, 3, 3, 1, code));
    assert(!terminal_code(0, 0, 0, codes, 2, 3, 1, code));
    assert(!terminal_code(0, 0, 0, codes, 3, 3, 3, code));
    assert(!terminal_code(0, 0, 0, nullptr, 3, 3, 1, code));
    test_42012_objects();
}
