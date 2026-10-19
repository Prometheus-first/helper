#pragma once
#include <memory>

// Fingerprint-locked Weixin 4.1.15.13. These entry points reuse the
// client's ordinary ChatroomService; they never bypass server permissions.
// Static callers: create 745790 -> 16da4f0; invite 74be60 -> 16f3390.
struct GroupManagementConfig {
    TextConfig base;
    char account[128];
    char members[1024];
    uint32_t operation; // 1 = invite existing friends, 2 = create a group.
    uint32_t reserved;
};
static_assert(sizeof(GroupManagementConfig) == 2848, "Unexpected group-management config ABI");
static_assert(offsetof(GroupManagementConfig, account) == 1688, "Unexpected group account offset");
static_assert(offsetof(GroupManagementConfig, members) == 1816, "Unexpected group members offset");
static_assert(offsetof(GroupManagementConfig, operation) == 2840, "Unexpected group mode offset");

struct GroupMemberVector {
    SmallString* begin;
    SmallString* end;
    SmallString* capacity;
};
static_assert(sizeof(GroupMemberVector) == 24, "Unexpected member vector ABI");

struct GroupManagementLease {
    GroupManagementConfig config;
    char members[8][128]{};
    uint32_t count = 0;
    bool execute = false;
    explicit GroupManagementLease(const GroupManagementConfig& value, bool send)
        : config(value), execute(send) { _InterlockedIncrement(&live_tasks); }
    ~GroupManagementLease() { _InterlockedDecrement(&live_tasks); }
};
static volatile LONG group_management_admission = 0;

static bool group_valid_person(const char* value) {
    if (!valid_target(value) || std::strchr(value, '@') ||
        !std::strncmp(value, "gh_", 3)) return false;
    const char* excluded[] = {".", "..", "filehelper", "weixin", "fmessage", "newsapp", "floatbottle"};
    for (const char* peer : excluded) if (!std::strcmp(value, peer)) return false;
    return true;
}

static bool group_valid_room(const char* value) {
    if (!valid_target(value)) return false;
    const size_t length = std::strlen(value);
    constexpr char suffix[] = "@chatroom";
    return length > sizeof(suffix) - 1 &&
        !std::strcmp(value + length - (sizeof(suffix) - 1), suffix);
}

static DWORD group_parse_members(GroupManagementLease& lease) {
    const char* cursor = lease.config.members;
    while (*cursor) {
        if (lease.count >= 8) return ERROR_INVALID_PARAMETER;
        const char* comma = std::strchr(cursor, ',');
        const size_t length = comma ? static_cast<size_t>(comma - cursor) : std::strlen(cursor);
        if (!length || length >= 128) return ERROR_INVALID_PARAMETER;
        char* member = lease.members[lease.count];
        std::memcpy(member, cursor, length);
        member[length] = 0;
        if (!group_valid_person(member) || !std::strcmp(member, lease.config.account))
            return ERROR_INVALID_PARAMETER;
        for (uint32_t index = 0; index < lease.count; ++index)
            if (!std::strcmp(member, lease.members[index])) return ERROR_INVALID_PARAMETER;
        ++lease.count;
        if (!comma) break;
        cursor = comma + 1;
        if (!*cursor) return ERROR_INVALID_PARAMETER;
    }
    // These are framework interface limits, not claims about WeChat's limits.
    return lease.count >= (lease.config.operation == 2 ? 2u : 1u)
        ? ERROR_SUCCESS : ERROR_INVALID_PARAMETER;
}

// Borrowed input views. ChatroomService copies the member strings into its
// own protobuf request; no native std::string destructor may consume a view.
static SmallString group_borrow_string(const char* text) {
    SmallString result{};
    result.length = std::strlen(text);
    result.capacity = result.length <= 15 ? 15 : result.length;
    if (result.length <= 15) std::memcpy(result.data, text, result.length + 1);
    else std::memcpy(result.data, &text, sizeof(text));
    return result;
}

