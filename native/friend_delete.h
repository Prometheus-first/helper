#pragma once
#include "friend_delete_reply.h"

struct FriendDeleteConfig {
    TextConfig base;
    char account[128];
    uint32_t reserved[2];
};
static_assert(sizeof(FriendDeleteConfig) == 1824, "friend-delete ABI mismatch");

struct FriendDeleteLease {
    FriendDeleteConfig config;
    ULONGLONG deadline;
    explicit FriendDeleteLease(const FriendDeleteConfig& value)
        : config(value), deadline(GetTickCount64() + 25000) {
        _InterlockedIncrement(&live_tasks);
    }
    ~FriendDeleteLease() {
        SecureZeroMemory(&config, sizeof(config));
        _InterlockedDecrement(&live_tasks);
    }
};

struct FriendDeletePending {
    uint64_t generation;
    ULONGLONG expires;
    char wxid[128];
    wchar_t report[MAX_PATH];
};

using OplogExchange = void* (__fastcall*)(void*, void*, const void*, const void*, const void*);
static OplogExchange original_oplog_exchange = nullptr;
static SRWLOCK friend_delete_lock = SRWLOCK_INIT;
static FriendDeletePending friend_delete_pending{};
static volatile LONG friend_delete_admission = 0;

static void friend_delete_report(const wchar_t* path, bool replied, int32_t code) {
    FILE* file = nullptr;
    if (_wfopen_s(&file, path, L"wb") != 0 || !file) return;
    std::fprintf(file,
        "{\"operation\":\"friend_delete\",\"replied\":%s,\"native_code\":%d}\n",
        replied ? "true" : "false", code);
    std::fclose(file);
}

template<class T>
static T friend_oplog_field(const void* object, size_t offset) {
    return *reinterpret_cast<const T*>(static_cast<const char*>(object) + offset);
}

static bool friend_oplog_is(const void* object, uintptr_t vtable) {
    return object && vtable && friend_oplog_field<uintptr_t>(object, 0) == vtable;
}

static int friend_delete_find_command(const void* request, const char* wxid, size_t& count) {
    __try {
        if (!friend_oplog_is(request, core_address(core_base, 0x9828098))) return -1;
        const auto list = friend_oplog_field<const void*>(request, 8);
        if (!friend_oplog_is(list, core_address(core_base, 0x96d1428))) return -1;
        const int32_t size = friend_oplog_field<int32_t>(list, 0x10);
        const auto items = friend_oplog_field<const void* const*>(list, 8);
        if (size <= 0 || size > 4096 || !items) return -1;
        int found = -1;
        for (int i = 0; i < size; ++i) {
            const void* command = items[i];
            if (!friend_oplog_is(command, core_address(core_base, 0x96d13a8))) return -1;
            // CmdItem::CmdId is at +0x10. The word at +0x18 holds presence bits.
            if (friend_oplog_field<uint32_t>(command, 0x10) != 4) continue;
            if ((friend_oplog_field<uint32_t>(command, 0x18) & 3) != 3) return -1;
            const auto payload = friend_oplog_field<const void*>(command, 8);
            if (!friend_oplog_is(payload, core_address(core_base, 0x9116c88))) return -1;
            const auto value = friend_oplog_field<const SmallString*>(payload, 8);
            if (!value || value->length > 134 || value->length > value->capacity ||
                value->length != friend_oplog_field<uint32_t>(payload, 0x10)) return -1;
            const char* bytes = value->capacity < 16 ? value->data :
                friend_oplog_field<const char*>(value, 0);
            if (!bytes || !friend_delete_reply::matches_contact({bytes, value->length}, wxid))
                continue;
            if (found >= 0) return -1;
            found = i;
        }
        count = static_cast<size_t>(size);
        return found;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return -1;
    }
}

static bool friend_delete_read_result(const void* exchange, size_t count, int index, int32_t& code) {
    __try {
        if (!exchange) return false;
        const auto response = friend_oplog_field<const void*>(exchange, 0x28);
        if (!friend_oplog_is(response, core_address(core_base, 0x9828118))) return false;
        // OplogResponse field 2 is the nested OplogRet with the indexed codes.
        if (!(friend_oplog_field<uint32_t>(response, 0x18) & 2)) return false;
        const auto results = friend_oplog_field<const void*>(response, 8);
        if (!friend_oplog_is(results, core_address(core_base, 0x9828018))) return false;
        const int32_t size = friend_oplog_field<int32_t>(results, 0x10);
        if (size <= 0) return false;
        return friend_delete_reply::terminal_code(
            friend_oplog_field<int32_t>(exchange, 0),
            friend_oplog_field<int32_t>(exchange, 4),
            friend_oplog_field<int32_t>(response, 0x10),
            friend_oplog_field<const int32_t*>(results, 8), static_cast<size_t>(size),
            count, static_cast<size_t>(index), code);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return false;
    }
}

static void* __fastcall friend_delete_exchange(void* output, void* service, const void* request,
                                               const void* tag, const void* options) {
    _InterlockedIncrement(&friend_delete_reply_callbacks);
    __try {
        FriendDeletePending pending{};
        size_t count = 0;
        int index = -1;
        AcquireSRWLockShared(&friend_delete_lock);
        if (friend_delete_pending.expires > GetTickCount64()) pending = friend_delete_pending;
        ReleaseSRWLockShared(&friend_delete_lock);
        if (pending.expires) index = friend_delete_find_command(request, pending.wxid, count);
        void* result = original_oplog_exchange(output, service, request, tag, options);
        int32_t code = 0;
        if (index >= 0 && friend_delete_read_result(result, count, index, code)) {
            AcquireSRWLockExclusive(&friend_delete_lock);
            const bool matched = friend_delete_pending.generation == pending.generation &&
                friend_delete_pending.expires > GetTickCount64();
            if (matched) friend_delete_pending.expires = 0;
            ReleaseSRWLockExclusive(&friend_delete_lock);
            if (matched) friend_delete_report(pending.report, true, code);
        }
        return result;
    } __finally {
        _InterlockedDecrement(&friend_delete_reply_callbacks);
    }
}

