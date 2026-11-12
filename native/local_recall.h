#pragma once
#include "local_recall_policy.h"
// 4.2.0.12 的本地撤回更新。服务器撤回请求在进入此处前已完成。
struct LocalRecallConfig {
    uint32_t size, pid, enabled, reserved;
    char account[128];
};
static_assert(sizeof(LocalRecallConfig) == 144);

using LocalRecallFunction = bool (__fastcall*)(void*, const unsigned char*);
using LocalRecallStoreFunction = void* (__fastcall*)(void*, unsigned char*, unsigned char*);
using LocalRecallDeleteFunction = uint32_t (__fastcall*)(void*, const unsigned char*, uint8_t);
static LocalRecallFunction original_local_recall = nullptr;
static LocalRecallStoreFunction original_local_recall_insert = nullptr;
static LocalRecallStoreFunction original_local_recall_replace = nullptr;
static LocalRecallDeleteFunction original_local_recall_delete = nullptr;
static SRWLOCK local_recall_lock = SRWLOCK_INIT;
static char local_recall_account[128]{};

struct LocalRecallScope {
    uint64_t original_id;
    unsigned char notice[0x2e8];
    bool own;
    bool notice_ready;
};
static_assert(offsetof(LocalRecallScope, notice) % alignof(uint64_t) == 0);
static thread_local LocalRecallScope* local_recall_scope = nullptr;
static volatile LONG local_recall_seen = 0, local_recall_preserved = 0;
static volatile LONG local_recall_prepared = 0, local_recall_written = 0, local_recall_failed = 0;

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

static bool keep_local_recall(const unsigned char* message, bool own_recall, LocalRecallScope* scope) {
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
            scope->original_id = *reinterpret_cast<const uint64_t*>(source + 0x248);
            scope->own = own_recall;
            _InterlockedIncrement(&local_recall_seen);
            keep = true;
        } __except (EXCEPTION_EXECUTE_HANDLER) { keep = false; }
    } __finally { ReleaseSRWLockShared(&local_recall_lock); }
    return keep;
}

static uint32_t __fastcall local_recall_delete(void* manager, const unsigned char* record, uint8_t flag) {
    _InterlockedIncrement(&active_callbacks);
    __try {
        bool preserve = false;
        __try {
            preserve = local_recall_scope && local_recall_matches_record(record, 0x2e8,
                core_address(core_base, 0x8d9f958), local_recall_scope->original_id);
        } __except (EXCEPTION_EXECUTE_HANDLER) { preserve = false; }
        if (preserve) {
            _InterlockedIncrement(&local_recall_preserved);
            return 0;
        }
        return original_local_recall_delete(manager, record, flag);
    } __finally { _InterlockedDecrement(&active_callbacks); }
}

static void* local_recall_store(LocalRecallStoreFunction original, void* service,
                               unsigned char* output, unsigned char* record) {
    _InterlockedIncrement(&active_callbacks);
    __try {
        bool detached = false;
        uint32_t old_local_id = 0;
        __try {
            if (local_recall_scope && record) {
                old_local_id = *reinterpret_cast<const uint32_t*>(record + 0x144);
                detached = local_recall_detach_notice(record, 0x2e8,
                    core_address(core_base, 0x8d9f958), local_recall_scope->original_id);
            }
        } __except (EXCEPTION_EXECUTE_HANDLER) { }
        if (detached) _InterlockedIncrement(&local_recall_prepared);
        // A preserved original must get a separate inserted notice, rather than
        // following the revoke replacement path with cleared record IDs.
        // Both wrappers return a RawMessage (0x2e8), not an optional RawMessage.
        auto store = detached ? original_local_recall_insert : original;
        void* result = store(service, output, record);
        if (detached) {
            bool written = false;
            __try {
                written = local_recall_notice_written(output, 0x2e8,
                    core_address(core_base, 0x8d9f958), old_local_id);
                if (written && local_recall_scope->own && !local_recall_scope->notice_ready) {
                    using CopyRecord = void* (__fastcall*)(void*, const void*);
                    reinterpret_cast<CopyRecord>(core_address(core_base, 0x1cd330))(
                        local_recall_scope->notice, output);
                    local_recall_scope->notice_ready = true;
                }
            } __except (EXCEPTION_EXECUTE_HANDLER) { }
            _InterlockedIncrement(written ? &local_recall_written : &local_recall_failed);
        }
        return result;
    } __finally { _InterlockedDecrement(&active_callbacks); }
}

static void* __fastcall local_recall_insert(void* service, unsigned char* output, unsigned char* record) {
    return local_recall_store(original_local_recall_insert, service, output, record);
}

static void* __fastcall local_recall_replace(void* service, unsigned char* output, unsigned char* record) {
    return local_recall_store(original_local_recall_replace, service, output, record);
}

