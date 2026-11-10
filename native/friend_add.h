#pragma once
struct FriendAddConfig {
    TextConfig base;
    char account[128];
    char room[128];
    uint32_t execute;
    uint32_t reserved;
};
static_assert(sizeof(FriendAddConfig) == 1952, "friend-add ABI mismatch");
// The copied input stays inside this process and is cleared on every return path.
struct FriendAddCopy {
    FriendAddConfig config{};
    ~FriendAddCopy() { SecureZeroMemory(&config, sizeof(config)); }
};

struct FriendAddLease {
    FriendAddConfig config;
    explicit FriendAddLease(const FriendAddConfig &v) : config(v) {
        _InterlockedIncrement(&live_tasks);
    }
    ~FriendAddLease() {
        SecureZeroMemory(&config, sizeof(config));
        _InterlockedDecrement(&live_tasks);
    }
};
static volatile LONG friend_add_admission = 0;
static void friend_add_task(const FriendAddLease &lease) {
    SharedMessage context{}, provider{}, manager{};
    SmallString target = friend_borrow_string(lease.config.base.target);
    SmallString message = friend_borrow_string(lease.config.base.base.text);
    SmallString room = friend_borrow_string(lease.config.room);
    SmallString empty{};
    empty.capacity = 15;
    alignas(8) unsigned char result[0x70]{};
    *reinterpret_cast<uint64_t *>(result + 0x20) = 15;
    *reinterpret_cast<uint64_t *>(result + 0x48) = 15;
    *reinterpret_cast<uint64_t *>(result + 0x68) = 15;
    bool used = false, success = false;
    DWORD status = ERROR_GEN_FAILURE;
    int result_code = 0;
    TestResult report{};
    _InterlockedIncrement(&active_callbacks);
    __try {
        __try {
            report.stage = 210;
            using GetContext = SharedMessage *(__fastcall *)(SharedMessage *);
            using GetService = SharedMessage *(__fastcall *)(void *, SharedMessage *);
            reinterpret_cast<GetContext>(core_address(core_base, 0x47220))(&context);
            if (!emoticon_account_matches(context, lease.config.account)) {
                status = ERROR_NOT_READY;
                __leave;
            }
            reinterpret_cast<GetService>(core_address(core_base, 0x36c740))(context.object,
                                                                            &provider);
            if (!provider.object || !provider.control) {
                status = ERROR_NOT_READY;
                __leave;
            }
            reinterpret_cast<GetService>(core_address(core_base, 0x754330))(provider.object,
                                                                            &manager);
            if (!manager.object || !manager.control ||
                !emoticon_account_matches(context, lease.config.account)) {
                status = ERROR_NOT_READY;
                __leave;
            }
            if (!lease.config.execute) {
                report.stage = 212;
                status = ERROR_SUCCESS;
                success = true;
                __leave;
            }
            report.stage = 213;
            // Verified on 4.2.0.12: opcode 2, scene 14, one current group member.
            // A successful result means the request was submitted, not accepted.
            using Verify = void *(__fastcall *)(void *, void *, const SmallString *, uint32_t,
                                                uint32_t, uint32_t, const SmallString *,
                                                const SmallString *, const SmallString *);
            used = true;
            reinterpret_cast<Verify>(core_address(core_base, 0x1a5a850))(
                manager.object, result, &target, 2, 14, 0, &empty, &message, &room);
            report.stage = 214;
            result_code = *reinterpret_cast<int32_t *>(result + 4);
            success = result[0] == 1;
            status = success ? ERROR_SUCCESS : ERROR_GEN_FAILURE;
            if (!emoticon_account_matches(context, lease.config.account)) {
                success = false;
                status = ERROR_NOT_READY;
            }
        } __except (record_exception(GetExceptionInformation(), &report)) {
            status = ERROR_GEN_FAILURE;
            success = false;
        }
        if (used) {
            __try {
                using Destroy = void(__fastcall *)(void *);
                reinterpret_cast<Destroy>(core_address(core_base, 0x7544b0))(result);
            } __except (record_exception(GetExceptionInformation(), &report)) {
                status = ERROR_GEN_FAILURE;
                success = false;
            }
        }
        guarded_release_shared(&manager, &report);
        guarded_release_shared(&provider, &report);
        guarded_release_shared(&context, &report);
        if (report.exception) {
            status = ERROR_GEN_FAILURE;
            success = false;
        }
        FILE *f = nullptr;
        if (_wfopen_s(&f, lease.config.base.base.log_path, L"wb") == 0 && f) {
            std::fprintf(f,
                         "{\"operation\":\"friend_add\",\"stage\":%lu,\"status\":%lu,\"exception\":"
                         "%lu,\"success\":%s,\"executed\":%s,\"native_code\":%d}\n",
                         report.stage, status, report.exception, success ? "true" : "false",
                         lease.config.execute ? "true" : "false", result_code);
            std::fclose(f);
        }
        SecureZeroMemory(result, sizeof(result));
        SecureZeroMemory(&target, sizeof(target));
        SecureZeroMemory(&message, sizeof(message));
        SecureZeroMemory(&room, sizeof(room));
    } __finally {
        _InterlockedDecrement(&active_callbacks);
    }
}
static DWORD friend_add_copy(void *parameter, FriendAddConfig *config) {
    __try {
        *config = *static_cast<const FriendAddConfig *>(parameter);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return ERROR_INVALID_PARAMETER;
    }
    return ERROR_SUCCESS;
}
static DWORD friend_add_dispatch(void *parameter) {
    if (!parameter || live_tasks || live_completions)
        return ERROR_BUSY;
    FriendAddCopy copy;
    FriendAddConfig &config = copy.config;
    if (friend_add_copy(parameter, &config) != ERROR_SUCCESS)
        return ERROR_INVALID_PARAMETER;
    size_t a = strnlen_s(config.account, sizeof(config.account)),
           t = strnlen_s(config.base.target, sizeof(config.base.target)),
           r = strnlen_s(config.room, sizeof(config.room)),
           m = strnlen_s(config.base.base.text, sizeof(config.base.base.text)),
           l = wcsnlen_s(config.base.base.log_path, std::size(config.base.base.log_path));
    if (config.base.base.size != sizeof(config) || config.base.base.pid != GetCurrentProcessId() ||
        config.reserved || config.execute > 1 || !a || a >= sizeof(config.account) || !t ||
        t >= sizeof(config.base.target) || !r || r >= sizeof(config.room) ||
        m >= sizeof(config.base.base.text) || !l || l >= std::size(config.base.base.log_path) ||
        !friend_valid_account(config.account) || !friend_valid_account(config.base.target) ||
        !valid_target(config.room) || r < 10 || std::strcmp(config.room + r - 9, "@chatroom") ||
        !std::strcmp(config.account, config.base.target))
        return ERROR_INVALID_PARAMETER;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core) || InterlockedCompareExchange(&core_profile_id, 0, 0) != 4)
        return ERROR_NOT_SUPPORTED;
    core_base = reinterpret_cast<uintptr_t>(core);
    DWORD code = ERROR_GEN_FAILURE;
    try {
        auto lease = std::make_shared<FriendAddLease>(config);
        std::function<void()> task = [lease]() { friend_add_task(*lease); };
        code = schedule_task(config.base.base, &task);
    } catch (...) {
        code = ERROR_GEN_FAILURE;
    }
    SecureZeroMemory(&config, sizeof(config));
    return code;
}
extern "C" __declspec(dllexport) DWORD WINAPI WxSendFriendRequest(void *parameter) {
    _InterlockedIncrement(&active_callbacks);
    bool admitted = false;
    __try {
        if (_InterlockedCompareExchange(&friend_add_admission, 1, 0) != 0)
            return ERROR_BUSY;
        admitted = true;
        return friend_add_dispatch(parameter);
    } __finally {
        if (admitted)
            _InterlockedExchange(&friend_add_admission, 0);
        _InterlockedDecrement(&active_callbacks);
    }
}