static void group_write_report(const GroupManagementLease& lease, const TestResult& report,
                               DWORD status, bool success, const char* room) {
    FILE* log = nullptr;
    if (_wfopen_s(&log, lease.config.base.base.log_path, L"wb") != 0 || !log) return;
    const char* operation = !lease.execute ? "group_probe" :
        (lease.config.operation == 1 ? "group_invite" : "group_create");
    std::fprintf(log, "{\"operation\":\"%s\",\"mode\":%u,\"stage\":%lu,"
        "\"exception\":%lu,\"status\":%lu,\"completion_success\":%s,\"sent\":%s,"
        "\"member_count\":%u,\"room_id\":", operation, lease.config.operation,
        report.stage, report.exception, status, success ? "true" : "false",
        lease.execute && success ? "true" : "false", lease.count);
    write_json_string(log, room ? room : "");
    std::fputs("}\n", log);
    std::fclose(log);
}

static void group_management_on_task(const GroupManagementLease& lease) {
    SharedMessage context{}, provider{}, manager{};
    SmallString member_strings[8]{};
    for (uint32_t index = 0; index < lease.count; ++index)
        member_strings[index] = group_borrow_string(lease.members[index]);
    GroupMemberVector members{member_strings, member_strings + lease.count,
                              member_strings + lease.count};
    SmallString target = group_borrow_string(lease.config.base.target);
    SmallString extra{};
    extra.capacity = 15;
    SmallString created{};
    created.capacity = 15;
    // micromsg::SceneContext is 0x50 bytes. The normal UI caller constructs
    // this empty object before calling InviteChatRoomMember.
    alignas(8) unsigned char scene[0x50]{};
    bool scene_initialized = false;
    bool success = false;
    char room[128]{};
    if (lease.config.operation == 1) strcpy_s(room, lease.config.base.target);
    TestResult report{};
    DWORD status = ERROR_GEN_FAILURE;
    _InterlockedIncrement(&active_callbacks);
    __try {
        __try {
            report.stage = 90;
            using GetContext = SharedMessage* (__fastcall*)(SharedMessage*);
            using GetService = SharedMessage* (__fastcall*)(void*, SharedMessage*);
            reinterpret_cast<GetContext>(core_base + 0x47220)(&context);
            if (!emoticon_account_matches(context, lease.config.account)) {
                status = ERROR_NOT_READY;
                __leave;
            }
            reinterpret_cast<GetService>(core_base + 0x36c740)(context.object, &provider);
            if (!provider.object || !provider.control) { status = ERROR_NOT_READY; __leave; }
            reinterpret_cast<GetService>(core_base + 0x6db150)(provider.object, &manager);
            if (!manager.object || !manager.control) { status = ERROR_NOT_READY; __leave; }
            report.stage = 91;
            using SceneConstructor = void (__fastcall*)(void*);
            reinterpret_cast<SceneConstructor>(core_base + 0x1984610)(scene);
            scene_initialized = true;
            if (*reinterpret_cast<uintptr_t*>(scene) != core_base + 0x910b378 ||
                !emoticon_account_matches(context, lease.config.account)) {
                status = ERROR_INVALID_DATA;
                __leave;
            }
            if (!lease.execute) {
                report.stage = 92;
                status = ERROR_SUCCESS;
                success = true;
                __leave;
            }
            report.stage = 93;
            if (lease.config.operation == 1) {
                // A successful invitation does not imply the invitees have joined.
                using Invite = bool (__fastcall*)(void*, GroupMemberVector*, SmallString*, void*);
                success = reinterpret_cast<Invite>(core_base + 0x16f3390)(
                    manager.object, &members, &target, scene);
            } else {
                using Create = SmallString* (__fastcall*)(void*, SmallString*,
                                                          GroupMemberVector*, SmallString*);
                reinterpret_cast<Create>(core_base + 0x16da4f0)(
                    manager.object, &created, &members, &extra);
                // Only return the group ID supplied by the successful server response.
                // Empty/invalid output is failure; never guess a room from recent chats.
                if (created.length && created.length < sizeof(room) &&
                    created.capacity >= created.length) {
                    const char* text = created.capacity <= 15 ? created.data :
                        *reinterpret_cast<char* const*>(created.data);
                    if (text) {
                        std::memcpy(room, text, created.length);
                        room[created.length] = 0;
                        success = group_valid_room(room);
                        if (!success) room[0] = 0;
                    }
                }
            }
            report.stage = 94;
            if (!emoticon_account_matches(context, lease.config.account)) {
                // A completed create may already have changed server state. Keep
                // its returned ID for diagnosis, but never report another account's
                // result as a successful operation or automatically retry it.
                success = false;
                status = ERROR_NOT_READY;
            } else {
                status = success ? ERROR_SUCCESS : ERROR_GEN_FAILURE;
            }
        } __except (record_exception(GetExceptionInformation(), &report)) {
            status = ERROR_GEN_FAILURE;
            success = false;
        }
        if (scene_initialized) {
            __try {
                using SceneDestructor = void (__fastcall*)(void*);
                reinterpret_cast<SceneDestructor>(core_base + 0x198e2e0)(scene);
            } __except (record_exception(GetExceptionInformation(), &report)) {
                status = ERROR_GEN_FAILURE;
                success = false;
            }
        }
        image_free_string(&created, &report);
        guarded_release_shared(&manager, &report);
        guarded_release_shared(&provider, &report);
        guarded_release_shared(&context, &report);
        if (report.exception) { status = ERROR_GEN_FAILURE; success = false; }
        group_write_report(lease, report, status, success, room);
    } __finally { _InterlockedDecrement(&active_callbacks); }
}