static bool __fastcall local_recall(void* manager, const unsigned char* message) {
    _InterlockedIncrement(&active_callbacks);
    __try {
        const bool own_recall =
            reinterpret_cast<uintptr_t>(_ReturnAddress()) == core_address(core_base, 0x2425b51);
        LocalRecallScope scope{};
        if (keep_local_recall(message, own_recall, &scope)) {
            LocalRecallScope* previous = local_recall_scope;
            local_recall_scope = &scope;
            __try {
                // Run WeChat's own notice formatter and local UI/store updates.
                // Nested hooks preserve the old record and detach notice IDs
                // before WeChat saves it as a distinct local system message.
                const bool result = original_local_recall(manager, message);
                if (scope.notice_ready) {
                    // 1853830 -> 182cab0 emits the added-message vector at +0xe8.
                    // The single-record signal at +0x2e8 is a removal/update
                    // path and must not be used for this newly inserted row.
                    // The signal copies our vector before returning; no send.
                    __try {
                        using NotifyAdded = void (__fastcall*)(void*, const void*);
                        const unsigned char* batch[3]{scope.notice,
                            scope.notice + sizeof(scope.notice), scope.notice + sizeof(scope.notice)};
                        reinterpret_cast<NotifyAdded>(core_address(core_base, 0x17f8020))(
                            static_cast<unsigned char*>(manager) + 0xe8, batch);
                    } __except (EXCEPTION_EXECUTE_HANDLER) { }
                }
                return result;
            } __finally {
                local_recall_scope = previous;
                if (scope.notice_ready) {
                    __try {
                        using DestroyRecord = void (__fastcall*)(void*);
                        reinterpret_cast<DestroyRecord>(core_address(core_base, 0x1ce140))(scope.notice);
                    } __except (EXCEPTION_EXECUTE_HANDLER) { }
                }
            }
        }
        return original_local_recall(manager, message);
    } __finally { _InterlockedDecrement(&active_callbacks); }
}

static DWORD ensure_local_recall_hook(void* target, void* replacement, void** original, bool* created) {
    if (*created) return ERROR_SUCCESS;
    if (!target) return ERROR_NOT_SUPPORTED;
    MH_STATUS hook = MH_CreateHook(target, replacement, original);
    if (hook != MH_OK) return 600 + hook;
    hook = MH_EnableHook(target);
    if (hook != MH_OK) {
        MH_RemoveHook(target);
        return 600 + hook;
    }
    *created = true;
    return ERROR_SUCCESS;
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
                status = ensure_local_recall_hook(reinterpret_cast<void*>(core_address(core_base, 0x18121d0)),
                    reinterpret_cast<void*>(&local_recall_delete),
                    reinterpret_cast<void**>(&original_local_recall_delete), &local_recall_delete_hook_created);
                if (status) return status;
                status = ensure_local_recall_hook(reinterpret_cast<void*>(core_address(core_base, 0x18536a0)),
                    reinterpret_cast<void*>(&local_recall_insert),
                    reinterpret_cast<void**>(&original_local_recall_insert), &local_recall_insert_hook_created);
                if (status) return status;
                status = ensure_local_recall_hook(reinterpret_cast<void*>(core_address(core_base, 0x1853740)),
                    reinterpret_cast<void*>(&local_recall_replace),
                    reinterpret_cast<void**>(&original_local_recall_replace), &local_recall_replace_hook_created);
                if (status) return status;
                status = ensure_local_recall_hook(reinterpret_cast<void*>(core_address(core_base, 0x1852d70)),
                    reinterpret_cast<void*>(&local_recall),
                    reinterpret_cast<void**>(&original_local_recall), &local_recall_hook_created);
                if (status) return status;
                strcpy_s(local_recall_account, config.account);
                InterlockedExchange(&local_recall_enabled, 1);
            } __finally { ReleaseSRWLockExclusive(&local_recall_lock); }
        } __except (EXCEPTION_EXECUTE_HANDLER) { status = ERROR_INVALID_DATA; }
    } __finally { _InterlockedDecrement(&active_callbacks); }
    return status;
}

struct LocalRecallStatistics {
    uint32_t size, enabled, seen, preserved, prepared, written, failed;
};
static_assert(sizeof(LocalRecallStatistics) == 28);

extern "C" __declspec(dllexport) DWORD WINAPI WxGetLocalRecallStatistics(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    _InterlockedIncrement(&active_callbacks);
    __try {
        __try {
            auto& result = *static_cast<LocalRecallStatistics*>(parameter);
            if (result.size != sizeof(result)) return ERROR_INVALID_PARAMETER;
            result.enabled = InterlockedCompareExchange(&local_recall_enabled, 0, 0);
            result.seen = InterlockedCompareExchange(&local_recall_seen, 0, 0);
            result.preserved = InterlockedCompareExchange(&local_recall_preserved, 0, 0);
            result.prepared = InterlockedCompareExchange(&local_recall_prepared, 0, 0);
            result.written = InterlockedCompareExchange(&local_recall_written, 0, 0);
            result.failed = InterlockedCompareExchange(&local_recall_failed, 0, 0);
            return ERROR_SUCCESS;
        } __except (EXCEPTION_EXECUTE_HANDLER) { return ERROR_INVALID_DATA; }
    } __finally { _InterlockedDecrement(&active_callbacks); }
}
