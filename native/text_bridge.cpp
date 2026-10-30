// 通用文本桥接。复用已经实测的版本指纹、字符串分配器、任务调度和卸载保护。
// 单独生成 text_bridge.dll，不替换仍在运行的 selftest_bridge.dll。
#define WXBOT_EXTENDED_RECEIVE
#define WXBOT_MEDIA_RECEIVE
#define WXBOT_LARGE_MEDIA_RECEIVE
#define WXBOT_MESSAGE_ID_RECEIVE
#define WXBOT_VOICE_RECEIVE
#define WXBOT_RECALL_RECEIVE
#define WXBOT_FRIEND_RECEIVE
#include "selftest_bridge.cpp"

static DWORD friend_receive_start();

using SystemParseFunction = uintptr_t (__fastcall*)(void*, const SmallString*, void*);
static SystemParseFunction original_system_parse = nullptr;

static void copy_recall_source(const SmallString* source) {
    __try {
        if (!source || source->length == 0 || source->length >= 16384 ||
                source->length > source->capacity) return;
        const char* content = source->capacity > 15 ?
            *reinterpret_cast<const char* const*>(source->data) : source->data;
        char snapshot[16384]{};
        std::memcpy(snapshot, content, source->length);
        if (!std::strstr(snapshot, "revokemsg") || !std::strstr(snapshot, "newmsgid")) return;
        // 2 = 已绑定进程内的系统 XML 入口。会话和编号由 Python 严格解析，不猜成员。
        enqueue_received("weixin", "weixin", "", nullptr, 10002, snapshot, 2);
    } __except (EXCEPTION_EXECUTE_HANDLER) { }
}

static uintptr_t __fastcall system_parse(void* object, const SmallString* source, void* context) {
    _InterlockedIncrement(&active_callbacks);
    uintptr_t result = 0;
    __try {
        copy_recall_source(source);
        result = original_system_parse(object, source, context);
    } __finally { _InterlockedDecrement(&active_callbacks); }
    return result;
}

static DWORD recall_receive_start() {
    void* target = reinterpret_cast<void*>(core_address(core_base, 0x229e790));
    MH_STATUS status = MH_CreateHook(target, reinterpret_cast<void*>(&system_parse),
                                    reinterpret_cast<void**>(&original_system_parse));
    if (status != MH_OK) return 600 + status;
    recall_receive_hook_created = true;
    status = MH_EnableHook(target);
    return status == MH_OK ? 0 : 600 + status;
}

struct TextConfig {
    TestConfig base;
    char target[128];
};
static_assert(sizeof(TextConfig) == 1688, "Unexpected text config ABI");

struct AtTextConfig { TextConfig base; char members[1024]; };
static_assert(sizeof(AtTextConfig) == 2712, "Unexpected at-text config ABI");

struct MentionContext { const char* members; bool applied; };
using MessageFactory = SharedMessage* (__fastcall*)(SharedMessage*);
static MessageFactory original_factory = nullptr;

static SharedMessage* __fastcall mention_factory(SharedMessage* output) {
    _InterlockedIncrement(&active_callbacks);
    SharedMessage* result = nullptr;
    __try {
        result = original_factory(output);
        const auto context = factory_tls == TLS_OUT_OF_INDEXES ? nullptr :
            static_cast<MentionContext*>(TlsGetValue(factory_tls));
        if (context) {
            auto object = output ? static_cast<unsigned char*>(output->object) : nullptr;
            if (!object || *reinterpret_cast<uintptr_t*>(object) != core_address(core_base, 0x8e53588))
                RaiseException(ERROR_INVALID_DATA, 0, 0, nullptr);
            // 实机真正艾特样本：最后两个 std::string 分别为正文 +0x758、成员列表 +0x778。
            auto members = reinterpret_cast<SmallString*>(object + 0x778);
            if (members->length != 0 || members->capacity != 15 ||
                !set_native_string(members, context->members))
                RaiseException(ERROR_INVALID_DATA, 0, 0, nullptr);
            context->applied = true;
        }
    } __finally { _InterlockedDecrement(&active_callbacks); }
    return result;
}

