#pragma once
// 微信 4.2.0.12。通过微信的消息索引取原消息，让原生发送器生成引用卡片。
struct QuoteTextConfig {
    TextConfig base;
    uint64_t original_id;
    char account[128];
};
static_assert(sizeof(QuoteTextConfig) == 1824 && offsetof(QuoteTextConfig, account) == 1696);

using QuoteGetService = SharedMessage* (__fastcall*)(void*, SharedMessage*);
static QuoteGetService original_quote_manager = nullptr;
static volatile LONG quote_admission = 0;

static SharedMessage* __fastcall quote_manager(void* provider, SharedMessage* output) {
    _InterlockedIncrement(&active_callbacks);
    __try {
        const auto context = text_factory_context();
        if (context && context->quote &&
                reinterpret_cast<uintptr_t>(_ReturnAddress()) == core_address(core_base, 0x5721535)) {
            auto& quote = *context->quote;
            // 文本构造器写入类型 1，再在提交前恢复引用消息的类型和子类型。
            if (quote.prepared.object || !quote.object || !quote.completion_quoted ||
                    *reinterpret_cast<const uint64_t*>(quote.object + 0x118) != ((57ull << 32) | 1))
                RaiseException(ERROR_INVALID_DATA, 0, 0, nullptr);
            *reinterpret_cast<uint64_t*>(quote.object + 0x118) = native_quote_type;
            *quote.completion_quoted = true;
            quote.applied = true;
        }
        return original_quote_manager(provider, output);
    } __finally { _InterlockedDecrement(&active_callbacks); }
}

static DWORD ensure_quote_hooks() {
    DWORD status = ensure_text_factory();
    if (status || quote_manager_hook_created) return status;
    auto target = reinterpret_cast<void*>(core_address(core_base, 0x6f4d90));
    if (!target) return ERROR_NOT_SUPPORTED;
    MH_STATUS hook = MH_CreateHook(target, reinterpret_cast<void*>(&quote_manager),
                                  reinterpret_cast<void**>(&original_quote_manager));
    if (hook != MH_OK) return 600 + hook;
    hook = MH_EnableHook(target);
    if (hook != MH_OK) {
        MH_RemoveHook(target);
        return 600 + hook;
    }
    quote_manager_hook_created = true;
    return ERROR_SUCCESS;
}

static bool quote_string_equals(const SmallString& value, const char* expected) {
    size_t length = std::strlen(expected);
    if (value.length != length || value.capacity < length) return false;
    const char* bytes = value.capacity < 16 ? value.data : *reinterpret_cast<char* const*>(value.data);
    return bytes && std::memcmp(bytes, expected, length) == 0;
}