static DWORD group_copy_config(void* parameter, GroupManagementConfig* config) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    __try { *config = *static_cast<const GroupManagementConfig*>(parameter); }
    __except (EXCEPTION_EXECUTE_HANDLER) { return ERROR_INVALID_PARAMETER; }
    return ERROR_SUCCESS;
}

static DWORD group_management_dispatch(void* parameter, bool execute, uint32_t required_mode) {
    if (live_tasks || live_completions) return ERROR_BUSY;
    GroupManagementConfig config{};
    DWORD status = group_copy_config(parameter, &config);
    if (status) return status;
    const size_t member_length = strnlen_s(config.members, sizeof(config.members));
    const size_t target_length = strnlen_s(config.base.target, sizeof(config.base.target));
    const size_t account_length = strnlen_s(config.account, sizeof(config.account));
    const size_t log_length = wcsnlen_s(config.base.base.log_path, std::size(config.base.base.log_path));
    if (config.base.base.size != sizeof(config) || config.base.base.pid != GetCurrentProcessId() ||
        (config.operation != 1 && config.operation != 2) || config.reserved ||
        (required_mode && config.operation != required_mode) ||
        !member_length || member_length >= sizeof(config.members) ||
        target_length >= sizeof(config.base.target) ||
        !account_length || account_length >= sizeof(config.account) ||
        !log_length || log_length >= std::size(config.base.base.log_path) ||
        !group_valid_person(config.account) ||
        (config.operation == 1 ? !group_valid_room(config.base.target) : target_length != 0))
        return ERROR_INVALID_PARAMETER;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return ERROR_NOT_SUPPORTED;
    core_base = reinterpret_cast<uintptr_t>(core);
    try {
        auto lease = std::make_shared<GroupManagementLease>(config, execute);
        status = group_parse_members(*lease);
        if (status) return status;
        std::function<void()> task = [lease]() { group_management_on_task(*lease); };
        return schedule_task(config.base.base, &task);
    } catch (const std::bad_alloc&) { return ERROR_NOT_ENOUGH_MEMORY; }
    catch (...) { return ERROR_GEN_FAILURE; }
}

static DWORD group_management_export(void* parameter, bool execute, uint32_t required_mode) {
    _InterlockedIncrement(&active_callbacks);
    bool admitted = false;
    __try {
        if (_InterlockedCompareExchange(&group_management_admission, 1, 0) != 0) return ERROR_BUSY;
        admitted = true;
        return group_management_dispatch(parameter, execute, required_mode);
    } __finally {
        if (admitted) _InterlockedExchange(&group_management_admission, 0);
        _InterlockedDecrement(&active_callbacks);
    }
}

extern "C" __declspec(dllexport) DWORD WINAPI WxInspectGroupManagement(void* parameter) {
    return group_management_export(parameter, false, 0);
}
extern "C" __declspec(dllexport) DWORD WINAPI WxInviteGroupMembers(void* parameter) {
    return group_management_export(parameter, true, 1);
}
extern "C" __declspec(dllexport) DWORD WINAPI WxCreateGroup(void* parameter) {
    return group_management_export(parameter, true, 2);
}
