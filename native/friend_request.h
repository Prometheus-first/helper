#pragma once
#include <memory>

// Weixin 4.1.15.13 only. The ordinary client path is:
// 1780cd0 -> 754330 (VerifyUser service) -> 1a26440 -> 1a17980,
// where 1a26440 supplies VerifyUser opcode 3 and waits for its response.
// The credentials are borrowed only during that synchronous service call.
struct FriendRequestConfig {
    TextConfig base;
    char account[128];
    char v3[8192];
    char v4[8192];
    uint32_t scene;
    uint32_t reserved;
};
static_assert(sizeof(FriendRequestConfig) == 18208, "Unexpected friend-request config ABI");
static_assert(offsetof(FriendRequestConfig, account) == 1688, "Unexpected friend account offset");
static_assert(offsetof(FriendRequestConfig, v3) == 1816, "Unexpected friend v3 offset");
static_assert(offsetof(FriendRequestConfig, v4) == 10008, "Unexpected friend v4 offset");
static_assert(offsetof(FriendRequestConfig, scene) == 18200, "Unexpected friend scene offset");

struct FriendRequestLease {
    FriendRequestConfig config;
    bool execute;
    explicit FriendRequestLease(const FriendRequestConfig& value, bool send)
        : config(value), execute(send) { _InterlockedIncrement(&live_tasks); }
    ~FriendRequestLease() {
        SecureZeroMemory(config.v3, sizeof(config.v3));
        SecureZeroMemory(config.v4, sizeof(config.v4));
        _InterlockedDecrement(&live_tasks);
    }
};

// Erase the export's short-lived input copy on all C++ return paths too.
struct FriendRequestCopy {
    FriendRequestConfig config{};
    ~FriendRequestCopy() {
        SecureZeroMemory(config.v3, sizeof(config.v3));
        SecureZeroMemory(config.v4, sizeof(config.v4));
    }
};
static volatile LONG friend_request_admission = 0;

static SmallString friend_borrow_string(const char* text) {
    SmallString result{};
    result.length = std::strlen(text);
    result.capacity = result.length <= 15 ? 15 : result.length;
    if (result.length <= 15) std::memcpy(result.data, text, result.length + 1);
    else std::memcpy(result.data, &text, sizeof(text));
    return result;
}

static bool friend_valid_account(const char* value) {
    return valid_target(value) && !std::strchr(value, '@') &&
        std::strcmp(value, "filehelper") && std::strcmp(value, "weixin") &&
        std::strcmp(value, "fmessage");
}

static void friend_write_report(const FriendRequestLease& lease, const TestResult& report,
                                DWORD status, bool success) {
    FILE* log = nullptr;
    if (_wfopen_s(&log, lease.config.base.base.log_path, L"wb") != 0 || !log) return;
    // Do not write the XML, ticket, v3/v4, native response text or account ID.
    std::fprintf(log, "{\"operation\":\"%s\",\"stage\":%lu,\"exception\":%lu,"
        "\"status\":%lu,\"completion_success\":%s,\"accepted\":%s,\"sent\":%s,\"scene\":%u}\n",
        lease.execute ? "friend_accept" : "friend_probe", report.stage, report.exception,
        status, success ? "true" : "false", lease.execute && success ? "true" : "false",
        lease.execute && success ? "true" : "false", lease.config.scene);
    std::fclose(log);
}