static DWORD ensure_mention_factory() {
    if (factory_hook_created) return 0;
    MH_STATUS status = MH_Initialize();
    const bool initialized_here = status == MH_OK;
    if (status != MH_OK && status != MH_ERROR_ALREADY_INITIALIZED) return 600 + status;
    factory_tls = TlsAlloc();
    if (factory_tls == TLS_OUT_OF_INDEXES) {
        if (initialized_here) MH_Uninitialize();
        return ERROR_NOT_ENOUGH_MEMORY;
    }
    void* target = reinterpret_cast<void*>(core_address(core_base, 0x6f4c70));
    status = MH_CreateHook(target, reinterpret_cast<void*>(&mention_factory), reinterpret_cast<void**>(&original_factory));
    if (status == MH_OK) {
        status = MH_EnableHook(target);
        if (status != MH_OK) MH_RemoveHook(target);
    }
    if (status != MH_OK) {
        TlsFree(factory_tls); factory_tls = TLS_OUT_OF_INDEXES;
        if (initialized_here) MH_Uninitialize();
        return 600 + status;
    }
    factory_hook_created = true;
    return 0;
}

static bool valid_target(const char* target) {
    size_t length = strnlen_s(target, 128);
    if (!length || length >= 128) return false;
    for (size_t index = 0; index < length; ++index) {
        unsigned char ch = target[index];
        if (!((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') ||
              (ch >= '0' && ch <= '9') || ch == '_' || ch == '.' || ch == '@' || ch == '-')) return false;
    }
    return true;
}

struct TextCompletion {
    volatile LONG references;
    uint32_t padding;
    void (__fastcall* invoke)(TextCompletion*, const unsigned char*);
    void (__fastcall* destroy)(TextCompletion*);
    TextConfig config;
    char members[1024];
};

static void __fastcall text_completion_invoke(TextCompletion* self, const unsigned char* result) {
    _InterlockedIncrement(&active_callbacks);
    __try {
        FILE* log = nullptr;
        bool success = result && result[0] == 1;
        if (_wfopen_s(&log, self->config.base.log_path, L"wb") == 0 && log) {
            std::fprintf(log, "{\"stage\":21,\"exception\":0,\"native_status\":%u,\"target\":", success ? 0 : 1);
            write_json_string(log, self->config.target);
            std::fputs(",\"text\":", log);
            write_json_string(log, self->config.base.text);
            std::fputs(",\"at_members\":", log);
            write_json_string(log, self->members);
            std::fprintf(log, ",\"thread_id\":%lu,\"completion_success\":%s}\n",
                         GetCurrentThreadId(), success ? "true" : "false");
            std::fclose(log);
        }
    } __finally { _InterlockedDecrement(&active_callbacks); }
}

static void __fastcall text_completion_destroy(TextCompletion* self) {
    delete self;
    _InterlockedDecrement(&live_completions);
}

static DWORD send_text_on_task(const TextConfig* config, const char* members = nullptr) {
    if (!config || live_completions != 0) return ERROR_BUSY;
    struct TextRequest { SmallString receiver; SmallString body; } request{};
    request.receiver.capacity = request.body.capacity = 15;
    SmallString description{};
    description.capacity = 15;
    if (!set_native_string(&request.receiver, config->target) ||
        !set_native_string(&request.body, config->base.text) ||
        !set_inline(&description, "wxbot.text")) {
        free_native_string(&request.receiver);
        free_native_string(&request.body);
        return ERROR_INVALID_PARAMETER;
    }
    TextCompletion* completion = new TextCompletion{1, 0, text_completion_invoke, text_completion_destroy, *config, {}};
    if (members) strcpy_s(completion->members, members);
    _InterlockedIncrement(&live_completions);
    TestResult report{};
    MentionContext mention{members, false};
    if (members && !TlsSetValue(factory_tls, &mention)) {
        text_completion_destroy(completion);
        free_native_string(&request.receiver); free_native_string(&request.body);
        return ERROR_INVALID_DATA;
    }
    __try {
        using Builder = void (__fastcall*)(void*, TextRequest*, SmallString*, TextCompletion**);
        reinterpret_cast<Builder>(core_address(core_base, 0x54b02f0))(nullptr, &request, &description, &completion);
        report.stage = 20;
    } __except (record_exception(GetExceptionInformation(), &report)) {
        write_report(config->base, report, "WXBOT_TEXT_SEND_EXCEPTION");
    }
    if (members) TlsSetValue(factory_tls, nullptr);
    if (completion) text_completion_destroy(completion);
    // 好友和群 ID 通常超过 SSO 的 15 字节，必须释放两份请求字符串。
    free_native_string(&request.receiver);
    free_native_string(&request.body);
    return report.exception ? 5 : 0;
}

static bool valid_mentions(const char* members) {
    size_t length = strnlen_s(members, 1024);
    if (!length || length >= 1024) return false;
    unsigned int count = 1;
    size_t part = 0;
    for (size_t index = 0; index < length; ++index) {
        unsigned char ch = members[index];
        if (ch == ',') {
            if (!part || part >= 128 || ++count > 8) return false;
            part = 0;
        } else {
            if (!((ch >= 'a' && ch <= 'z') || (ch >= 'A' && ch <= 'Z') ||
                  (ch >= '0' && ch <= '9') || ch == '_' || ch == '.' || ch == '-')) return false;
            ++part;
        }
    }
    return part > 0 && part < 128;
}

struct AtTaskLease {
    AtTextConfig config;
    explicit AtTaskLease(const AtTextConfig& value) : config(value) { _InterlockedIncrement(&live_tasks); }
    ~AtTaskLease() { _InterlockedDecrement(&live_tasks); }
};

extern "C" __declspec(dllexport) DWORD WINAPI WxSendAtText(void* parameter) {
    if (!parameter || live_completions != 0 || live_tasks != 0) return ERROR_BUSY;
    const AtTextConfig config = *static_cast<const AtTextConfig*>(parameter);
    size_t target_length = strnlen_s(config.base.target, sizeof(config.base.target));
    if (config.base.base.size != sizeof(AtTextConfig) || config.base.base.pid != GetCurrentProcessId() ||
        !valid_target(config.base.target) || target_length < 9 ||
        std::strcmp(config.base.target + target_length - 9, "@chatroom") || !valid_mentions(config.members))
        return ERROR_INVALID_PARAMETER;
    size_t length = strnlen_s(config.base.base.text, sizeof(config.base.base.text));
    if (!length || length >= sizeof(config.base.base.text) ||
        !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, config.base.base.text, static_cast<int>(length), nullptr, 0))
        return ERROR_INVALID_PARAMETER;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 4;
    core_base = reinterpret_cast<uintptr_t>(core);
    DWORD status = ensure_mention_factory();
    if (status != 0) return status;
    auto lease = std::make_shared<AtTaskLease>(config);
    std::function<void()> task = [lease]() {
        _InterlockedIncrement(&active_callbacks);
        __try { send_text_on_task(&lease->config.base, lease->config.members); }
        __finally { _InterlockedDecrement(&active_callbacks); }
    };
    status = schedule_task(config.base.base, &task);
    return status;
}

