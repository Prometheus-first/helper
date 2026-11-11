#pragma once
// 4.2.0.12 的本地撤回更新。服务器撤回请求在进入此处前已完成。
struct LocalRecallConfig {
    uint32_t size, pid, enabled, reserved;
    char account[128];
};
static_assert(sizeof(LocalRecallConfig) == 144);

using LocalRecallFunction = bool (__fastcall*)(void*, const unsigned char*);
static LocalRecallFunction original_local_recall = nullptr;
static SRWLOCK local_recall_lock = SRWLOCK_INIT;
static char local_recall_account[128]{};

static bool local_recall_account_matches(const char* expected) {
    const auto root = *reinterpret_cast<const uintptr_t*>(core_address(core_base, 0xb912690));
    if (!root || *reinterpret_cast<const uintptr_t*>(root) != core_address(core_base, 0x8d804e8))
        return false;
    const auto context = *reinterpret_cast<const unsigned char* const*>(root + 0x68);
    if (!context || *reinterpret_cast<const uintptr_t*>(context) != core_address(core_base, 0x8dfe2f8))
        return false;
    return quote_string_equals(*reinterpret_cast<const SmallString*>(context + 0x48), expected) &&
        *reinterpret_cast<const uintptr_t*>(core_address(core_base, 0xb912690)) == root &&
        *reinterpret_cast<const unsigned char* const*>(root + 0x68) == context;
}

static bool keep_local_recall(const unsigned char* message, bool own_recall) {
    bool keep = false;
    AcquireSRWLockShared(&local_recall_lock);
    __try {
        __try {
            if (!local_recall_enabled || !message ||
                    *reinterpret_cast<const uintptr_t*>(message) != core_address(core_base, 0x8d9f958) ||
                    !local_recall_account_matches(local_recall_account))
                return false;
            const auto source = *reinterpret_cast<const unsigned char* const*>(message + 0x288);
            if (!source || *reinterpret_cast<const uintptr_t*>(source) != core_address(core_base, 0x91aa8b8) ||
                    !*reinterpret_cast<const uint64_t*>(source + 0x248))
                return false;
            const auto& kind = *reinterpret_cast<const SmallString*>(source + 0x220);
            // 本机撤回成功回调只填写会话和编号，type 保持默认空串。
            if (!quote_string_equals(kind, "revokemsg") &&
                    !(own_recall && kind.length == 0 && kind.capacity == 15))
                return false;
            const auto& session = *reinterpret_cast<const SmallString*>(source + 0x270);
            if (!session.length || session.length >= 128 || session.capacity < session.length ||
                    session.capacity > 1024 * 1024)
                return false;
            const char* bytes = session.capacity < 16 ? session.data : *reinterpret_cast<char* const*>(session.data);
            char target[128]{};
            std::memcpy(target, bytes, static_cast<size_t>(session.length));
            if (!valid_target(target)) return false;
            // 自己在本机撤回时可能不再经过同步 XML 解析，补充同一真实撤回事件。
            // 队列消费者按会话及原编号去重，普通同步路径不会重复补发。
            char notice[512]{};
            sprintf_s(notice,
                "<sysmsg type=\"revokemsg\"><revokemsg><session>%s</session>"
                "<newmsgid>%llu</newmsgid></revokemsg></sysmsg>",
                target, *reinterpret_cast<const uint64_t*>(source + 0x248));
            enqueue_received("weixin", "weixin", "", nullptr, 10002, notice, 2);
            keep = true;
        } __except (EXCEPTION_EXECUTE_HANDLER) { keep = false; }
    } __finally { ReleaseSRWLockShared(&local_recall_lock); }
    return keep;
}

static bool __fastcall local_recall(void* manager, const unsigned char* message) {
    _InterlockedIncrement(&active_callbacks);
    __try {
        const bool own_recall =
            reinterpret_cast<uintptr_t>(_ReturnAddress()) == core_address(core_base, 0x2425b51);
        if (keep_local_recall(message, own_recall)) return true;
        return original_local_recall(manager, message);
    } __finally { _InterlockedDecrement(&active_callbacks); }
}

extern "C" __declspec(dllexport) DWORD WINAPI WxSetLocalRecall(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    _InterlockedIncrement(&active_callbacks);
    DWORD status = ERROR_SUCCESS;
    __try {
        __try {
            const auto& config = *static_cast<const LocalRecallConfig*>(parameter);
            if (config.size != sizeof(config) || config.pid != GetCurrentProcessId() ||
                    config.enabled > 1 || config.reserved || !valid_target(config.account) ||
                    std::strchr(config.account, '@') || !std::strcmp(config.account, "filehelper"))
                return ERROR_INVALID_PARAMETER;
            if (core_profile_id != 4 || !core_base)
                return ERROR_NOT_SUPPORTED;
            AcquireSRWLockExclusive(&local_recall_lock);
            __try {
                if (!config.enabled) {
                    InterlockedExchange(&local_recall_enabled, 0);
                    return ERROR_SUCCESS;
                }
                if (!local_recall_account_matches(config.account)) return ERROR_NOT_FOUND;
                if (!local_recall_hook_created) {
                    void* target = reinterpret_cast<void*>(core_address(core_base, 0x1852d70));
                    if (!target) return ERROR_NOT_SUPPORTED;
                    MH_STATUS hook = MH_CreateHook(target, reinterpret_cast<void*>(&local_recall),
                                                    reinterpret_cast<void**>(&original_local_recall));
                    if (hook != MH_OK) return 600 + hook;
                    hook = MH_EnableHook(target);
                    if (hook != MH_OK) {
                        MH_RemoveHook(target);
                        return 600 + hook;
                    }
                    local_recall_hook_created = true;
                }
                strcpy_s(local_recall_account, config.account);
                InterlockedExchange(&local_recall_enabled, 1);
            } __finally { ReleaseSRWLockExclusive(&local_recall_lock); }
        } __except (EXCEPTION_EXECUTE_HANDLER) { status = ERROR_INVALID_DATA; }
    } __finally { _InterlockedDecrement(&active_callbacks); }
    return status;
}