static void friend_request_on_task(const FriendRequestLease& lease) {
    SharedMessage context{}, provider{}, manager{};
    SmallString v3 = friend_borrow_string(lease.config.v3);
    SmallString ticket = friend_borrow_string(lease.config.v4);
    SmallString empty{};
    empty.capacity = 15;
    // VerifyUserResult: success byte + error, then strings at 8, 0x30, 0x50.
    // Initialize the capacities so exceptional cleanup is safe before dispatch.
    alignas(8) unsigned char result[0x70]{};
    *reinterpret_cast<uint64_t*>(result + 0x20) = 15;
    *reinterpret_cast<uint64_t*>(result + 0x48) = 15;
    *reinterpret_cast<uint64_t*>(result + 0x68) = 15;
    bool result_used = false;
    bool success = false;
    DWORD status = ERROR_GEN_FAILURE;
    TestResult report{};
    _InterlockedIncrement(&active_callbacks);
    __try {
        __try {
            report.stage = 110;
            using GetContext = SharedMessage* (__fastcall*)(SharedMessage*);
            using GetService = SharedMessage* (__fastcall*)(void*, SharedMessage*);
            reinterpret_cast<GetContext>(core_address(core_base, 0x47220))(&context);
            if (!emoticon_account_matches(context, lease.config.account)) {
                status = ERROR_NOT_READY;
                __leave;
            }
            reinterpret_cast<GetService>(core_address(core_base, 0x36c740))(context.object, &provider);
            if (!provider.object || !provider.control) { status = ERROR_NOT_READY; __leave; }
            reinterpret_cast<GetService>(core_address(core_base, 0x754330))(provider.object, &manager);
            if (!manager.object || !manager.control) { status = ERROR_NOT_READY; __leave; }
            report.stage = 111;
            if (!emoticon_account_matches(context, lease.config.account)) {
                status = ERROR_NOT_READY;
                __leave;
            }
            if (!lease.execute) {
                // A probe validates the service/account without issuing VerifyUser.
                report.stage = 112;
                status = ERROR_SUCCESS;
                success = true;
                __leave;
            }
            report.stage = 113;
            using AcceptFriend = void* (__fastcall*)(void*, void*, const SmallString*,
                uint32_t, uint32_t, const SmallString*, const SmallString*, const SmallString*);
            result_used = true;
            reinterpret_cast<AcceptFriend>(core_address(core_base, 0x1a26440))(manager.object, result,
                &v3, lease.config.scene, 0, &ticket, &empty, &empty);
            report.stage = 114;
            // The native result's error starts at -1 and may stay there even on
            // success; the first byte is the client's authoritative success flag.
            success = result[0] == 1;
            if (!emoticon_account_matches(context, lease.config.account)) {
                // The server may already have accepted this request. Never retry
                // automatically merely because the active account changed.
                success = false;
                status = ERROR_NOT_READY;
            } else {
                status = success ? ERROR_SUCCESS : ERROR_GEN_FAILURE;
            }
        } __except (record_exception(GetExceptionInformation(), &report)) {
            status = ERROR_GEN_FAILURE;
            success = false;
        }
        if (result_used) {
            __try {
                using DestroyResult = void (__fastcall*)(void*);
                reinterpret_cast<DestroyResult>(core_address(core_base, 0x7544b0))(result);
            } __except (record_exception(GetExceptionInformation(), &report)) {
                status = ERROR_GEN_FAILURE;
                success = false;
            }
        }
        guarded_release_shared(&manager, &report);
        guarded_release_shared(&provider, &report);
        guarded_release_shared(&context, &report);
        if (report.exception) { status = ERROR_GEN_FAILURE; success = false; }
        SecureZeroMemory(&v3, sizeof(v3));
        SecureZeroMemory(&ticket, sizeof(ticket));
        SecureZeroMemory(result, sizeof(result));
        friend_write_report(lease, report, status, success);
    } __finally { _InterlockedDecrement(&active_callbacks); }
}

static DWORD friend_request_copy(void* parameter, FriendRequestConfig* config) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    __try { *config = *static_cast<const FriendRequestConfig*>(parameter); }
    __except (EXCEPTION_EXECUTE_HANDLER) { return ERROR_INVALID_PARAMETER; }
    return ERROR_SUCCESS;
}

static DWORD friend_request_dispatch(void* parameter, bool execute) {
    if (live_tasks || live_completions) return ERROR_BUSY;
    FriendRequestCopy copy;
    DWORD status = friend_request_copy(parameter, &copy.config);
    if (status) return status;
    const FriendRequestConfig& config = copy.config;
    const size_t account_length = strnlen_s(config.account, sizeof(config.account));
    const size_t v3_length = strnlen_s(config.v3, sizeof(config.v3));
    const size_t v4_length = strnlen_s(config.v4, sizeof(config.v4));
    const size_t log_length = wcsnlen_s(config.base.base.log_path, std::size(config.base.base.log_path));
    if (config.base.base.size != sizeof(config) || config.base.base.pid != GetCurrentProcessId() ||
        config.reserved || config.scene > 255 || config.base.target[0] ||
        !account_length || account_length >= sizeof(config.account) ||
        !v3_length || v3_length >= sizeof(config.v3) ||
        !v4_length || v4_length >= sizeof(config.v4) ||
        !log_length || log_length >= std::size(config.base.base.log_path) ||
        !friend_valid_account(config.account)) return ERROR_INVALID_PARAMETER;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return ERROR_NOT_SUPPORTED;
    core_base = reinterpret_cast<uintptr_t>(core);
    try {
        auto lease = std::make_shared<FriendRequestLease>(config, execute);
        std::function<void()> task = [lease]() { friend_request_on_task(*lease); };
        return schedule_task(config.base.base, &task);
    } catch (const std::bad_alloc&) { return ERROR_NOT_ENOUGH_MEMORY; }
    catch (...) { return ERROR_GEN_FAILURE; }
}

static DWORD friend_request_export(void* parameter, bool execute) {
    _InterlockedIncrement(&active_callbacks);
    bool admitted = false;
    __try {
        if (_InterlockedCompareExchange(&friend_request_admission, 1, 0) != 0) return ERROR_BUSY;
        admitted = true;
        return friend_request_dispatch(parameter, execute);
    } __finally {
        if (admitted) _InterlockedExchange(&friend_request_admission, 0);
        _InterlockedDecrement(&active_callbacks);
    }
}

extern "C" __declspec(dllexport) DWORD WINAPI WxInspectFriendRequest(void* parameter) {
    return friend_request_export(parameter, false);
}
extern "C" __declspec(dllexport) DWORD WINAPI WxAcceptFriendRequest(void* parameter) {
    return friend_request_export(parameter, true);
}