static bool quote_on_task(const QuoteTextConfig& config, TestResult* report) {
    alignas(16) unsigned char content[0x2f0]{}, source[0x440]{}, extra[0x68]{};
    SharedMessage app{}, provider{}, manager{};
    SmallString session{}, reply{};
    session.capacity = reply.capacity = 15;
    QuoteScope quote{};
    bool content_ready = false, source_ready = false, extra_ready = false;
    __try {
        __try {
            using GetContext = SharedMessage* (__fastcall*)(SharedMessage*);
            using Query = void* (__fastcall*)(void*, void*, const SmallString*, uint64_t, uint32_t);
            using Describe = void* (__fastcall*)(const void*, void*);
            using ConstructExtra = void* (__fastcall*)(void*);
            using ConstructQuote = SharedMessage* (__fastcall*)(SharedMessage*, void*, const SmallString*, void*);
            report->stage = 80;
            reinterpret_cast<GetContext>(core_address(core_base, 0x47220))(&app);
            if (!emoticon_account_matches(app, config.account)) return false;
            reinterpret_cast<QuoteGetService>(core_address(core_base, 0x36c740))(app.object, &provider);
            if (!provider.object) return false;
            reinterpret_cast<QuoteGetService>(core_address(core_base, 0x6f4d90))(provider.object, &manager);
            if (!manager.object || !set_native_string(&session, config.base.target)) return false;

            report->stage = 81;
            reinterpret_cast<Query>(core_address(core_base, 0x1804cc0))(manager.object, content, &session, config.original_id, 0);
            content_ready = content[0x2e8] == 1;
            if (!content_ready) {
                // 接收回调可能早于索引入库，调用方可安全重查。
                report->stage = 84;
                return false;
            }
            if (*reinterpret_cast<const uintptr_t*>(content) != core_address(core_base, 0x8d9f958)) return false;
            reinterpret_cast<Describe>(core_address(core_base, 0xaa1910))(content, source);
            source_ready = true;
            if (*reinterpret_cast<const uintptr_t*>(source) != core_address(core_base, 0x9074f18) ||
                    *reinterpret_cast<const uint32_t*>(source + 8) != 1 ||
                    *reinterpret_cast<const uint64_t*>(source + 0xb8) != config.original_id ||
                    !quote_string_equals(*reinterpret_cast<const SmallString*>(source + 0x70), config.base.target))
                return false;

            report->stage = 82;
            if (!set_native_string(&reply, config.base.base.text)) return false;
            reinterpret_cast<ConstructExtra>(core_address(core_base, 0x1aa3a60))(extra);
            extra_ready = true;
            reinterpret_cast<ConstructQuote>(core_address(core_base, 0x3ab6aa0))(&quote.prepared, source, &reply, extra);
            quote.object = static_cast<unsigned char*>(quote.prepared.object);
            if (!quote.object || !quote.prepared.control ||
                    *reinterpret_cast<const uintptr_t*>(quote.object) != core_address(core_base, 0x8e53588) ||
                    *reinterpret_cast<const uint64_t*>(quote.object + 0x118) != native_quote_type ||
                    *reinterpret_cast<const uint64_t*>(quote.object + 0x300) != config.original_id ||
                    !set_native_string(reinterpret_cast<SmallString*>(quote.object + 0xb0), config.base.target) ||
                    !emoticon_account_matches(app, config.account))
                return false;

            report->stage = 83;
            report->native_status = send_text_on_task(&config.base, nullptr, &quote);
            return report->native_status == ERROR_SUCCESS && quote.applied;
        } __except (record_exception(GetExceptionInformation(), report)) {
            return false;
        }
    } __finally {
        using Destroy = void (__fastcall*)(void*);
        guarded_release_shared(&quote.prepared, report);
        if (extra_ready) reinterpret_cast<Destroy>(core_address(core_base, 0xee4c70))(extra);
        if (source_ready) reinterpret_cast<Destroy>(core_address(core_base, 0x1b7ff0))(source);
        if (content_ready) reinterpret_cast<Destroy>(core_address(core_base, 0x1ce140))(content);
        free_native_string(&reply);
        free_native_string(&session);
        guarded_release_shared(&manager, report);
        guarded_release_shared(&provider, report);
        guarded_release_shared(&app, report);
    }
}

struct QuoteTaskLease {
    QuoteTextConfig config;
    explicit QuoteTaskLease(const QuoteTextConfig& value) : config(value) {
        _InterlockedIncrement(&live_tasks);
    }
    ~QuoteTaskLease() { _InterlockedDecrement(&live_tasks); }
};

static DWORD dispatch_quote(const QuoteTextConfig& config) {
    const auto& base = config.base.base;
    const size_t length = strnlen_s(base.text, sizeof(base.text));
    const size_t log_length = wcsnlen_s(base.log_path, std::size(base.log_path));
    if (base.size != sizeof(config) || base.pid != GetCurrentProcessId() || !config.original_id ||
            !valid_target(config.base.target) || !valid_target(config.account) ||
            std::strchr(config.account, '@') || !std::strcmp(config.account, "filehelper") ||
            !log_length || log_length >= std::size(base.log_path) ||
            !length || length >= sizeof(base.text) ||
            !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, base.text, static_cast<int>(length), nullptr, 0))
        return ERROR_INVALID_PARAMETER;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core) || core_profile_id != 4) return ERROR_NOT_SUPPORTED;
    core_base = reinterpret_cast<uintptr_t>(core);
    DWORD status = ensure_quote_hooks();
    if (status) return status;
    try {
        auto lease = std::make_shared<QuoteTaskLease>(config);
        std::function<void()> task = [lease]() {
            _InterlockedIncrement(&active_callbacks);
            TestResult report{};
            __try {
                if (!quote_on_task(lease->config, &report) || report.exception)
                    write_report(lease->config.base.base, report, "quote_send_failed");
            } __finally { _InterlockedDecrement(&active_callbacks); }
        };
        return schedule_task(base, &task);
    } catch (const std::bad_alloc&) { return ERROR_NOT_ENOUGH_MEMORY; }
    catch (...) { return ERROR_GEN_FAILURE; }
}

extern "C" __declspec(dllexport) DWORD WINAPI WxSendQuoteText(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    _InterlockedIncrement(&active_callbacks);
    bool admitted = false;
    __try {
        if (_InterlockedCompareExchange(&quote_admission, 1, 0) != 0) return ERROR_BUSY;
        admitted = true;
        if (live_tasks || live_completions) return ERROR_BUSY;
        return dispatch_quote(*static_cast<const QuoteTextConfig*>(parameter));
    } __finally {
        if (admitted) _InterlockedExchange(&quote_admission, 0);
        _InterlockedDecrement(&active_callbacks);
    }
}