struct TextTaskLease {
    TextConfig config;
    explicit TextTaskLease(const TextConfig& value) : config(value) { _InterlockedIncrement(&live_tasks); }
    ~TextTaskLease() { _InterlockedDecrement(&live_tasks); }
};

extern "C" __declspec(dllexport) DWORD WINAPI WxSendText(void* parameter) {
    if (!parameter || live_completions != 0 || live_tasks != 0) return ERROR_BUSY;
    const TextConfig config = *static_cast<const TextConfig*>(parameter);
    if (config.base.size != sizeof(TextConfig) || config.base.pid != GetCurrentProcessId() ||
        !valid_target(config.target)) return ERROR_INVALID_PARAMETER;
    size_t length = strnlen_s(config.base.text, sizeof(config.base.text));
    if (!length || length >= sizeof(config.base.text) ||
        !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, config.base.text, static_cast<int>(length), nullptr, 0))
        return ERROR_INVALID_PARAMETER;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 4;
    core_base = reinterpret_cast<uintptr_t>(core);
    auto lease = std::make_shared<TextTaskLease>(config);
    std::function<void()> task = [lease]() {
        _InterlockedIncrement(&active_callbacks);
        __try { send_text_on_task(&lease->config); }
        __finally { _InterlockedDecrement(&active_callbacks); }
    };
    return schedule_task(config.base, &task);
}

