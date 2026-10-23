#pragma once

// Included by text_bridge.cpp after its common account/queue helpers.
// friend_receive_hook_created is owned by selftest_bridge.cpp so the shared
// WxRemoveSelfTest path can disable this hook before waiting for callbacks.
// Static live path: 3866460 -> 3866510(message, history=0).
// Static cached/history path: 3871980 -> 3866510(message, history=1).
using FriendReceiveFunction = uintptr_t (__fastcall*)(void*, const unsigned char*, uint8_t);
static FriendReceiveFunction original_friend_receive = nullptr;

static bool friend_receive_copy_string(const unsigned char* object, size_t offset,
                                       char* output, size_t capacity) {
    const auto value = reinterpret_cast<const SmallString*>(object + offset);
    if (!value->length || value->length >= capacity || value->capacity < value->length ||
        !(value->capacity == 15 || (value->capacity >= 16 && value->capacity <= 1024 * 1024)))
        return false;
    const char* bytes = value->capacity == 15 ? value->data :
        *reinterpret_cast<const char* const*>(value->data);
    if (!bytes) return false;
    std::memcpy(output, bytes, static_cast<size_t>(value->length));
    output[value->length] = 0;
    return true;
}

static void sample_friend_request(const unsigned char* message, uint8_t history) {
    char xml[16384]{};
    __try {
      __try {
        // Historical initialization must not turn old pending records into
        // automatic acceptance attempts. A newly received live event is needed.
        if (history || !message ||
            *reinterpret_cast<const uintptr_t*>(message) != core_address(core_base, 0x8d9f958) ||
            *reinterpret_cast<const uint32_t*>(message + 0xc) != 37) return;
        char sender[128]{}, receiver[128]{};
        if (!friend_receive_copy_string(message, 0x18, sender, sizeof(sender)) ||
            !valid_target(sender) ||
            !friend_receive_copy_string(message, 0x38, receiver, sizeof(receiver)) ||
            !friend_valid_account(receiver) ||
            !friend_receive_copy_string(message, 0x180, xml, sizeof(xml)) ||
            !std::strstr(xml, "<msg") || !std::strstr(xml, "encryptusername=") ||
            !std::strstr(xml, "ticket=")) return;
        const uintptr_t root = *reinterpret_cast<const uintptr_t*>(core_address(core_base, 0xb912690));
        if (!root || *reinterpret_cast<const uintptr_t*>(root) != core_address(core_base, 0x8d804e8)) return;
        const void* context = *reinterpret_cast<void* const*>(root + 0x68);
        if (!context || *static_cast<const uintptr_t*>(context) != core_address(core_base, 0x8dfe2f8)) return;
        char account[128]{};
        if (!friend_receive_copy_string(static_cast<const unsigned char*>(context), 0x48,
                                        account, sizeof(account)) ||
            std::strcmp(receiver, account) ||
            *reinterpret_cast<const uintptr_t*>(core_address(core_base, 0xb912690)) != root ||
            *reinterpret_cast<void* const*>(root + 0x68) != context) return;
        // This separate friend-verification channel does not necessarily enter
        // ordinary AddMsg. Normalize it to the existing V3 type-37 queue only;
        // credentials and XML are never sent to a log file or native report.
        enqueue_received("fmessage", receiver, "", nullptr, 37, xml);
        _InterlockedIncrement64(&receive_statistics.messages);
        _InterlockedExchange64(&receive_statistics.last_type, 37);
      } __except (EXCEPTION_EXECUTE_HANDLER) {
        // Layout mismatch must not affect the native client's own handler.
      }
    } __finally { SecureZeroMemory(xml, sizeof(xml)); }
}

static uintptr_t __fastcall friend_receive(void* manager, const unsigned char* message,
                                         uint8_t history) {
    _InterlockedIncrement(&active_callbacks);
    uintptr_t result = 0;
    __try {
        sample_friend_request(message, history);
        result = original_friend_receive(manager, message, history);
    } __finally { _InterlockedDecrement(&active_callbacks); }
    return result;
}

static DWORD friend_receive_start() {
    if (friend_receive_hook_created) return ERROR_SUCCESS;
    void* target = reinterpret_cast<void*>(core_address(core_base, 0x3866510));
    MH_STATUS status = MH_CreateHook(target, reinterpret_cast<void*>(&friend_receive),
                                     reinterpret_cast<void**>(&original_friend_receive));
    if (status != MH_OK) return 600 + status;
    status = MH_EnableHook(target);
    if (status != MH_OK) {
        MH_RemoveHook(target);
        return 600 + status;
    }
    friend_receive_hook_created = true;
    return ERROR_SUCCESS;
}