static DWORD friend_delete_prepare_hook() {
    if (friend_delete_hook_created) return ERROR_SUCCESS;
    MH_STATUS status = MH_Initialize();
    if (status != MH_OK && status != MH_ERROR_ALREADY_INITIALIZED) return ERROR_NOT_READY;
    auto target = reinterpret_cast<void*>(core_address(core_base, 0x19db290));
    status = MH_CreateHook(target, reinterpret_cast<void*>(&friend_delete_exchange),
                           reinterpret_cast<void**>(&original_oplog_exchange));
    if (status != MH_OK) return ERROR_NOT_READY;
    status = MH_EnableHook(target);
    if (status != MH_OK) {
        MH_RemoveHook(target);
        return ERROR_NOT_READY;
    }
    friend_delete_hook_created = true;
    return ERROR_SUCCESS;
}

static void friend_delete_task(const FriendDeleteLease& lease) {
    const auto& config = lease.config;
    SharedMessage context{};
    SmallString target = friend_borrow_string(config.base.target);
    TestResult report{};
    uint64_t generation = 0;
    bool dispatched = false;
    _InterlockedIncrement(&active_callbacks);
    __try {
        __try {
            using GetContext = SharedMessage* (__fastcall*)(SharedMessage*);
            reinterpret_cast<GetContext>(core_address(core_base, 0x47220))(&context);
            if (!emoticon_account_matches(context, config.account)) __leave;
            AcquireSRWLockExclusive(&friend_delete_lock);
            const auto now = GetTickCount64();
            if (friend_delete_pending.expires <= now && lease.deadline > now) {
                generation = ++friend_delete_pending.generation;
                friend_delete_pending.expires = lease.deadline;
                std::memcpy(friend_delete_pending.wxid, config.base.target, sizeof(config.base.target));
                std::memcpy(friend_delete_pending.report, config.base.base.log_path,
                            sizeof(config.base.base.log_path));
            }
            ReleaseSRWLockExclusive(&friend_delete_lock);
            if (!generation) __leave;
            using DeleteContact = void (__fastcall*)(void*, const SmallString*, uint32_t);
            // 0 skips the chat-history removal task. Never expose this flag to callers.
            reinterpret_cast<DeleteContact>(core_address(core_base, 0x710470))(nullptr, &target, 0);
            dispatched = true;
        } __except (record_exception(GetExceptionInformation(), &report)) {
        }
        guarded_release_shared(&context, &report);
        if (!dispatched) {
            AcquireSRWLockExclusive(&friend_delete_lock);
            const bool live = generation && friend_delete_pending.generation == generation &&
                friend_delete_pending.expires;
            if (live) friend_delete_pending.expires = 0;
            ReleaseSRWLockExclusive(&friend_delete_lock);
            if (!generation || live) friend_delete_report(config.base.base.log_path, false, 0);
        }
    } __finally {
        _InterlockedDecrement(&active_callbacks);
    }
}

static DWORD friend_delete_copy(void* parameter, FriendDeleteConfig& config) {
    __try {
        config = *static_cast<const FriendDeleteConfig*>(parameter);
        return ERROR_SUCCESS;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return ERROR_INVALID_PARAMETER;
    }
}

static DWORD friend_delete_dispatch(void* parameter) {
    if (!parameter || live_tasks || text_reply_pending()) return ERROR_BUSY;
    FriendDeleteConfig config{};
    if (friend_delete_copy(parameter, config) != ERROR_SUCCESS) return ERROR_INVALID_PARAMETER;
    if (config.base.base.size != sizeof(config) || config.base.base.pid != GetCurrentProcessId() ||
        config.reserved[0] || config.reserved[1] ||
        strnlen_s(config.account, sizeof(config.account)) >= sizeof(config.account) ||
        strnlen_s(config.base.target, sizeof(config.base.target)) >= sizeof(config.base.target) ||
        !friend_valid_account(config.account) || !friend_valid_account(config.base.target) ||
        !std::strcmp(config.account, config.base.target) || !config.base.base.log_path[0] ||
        wcsnlen_s(config.base.base.log_path, MAX_PATH) >= MAX_PATH) return ERROR_INVALID_PARAMETER;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core) || _InterlockedCompareExchange(&core_profile_id, 0, 0) != 4)
        return ERROR_NOT_SUPPORTED;
    core_base = reinterpret_cast<uintptr_t>(core);
    const DWORD observed = friend_delete_prepare_hook();
    if (observed != ERROR_SUCCESS) return observed;
    DWORD status = ERROR_GEN_FAILURE;
    try {
        const auto lease = std::make_shared<FriendDeleteLease>(config);
        std::function<void()> task = [lease]() { friend_delete_task(*lease); };
        status = schedule_task(config.base.base, &task);
    } catch (...) {
    }
    SecureZeroMemory(&config, sizeof(config));
    return status;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxDeleteFriend(void* parameter) {
    _InterlockedIncrement(&active_callbacks);
    bool admitted = false;
    __try {
        if (_InterlockedCompareExchange(&friend_delete_admission, 1, 0) != 0) return ERROR_BUSY;
        admitted = true;
        return friend_delete_dispatch(parameter);
    } __finally {
        if (admitted) _InterlockedExchange(&friend_delete_admission, 0);
        _InterlockedDecrement(&active_callbacks);
    }
}