static bool try_read_source(const unsigned char* message, size_t offset, char* output, size_t capacity) {
    __try {
        // 真正艾特小号样本已核对：msgsource +0x38 是直接 std::string 指针。
        // 与 from/to/content 的 StringValue 包装不同。
        const auto string = *reinterpret_cast<const SmallString* const*>(message + offset);
        if (!string || !string->length || string->length >= capacity || string->capacity < string->length ||
            !(string->capacity == 15 || (string->capacity >= 16 && string->capacity <= 1024 * 1024))) return false;
        const char* bytes = string->capacity > 15 ? *reinterpret_cast<const char* const*>(string->data) : string->data;
        std::memcpy(output, bytes, static_cast<size_t>(string->length));
        output[string->length] = 0;
        return std::strstr(output, "<msgsource") || std::strstr(output, "<atuserlist");
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

struct ReceiveStatistics {
    uint32_t size, reserved;
    LONG64 batches, messages, texts, images, queued_images, invalid_images, last_type;
};
static ReceiveStatistics receive_statistics{};
static_assert(sizeof(ReceiveStatistics) == 64, "Unexpected receive statistics ABI");

// V7 使用原有媒体载荷区携带长文本，避免改变结构大小和每条消息的队列容量。
static constexpr uint32_t RECEIVE_LONG_TEXT = 4;
static constexpr uint32_t RECEIVE_TEXT_UNAVAILABLE = 8;

static void copy_long_text(const unsigned char* message, const char* sender,
                           const char* receiver, const char* source, uint64_t message_id) {
    const auto wrapper = *reinterpret_cast<const unsigned char* const*>(message + 0x20);
    const auto string = wrapper ? *reinterpret_cast<const SmallString* const*>(wrapper + 8) : nullptr;
    if (!string || string->length < 1024 || string->length >= 262144 ||
        string->capacity < string->length || string->capacity > 1024 * 1024) {
        enqueue_received(sender, receiver, "", source, 1, nullptr, RECEIVE_TEXT_UNAVAILABLE, message_id);
        return;
    }
    const size_t length = static_cast<size_t>(string->length);
    char* content = static_cast<char*>(HeapAlloc(GetProcessHeap(), 0, length + 1));
    if (!content) {
        enqueue_received(sender, receiver, "", source, 1, nullptr, RECEIVE_TEXT_UNAVAILABLE, message_id);
        return;
    }
    __try {
        if (read_pb_string(message, 0x20, content, length + 1) && std::strlen(content) == length)
            enqueue_received(sender, receiver, "", source, 1, content, RECEIVE_LONG_TEXT, message_id);
        else
            enqueue_received(sender, receiver, "", source, 1, nullptr, RECEIVE_TEXT_UNAVAILABLE, message_id);
    } __finally { HeapFree(GetProcessHeap(), 0, content); }
}

static void copy_app_message(const unsigned char* message, const char* sender,
                             const char* receiver, const char* source) {
    char* content = static_cast<char*>(HeapAlloc(GetProcessHeap(), HEAP_ZERO_MEMORY, 262144));
    if (!content) return;
    __try {
        if (read_pb_string(message, 0x20, content, 262144)) {
            enqueue_received(sender, receiver, "", source, 49, content, 0,
                             *reinterpret_cast<const uint64_t*>(message + 0x50));
        } else {
            // 仍提供一个可见事件，不将不完整 XML 交给解析器。
            enqueue_received(sender, receiver, "", source, 49, nullptr, 1,
                             *reinterpret_cast<const uint64_t*>(message + 0x50));
        }
    } __finally { HeapFree(GetProcessHeap(), 0, content); }
}

static void copy_voice_message(const unsigned char* message, const char* sender,
                               const char* receiver, const char* source, uint64_t message_id) {
    char metadata[16384]{};
    if (!read_pb_string(message, 0x20, metadata, sizeof(metadata))) metadata[0] = 0;
    const char* payload = nullptr;
    uint32_t length = 0;
    // 4.1.15.56 and 4.2.0.11 copy ImgBuf to voice bytes (+98).
    // +28 is a status DWORD; the optional SKBuiltinBuffer_t pointer is at +30.
    if (InterlockedCompareExchange(&core_profile_id, 0, 0) >= 2) {
        const auto buffer = *reinterpret_cast<const unsigned char* const*>(message + 0x30);
        if (buffer) {
            const uint32_t declared = *reinterpret_cast<const uint32_t*>(buffer + 0x10);
            const auto value = *reinterpret_cast<const SmallString* const*>(buffer + 8);
            if (value && declared > 0 && declared <= 262143 && value->length == declared &&
                value->capacity >= value->length &&
                (value->capacity == 15 || (value->capacity >= 16 && value->capacity <= 1024 * 1024))) {
                payload = value->capacity > 15 ? *reinterpret_cast<const char* const*>(value->data) : value->data;
                if (payload) length = declared;
            }
        }
    }
    // Explicit byte count preserves NULs. The queue owns its copy before the
    // original receive handler can release the AddMsg buffer. No disk I/O here.
    enqueue_received(sender, receiver, "", source, 34, metadata, 0, message_id, payload, length);
}

static void sample_all_text(const uintptr_t* batch, size_t stride) {
    __try {
        _InterlockedIncrement64(&receive_statistics.batches);
        if (!batch || batch[1] < batch[0] || (batch[1] - batch[0]) % stride != 0 ||
            (batch[1] - batch[0]) / stride > 512) return;
        for (uintptr_t address = batch[0]; address < batch[1]; address += stride) {
            const auto message = reinterpret_cast<const unsigned char*>(address);
            const uint32_t type = *reinterpret_cast<const uint32_t*>(message + 0x14);
            _InterlockedIncrement64(&receive_statistics.messages);
            _InterlockedExchange64(&receive_statistics.last_type, type);
            if (type == 1) _InterlockedIncrement64(&receive_statistics.texts);
            if (type == 3) _InterlockedIncrement64(&receive_statistics.images);
            const bool monitor_only = type == 34 || type == 43 || type == 47 || type == 62;
            if (type != 1 && type != 3 && type != 49 && type != 10000 && type != 10002 && !monitor_only) continue;
            char sender[128]{}, receiver[128]{}, text[1024]{};
            if (!read_pb_string(message, 8, sender, sizeof(sender)) || !valid_target(sender) ||
                !read_pb_string(message, 0x18, receiver, sizeof(receiver)) || !valid_target(receiver)) continue;
            // 接收瞬间复制元数据，不持有任何微信消息对象或字符串指针。
            char source[4096]{};
            if (!try_read_source(message, 0x38, source, sizeof(source))) source[0] = 0;
            // Sync AddMsg +0x50 -> 消息对象 +0x148；保留 64 位服务器编号，不能用队列序号代替。
            const uint64_t message_id = *reinterpret_cast<const uint64_t*>(message + 0x50);
            if (type == 34) {
                copy_voice_message(message, sender, receiver, source, message_id);
            } else if (monitor_only) {
                // 只复制已有消息布局的元数据；不下载或解析语音、视频和表情媒体。
                char metadata[16384]{};
                if (!read_pb_string(message, 0x20, metadata, sizeof(metadata))) metadata[0] = 0;
                enqueue_received(sender, receiver, "", source, type, metadata, 0, message_id);
            } else if (type == 49) {
                copy_app_message(message, sender, receiver, source);
            } else if (type == 3 || type == 10000 || type == 10002) {
                char image_xml[16384]{};
                if (read_pb_string(message, 0x20, image_xml, sizeof(image_xml))) {
                    enqueue_received(sender, receiver, "", source, type, image_xml, 0, message_id);
                    if (type == 3) _InterlockedIncrement64(&receive_statistics.queued_images);
                } else {
                    if (type == 3) _InterlockedIncrement64(&receive_statistics.invalid_images);
                }
            } else if (read_pb_string(message, 0x20, text, sizeof(text))) {
                enqueue_received(sender, receiver, text, source, 1, nullptr, 0, message_id);
            } else {
                copy_long_text(message, sender, receiver, source, message_id);
            }
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        // 布局不匹配时放弃采样，真实客户端仍按原逻辑收消息。
    }
}

static uintptr_t __fastcall text_receive(void* manager, const uintptr_t* batch, uint8_t flag1, uint8_t flag2) {
    _InterlockedIncrement(&active_callbacks);
    uintptr_t result = 0;
    __try {
        sample_all_text(batch, 0x78);
        result = original_receive(manager, batch, flag1, flag2);
    } __finally { _InterlockedDecrement(&active_callbacks); }
    return result;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxStartTextReceiver(void* parameter) {
    if (!parameter || hook_created || receive_hook_created) return ERROR_BUSY;
    const TextConfig config = *static_cast<const TextConfig*>(parameter);
    if (config.base.size != sizeof(TextConfig) || config.base.pid != GetCurrentProcessId()) return 3;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 4;
    core_base = reinterpret_cast<uintptr_t>(core);
    AcquireSRWLockExclusive(&receive_lock);
    receive_head = receive_count = receive_dropped = 0;
    receive_sequence = 0;
    receive_statistics = {};
    ReleaseSRWLockExclusive(&receive_lock);
    MH_STATUS status = MH_Initialize();
    if (status != MH_OK) return 600 + status;
    void* target = reinterpret_cast<void*>(core_address(core_base, 0x17b9010));
    status = MH_CreateHook(target, reinterpret_cast<void*>(&text_receive), reinterpret_cast<void**>(&original_receive));
    if (status != MH_OK) { MH_Uninitialize(); return 600 + status; }
    status = MH_EnableHook(target);
    if (status != MH_OK) {
        MH_RemoveHook(target); MH_Uninitialize(); return 600 + status;
    }
    receive_hook_created = true;
    const DWORD friend_status = friend_receive_start();
    if (friend_status != ERROR_SUCCESS) return friend_status;
    return recall_receive_start();
}

static void scrub_extended_text(ReceivedMessage* message) {
    // 旧调用者无法解释新的载荷标志；保留此前不投递长文本的行为。
    if (message->type == 1 && (message->reserved & (RECEIVE_LONG_TEXT | RECEIVE_TEXT_UNAVAILABLE))) {
        message->present = 0;
        SecureZeroMemory(message->image_xml, sizeof(message->image_xml));
        message->reserved = 0;
    }
}

extern "C" __declspec(dllexport) DWORD WINAPI WxPollTextMessage(void* parameter) {
    // 保留旧接口的 1312 字节边界，旧调用者不会被 V2 写越界。
    if (!parameter) return ERROR_INVALID_PARAMETER;
    ReceivedMessage message{};
    const DWORD code = poll_received(&message);
    scrub_extended_text(&message);
    std::memcpy(parameter, &message, 1312);
    *static_cast<uint32_t*>(parameter) = 1312;
    return code;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxPollTextMessageV2(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    ReceivedMessage message{};
    const DWORD code = poll_received(&message);
    scrub_extended_text(&message);
    std::memcpy(parameter, &message, 5408);
    *static_cast<uint32_t*>(parameter) = 5408;
    return code;
}

static void scrub_legacy_voice(ReceivedMessage* message) {
    if (message->voice_size && message->voice_offset <= sizeof(message->image_xml) &&
        message->voice_size <= sizeof(message->image_xml) - message->voice_offset)
        SecureZeroMemory(message->image_xml + message->voice_offset, message->voice_size);
    message->voice_offset = message->voice_size = 0;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxPollMediaMessageV3(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    auto message = std::unique_ptr<ReceivedMessage>(new (std::nothrow) ReceivedMessage{});
    if (!message) return ERROR_NOT_ENOUGH_MEMORY;
    const DWORD code = poll_received(message.get());
    scrub_extended_text(message.get());
    scrub_legacy_voice(message.get());
    if (std::strlen(message->image_xml) >= 16384) {
        SecureZeroMemory(message->image_xml, 16384);
        message->reserved |= 1;
    }
    std::memcpy(parameter, message.get(), 21792);
    *static_cast<uint32_t*>(parameter) = 21792;
    return code;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxPollMediaMessageV4(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    auto message = std::unique_ptr<ReceivedMessage>(new (std::nothrow) ReceivedMessage{});
    if (!message) return ERROR_NOT_ENOUGH_MEMORY;
    const DWORD code = poll_received(message.get());
    scrub_extended_text(message.get());
    scrub_legacy_voice(message.get());
    std::memcpy(parameter, message.get(), 267552);
    *static_cast<uint32_t*>(parameter) = 267552;
    return code;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxPollMediaMessageV5(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    auto message = std::unique_ptr<ReceivedMessage>(new (std::nothrow) ReceivedMessage{});
    if (!message) return ERROR_NOT_ENOUGH_MEMORY;
    const DWORD code = poll_received(message.get());
    scrub_extended_text(message.get());
    scrub_legacy_voice(message.get());
    std::memcpy(parameter, message.get(), 267560);
    *static_cast<uint32_t*>(parameter) = 267560;
    return code;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxPollMediaMessageV6(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    const DWORD code = poll_received(static_cast<ReceivedMessage*>(parameter));
    scrub_extended_text(static_cast<ReceivedMessage*>(parameter));
    return code;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxPollMediaMessageV7(void* parameter) {
    return poll_received(static_cast<ReceivedMessage*>(parameter));
}

extern "C" __declspec(dllexport) DWORD WINAPI WxGetReceiveStatistics(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    _InterlockedIncrement(&active_callbacks);
    auto output = static_cast<ReceiveStatistics*>(parameter);
    *output = receive_statistics;
    output->size = sizeof(*output);
    _InterlockedDecrement(&active_callbacks);
    return 0;
}

#include "image_send.h"
#include "image_download.h"
#include "emoticon_send.h"
#include "voice_send.h"
#include "file_download.h"
#include "file_send.h"
#include "group_management.h"
#include "friend_request.h"
#include "friend_receive.h"

// 定向只读探针：仅在手动发送 WXBOT_AT_PROBE 的消息中记录 msgsource 字符串。
// 不发送消息、不改写消息对象，用于核对该版本真实艾特的元数据布局。
static TextConfig mention_probe_config{};

static bool copy_small_string(const unsigned char* object, size_t offset, char* output, size_t size) {
    __try {
        const auto string = reinterpret_cast<const SmallString*>(object + offset);
        if (!string->length || string->length >= size || string->capacity < string->length ||
            !(string->capacity == 15 || (string->capacity >= 16 && string->capacity <= 1024 * 1024))) return false;
        const char* data = string->capacity == 15 ? string->data : *reinterpret_cast<const char* const*>(string->data);
        std::memcpy(output, data, static_cast<size_t>(string->length));
        output[string->length] = 0;
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

static void capture_mention_source(SharedMessage* message) {
    __try {
        if (!message || !message->object) return;
        const auto object = static_cast<const unsigned char*>(message->object);
        if (*reinterpret_cast<const uintptr_t*>(object) != core_address(core_base, 0x8e53588)) return;
        char body[1024]{};
        if (!copy_small_string(object, 0x758, body, sizeof(body)) || !std::strstr(body, "WXBOT_AT_PROBE")) return;
        if (_InterlockedCompareExchange(&triggered_once, 1, 0) != 0) return;
        FILE* log = nullptr;
        if (_wfopen_s(&log, mention_probe_config.base.log_path, L"wb") != 0 || !log) return;
        std::fprintf(log, "{\"vtable_rva\":\"0x%llx\",\"fields\":[", static_cast<unsigned long long>(core_rva(0x8e53588)));
        bool comma = false;
        for (size_t offset = 0; offset + sizeof(SmallString) <= 0x798; offset += 8) {
            char value[4096]{};
            if (!copy_small_string(object, offset, value, sizeof(value))) continue;
            if (!std::strstr(value, "msgsource") && !std::strstr(value, "atuserlist") &&
                !std::strstr(value, "WXBOT_AT_PROBE") && !std::strstr(value, "wxid_") &&
                !std::strstr(value, "@chatroom")) continue;
            if (comma) std::fputc(',', log);
            std::fprintf(log, "{\"offset\":%llu,\"value\":", static_cast<unsigned long long>(offset));
            write_json_string(log, value);
            std::fputc('}', log);
            comma = true;
        }
        std::fputs("],\"vectors\":[", log);
        comma = false;
        for (size_t offset = 0; offset + 24 <= 0x798; offset += 8) {
            const auto begin = *reinterpret_cast<const uintptr_t*>(object + offset);
            const auto end = *reinterpret_cast<const uintptr_t*>(object + offset + 8);
            const auto capacity = *reinterpret_cast<const uintptr_t*>(object + offset + 16);
            if (begin < 0x10000 || end <= begin || end > capacity || end - begin > 256 ||
                (end - begin) % 32 || capacity - begin > 1024 * 1024) continue;
            char member[128]{};
            if (!copy_small_string(reinterpret_cast<const unsigned char*>(begin), 0, member, sizeof(member)) ||
                std::strncmp(member, "wxid_", 5)) continue;
            if (comma) std::fputc(',', log);
            std::fprintf(log, "{\"offset\":%llu,\"member\":", static_cast<unsigned long long>(offset));
            write_json_string(log, member);
            std::fputc('}', log);
            comma = true;
        }
        std::fputs("],\"object_hex\":\"", log);
        for (size_t offset = 0; offset < 0x798; ++offset) std::fprintf(log, "%02x", object[offset]);
        std::fputs("\"}\n", log);
        std::fclose(log);
    } __except (EXCEPTION_EXECUTE_HANDLER) { }
}

static void* __fastcall mention_probe_send(void* manager, void* output, SharedMessage* message, uint8_t flag) {
    _InterlockedIncrement(&active_callbacks);
    void* result = nullptr;
    __try {
        capture_mention_source(message);
        result = original_send(manager, output, message, flag);
    } __finally { _InterlockedDecrement(&active_callbacks); }
    return result;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxArmMentionProbe(void* parameter) {
    if (!parameter || hook_created || receive_hook_created) return ERROR_BUSY;
    const TextConfig config = *static_cast<const TextConfig*>(parameter);
    if (config.base.size != sizeof(TextConfig) || config.base.pid != GetCurrentProcessId()) return 3;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 4;
    core_base = reinterpret_cast<uintptr_t>(core);
    mention_probe_config = config;
    triggered_once = 0;
    MH_STATUS status = MH_Initialize();
    if (status != MH_OK) return 600 + status;
    void* target = reinterpret_cast<void*>(core_address(core_base, 0x178f100));
    status = MH_CreateHook(target, reinterpret_cast<void*>(&mention_probe_send), reinterpret_cast<void**>(&original_send));
    if (status != MH_OK) { MH_Uninitialize(); return 600 + status; }
    status = MH_EnableHook(target);
    if (status != MH_OK) { MH_RemoveHook(target); MH_Uninitialize(); return 600 + status; }
    hook_created = true;
    return 0;
}

#include "moments_send.h"

#ifndef WXBOT_EXCLUDE_SNS_INTERACTION
#include "moments_interaction_bridge.h"
#endif
