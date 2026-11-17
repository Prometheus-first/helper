#pragma once

struct FriendRelationConfig {
    TextConfig base;
    char account[128];
};
static_assert(sizeof(FriendRelationConfig) == 1816);

struct FriendRelationLease {
    FriendRelationConfig config;
    explicit FriendRelationLease(const FriendRelationConfig& value) : config(value) {
        _InterlockedIncrement(&live_tasks);
    }
    ~FriendRelationLease() {
        SecureZeroMemory(&config, sizeof(config));
        _InterlockedDecrement(&live_tasks);
    }
};

static void friend_relation_task(const FriendRelationConfig& config) {
    SharedMessage context{}, provider{}, manager{};
    SmallString peer = friend_borrow_string(config.base.target), empty{};
    empty.capacity = 15;
    alignas(8) unsigned char output[0x70]{};
    *reinterpret_cast<uint64_t*>(output + 0x20) = 15;
    *reinterpret_cast<uint64_t*>(output + 0x48) = 15;
    *reinterpret_cast<uint64_t*>(output + 0x68) = 15;
    TestResult report{};
    bool invoked = false, complete = false, accepted = false;
    int32_t code = 0;
    _InterlockedIncrement(&active_callbacks);
    __try {
        __try {
            using GetContext = SharedMessage* (__fastcall*)(SharedMessage*);
            using GetService = SharedMessage* (__fastcall*)(void*, SharedMessage*);
            reinterpret_cast<GetContext>(core_address(core_base, 0x47220))(&context);
            if (!emoticon_account_matches(context, config.account)) __leave;
            reinterpret_cast<GetService>(core_address(core_base, 0x36c740))(context.object, &provider);
            if (!provider.object || !provider.control) __leave;
            reinterpret_cast<GetService>(core_address(core_base, 0x754330))(provider.object, &manager);
            if (!manager.object || !manager.control || !emoticon_account_matches(context, config.account)) __leave;
            using Verify = void* (__fastcall*)(void*, void*, const SmallString*, uint32_t, uint32_t,
                                              uint32_t, const SmallString*, const SmallString*, const SmallString*);
            invoked = true;
            // 4.2.0.12 VerifyUser opcode 1. Successful requests can generate
            // an add-contact notice and update the contact. Never chain opcode 2.
            auto result = reinterpret_cast<Verify>(core_address(core_base, 0x1a5a850))(
                manager.object, output, &peer, 1, 6, 0, &empty, &empty, &empty);
            complete = result == output && emoticon_account_matches(context, config.account);
            accepted = output[0] == 1;
            code = *reinterpret_cast<const int32_t*>(output + 4);
        } __except (record_exception(GetExceptionInformation(), &report)) { complete = false; }
        if (invoked) {
            __try {
                using Destroy = void (__fastcall*)(void*);
                reinterpret_cast<Destroy>(core_address(core_base, 0x7544b0))(output);
            } __except (record_exception(GetExceptionInformation(), &report)) { complete = false; }
        }
        guarded_release_shared(&manager, &report);
        guarded_release_shared(&provider, &report);
        guarded_release_shared(&context, &report);
        FILE* file = nullptr;
        if (_wfopen_s(&file, config.base.base.log_path, L"wb") == 0 && file) {
            std::fprintf(file, "{\"operation\":\"friend_relation\",\"complete\":%s,"
                         "\"native_success\":%s,\"native_code\":%d,\"exception\":%lu}\n",
                         complete && !report.exception ? "true" : "false",
                         accepted ? "true" : "false", code, report.exception);
            std::fclose(file);
        }
        SecureZeroMemory(output, sizeof(output));
    } __finally { _InterlockedDecrement(&active_callbacks); }
}

static DWORD schedule_friend_relation(const FriendRelationConfig& config) {
    try {
        auto lease = std::make_shared<FriendRelationLease>(config);
        std::function<void()> task = [lease]() { friend_relation_task(lease->config); };
        return schedule_task(config.base.base, &task);
    } catch (...) { return ERROR_GEN_FAILURE; }
}

extern "C" __declspec(dllexport) DWORD WINAPI WxCheckFriendRelation(void* parameter) {
    _InterlockedIncrement(&active_callbacks);
    __try {
        if (!parameter || live_tasks || text_reply_pending()) return ERROR_BUSY;
        FriendRelationConfig config{};
        __try { config = *static_cast<const FriendRelationConfig*>(parameter); }
        __except (EXCEPTION_EXECUTE_HANDLER) { return ERROR_INVALID_PARAMETER; }
        const auto length = wcsnlen_s(config.base.base.log_path, MAX_PATH);
        if (config.base.base.size != sizeof(config) || config.base.base.pid != GetCurrentProcessId() ||
                config.base.base.manager || config.base.base.text[0] || !length || length >= MAX_PATH ||
                !friend_valid_account(config.account) || !friend_valid_account(config.base.target) ||
                !std::strcmp(config.account, config.base.target))
            return ERROR_INVALID_PARAMETER;
        HMODULE core = GetModuleHandleW(L"Weixin.dll");
        if (!core || !core_fingerprint(core) || core_profile_id != 4) return ERROR_NOT_SUPPORTED;
        core_base = reinterpret_cast<uintptr_t>(core);
        return schedule_friend_relation(config);
    } __finally { _InterlockedDecrement(&active_callbacks); }
}
