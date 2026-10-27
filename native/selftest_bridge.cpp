// A version-locked native self-test. Only sends to the current user's filehelper.
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <bcrypt.h>
#include <cstdint>
#include <cstring>
#include <algorithm>
#include <cstdio>
#include <functional>
#include <memory>
#include "vendor/minhook-1.3.4/include/MinHook.h"
#include "core_version.h"

struct SharedMessage { void* object; void* control; };
struct SmallString { char data[16]; uint64_t length; uint64_t capacity; };
struct TestConfig {
    uint32_t size;
    uint32_t pid;
    uint64_t manager;
    wchar_t log_path[MAX_PATH];
    char text[1024];
};
struct TestResult {
    DWORD stage;
    DWORD exception;
    uint64_t vtable_rva;
    uint32_t native_status;
    uint64_t exception_pc;
    uint64_t fault_address;
    uint64_t access_kind;
    uint32_t native_status_high;
};

using SendFunction = void* (__fastcall*)(void*, void*, SharedMessage*, uint8_t);
static SendFunction original_send = nullptr;
static uintptr_t core_base = 0;
static TestConfig test_config{};
static volatile LONG active_callbacks = 0;
static volatile LONG triggered_once = 0;
static volatile LONG live_completions = 0;
static volatile LONG live_tasks = 0;
static bool hook_created = false;
static bool factory_hook_created = false;
static bool image_download_factory_hook_created = false;
static DWORD factory_tls = TLS_OUT_OF_INDEXES;
using ReceiveFunction = uintptr_t (__fastcall*)(void*, const uintptr_t*, uint8_t, uint8_t);
static ReceiveFunction original_receive = nullptr;
static bool receive_hook_created = false;
#ifdef WXBOT_RECALL_RECEIVE
static bool recall_receive_hook_created = false;
#endif
#ifdef WXBOT_FRIEND_RECEIVE
static bool friend_receive_hook_created = false;
#endif
static TestConfig receive_config{};
static bool receive_queue_mode = false;

// 只复制文件传输助手消息；Hook 内不做磁盘读写或调用 Python。
struct ReceivedMessage {
    uint32_t size, present, type, thread_id, dropped, reserved;
    uint64_t sequence;
    char sender[128], receiver[128], text[1024];
#ifdef WXBOT_EXTENDED_RECEIVE
    char mention_source[4096];
#endif
#ifdef WXBOT_MEDIA_RECEIVE
#ifdef WXBOT_LARGE_MEDIA_RECEIVE
    char image_xml[262144];
#else
    char image_xml[16384];
#endif
#endif
#ifdef WXBOT_MESSAGE_ID_RECEIVE
    uint64_t native_message_id;
#endif
#ifdef WXBOT_VOICE_RECEIVE
    uint32_t voice_offset, voice_size; // Binary slice within image_xml, after its XML NUL.
#endif
};
#ifdef WXBOT_VOICE_RECEIVE
static_assert(sizeof(ReceivedMessage) == 267568, "Unexpected media receive V6 ABI");
#elif defined(WXBOT_MESSAGE_ID_RECEIVE)
static_assert(sizeof(ReceivedMessage) == 267560, "Unexpected media receive V5 ABI");
#elif defined(WXBOT_LARGE_MEDIA_RECEIVE)
static_assert(sizeof(ReceivedMessage) == 267552, "Unexpected media receive V4 ABI");
#elif defined(WXBOT_MEDIA_RECEIVE)
static_assert(sizeof(ReceivedMessage) == 21792, "Unexpected media receive V3 ABI");
#elif defined(WXBOT_EXTENDED_RECEIVE)
static_assert(sizeof(ReceivedMessage) == 5408, "Unexpected text receive V2 ABI");
#else
static_assert(sizeof(ReceivedMessage) == 1312, "Unexpected receive ABI");
#endif
static SRWLOCK receive_lock = SRWLOCK_INIT;
static ReceivedMessage received_messages[64]{};
static uint32_t receive_head = 0, receive_count = 0, receive_dropped = 0;
static uint64_t receive_sequence = 0;

static void enqueue_received(const char* sender, const char* receiver, const char* text,
                             const char* source = nullptr, uint32_t type = 1, const char* image_xml = nullptr,
                             uint32_t reserved = 0, uint64_t native_message_id = 0,
                             const void* voice = nullptr, uint32_t voice_size = 0) {
    // 大型转发内容留在堆上，避免消耗微信接收线程的栈。
    auto storage = std::unique_ptr<ReceivedMessage>(new (std::nothrow) ReceivedMessage{});
    if (!storage) return;
    auto& message = *storage;
    message.size = sizeof(message);
    message.present = 1;
    message.type = type;
    message.reserved = reserved;
    message.thread_id = GetCurrentThreadId();
#ifdef WXBOT_MESSAGE_ID_RECEIVE
    message.native_message_id = native_message_id;
#else
    (void)native_message_id;
#endif
    std::memcpy(message.sender, sender, std::strlen(sender) + 1);
    std::memcpy(message.receiver, receiver, std::strlen(receiver) + 1);
    std::memcpy(message.text, text, std::strlen(text) + 1);
#ifdef WXBOT_EXTENDED_RECEIVE
    if (source) std::memcpy(message.mention_source, source, std::strlen(source) + 1);
#else
    (void)source;
#endif
#ifdef WXBOT_MEDIA_RECEIVE
    if (image_xml) strcpy_s(message.image_xml, image_xml);
#else
    (void)image_xml;
#endif
#ifdef WXBOT_VOICE_RECEIVE
    if (type == 34 && voice && voice_size) {
        const size_t offset = std::strlen(message.image_xml) + 1;
        if (offset < sizeof(message.image_xml) && voice_size <= sizeof(message.image_xml) - offset) {
            std::memcpy(message.image_xml + offset, voice, voice_size);
            message.voice_offset = static_cast<uint32_t>(offset);
            message.voice_size = voice_size;
        }
    }
#else
    (void)voice;
    (void)voice_size;
#endif
    AcquireSRWLockExclusive(&receive_lock);
    if (receive_count == std::size(received_messages)) {
        receive_head = (receive_head + 1) % 64;
        --receive_count;
        ++receive_dropped;
    }
    message.sequence = ++receive_sequence;
    message.dropped = receive_dropped;
    received_messages[(receive_head + receive_count) % 64] = message;
    ++receive_count;
    ReleaseSRWLockExclusive(&receive_lock);
}

static int record_exception(EXCEPTION_POINTERS* exception, TestResult* result) {
    result->exception = exception->ExceptionRecord->ExceptionCode;
    result->exception_pc = reinterpret_cast<uintptr_t>(exception->ExceptionRecord->ExceptionAddress);
    if (exception->ExceptionRecord->NumberParameters >= 2) {
        result->access_kind = exception->ExceptionRecord->ExceptionInformation[0];
        result->fault_address = exception->ExceptionRecord->ExceptionInformation[1];
    }
    return EXCEPTION_EXECUTE_HANDLER;
}

static bool core_image_identity(HMODULE core, uint32_t* timestamp, uint32_t* image_size) {
    __try {
        const auto bytes = reinterpret_cast<const unsigned char*>(core);
        const auto dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(bytes);
        if (dos->e_magic != IMAGE_DOS_SIGNATURE || dos->e_lfanew < 64 || dos->e_lfanew > 1048576) return false;
        const auto nt = reinterpret_cast<const IMAGE_NT_HEADERS64*>(bytes + dos->e_lfanew);
        if (nt->Signature != IMAGE_NT_SIGNATURE || nt->FileHeader.Machine != IMAGE_FILE_MACHINE_AMD64 ||
                nt->OptionalHeader.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC) return false;
        *timestamp = nt->FileHeader.TimeDateStamp;
        *image_size = nt->OptionalHeader.SizeOfImage;
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

static bool core_fingerprint(HMODULE core) {
    uint32_t timestamp = 0, image_size = 0;
    if (!core_image_identity(core, &timestamp, &image_size)) return false;
    wchar_t path[32768]{};
    if (!GetModuleFileNameW(core, path, static_cast<DWORD>(std::size(path)))) return false;
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE,
                              nullptr, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
    if (file == INVALID_HANDLE_VALUE) return false;
    LARGE_INTEGER size{};
    bool valid = GetFileSizeEx(file, &size) && core_size_supported(static_cast<uint64_t>(size.QuadPart));
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    if (valid && BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) >= 0) {
        // Windows allocates the hash object when pbHashObject is NULL on supported Windows.
        if (BCryptCreateHash(algorithm, &hash, nullptr, 0, nullptr, 0, 0) >= 0) {
            unsigned char buffer[65536], digest[32];
            DWORD count = 0;
            while (valid) {
                valid = ReadFile(file, buffer, sizeof(buffer), &count, nullptr) != FALSE;
                if (!valid || !count) break;
                valid = BCryptHashData(hash, buffer, count, 0) >= 0;
            }
            valid = valid && BCryptFinishHash(hash, digest, sizeof(digest), 0) >= 0 &&
                core_select_build(static_cast<uint64_t>(size.QuadPart), digest, timestamp, image_size);
            BCryptDestroyHash(hash);
        } else valid = false;
        BCryptCloseAlgorithmProvider(algorithm, 0);
    } else valid = false;
    CloseHandle(file);
    return valid;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxInspectCoreVersion(void*) {
    const auto core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 0;
    return static_cast<DWORD>(InterlockedCompareExchange(&core_profile_id, 0, 0));
}

static void release_shared(SharedMessage& message) {
    if (!message.control) return;
    auto control = static_cast<unsigned char*>(message.control);
    if (_InterlockedDecrement(reinterpret_cast<volatile LONG*>(control + 8)) == 0) {
        using VirtualFunction = void(__fastcall*)(void*);
        auto table = *reinterpret_cast<VirtualFunction**>(control);
        table[0](control);
        if (_InterlockedDecrement(reinterpret_cast<volatile LONG*>(control + 12)) == 0) {
            table = *reinterpret_cast<VirtualFunction**>(control);
            table[1](control);
        }
    }
    message = {};
}

static void guarded_release_shared(SharedMessage* message, TestResult* report) {
    __try {
        release_shared(*message);
    } __except (record_exception(GetExceptionInformation(), report)) {
    }
}

static bool set_inline(SmallString* string, const char* text) {
    const size_t length = std::strlen(text);
    if (length > 15 || string->capacity != 15 || string->length != 0) return false;
    std::memcpy(string->data, text, length + 1);
    string->length = length;
    return true;
}

static bool set_native_string(SmallString* string, const char* text) {
    const size_t length = std::strlen(text);
    if (length < 16) return set_inline(string, text);
    if (length > 1023 || string->capacity != 15 || string->length != 0) return false;
    using Allocate = void* (__fastcall*)(size_t);
    char* buffer = static_cast<char*>(reinterpret_cast<Allocate>(core_address(core_base, 0x74ab89c))(length + 1));
    if (!buffer) return false;
    std::memcpy(buffer, text, length + 1);
    std::memcpy(string->data, &buffer, sizeof(buffer));
    string->length = string->capacity = length;
    return true;
}

static void free_native_string(SmallString* string) {
    if (string->capacity > 15) {
        void* allocation = *reinterpret_cast<void**>(string->data);
        using Free = void (__fastcall*)(void*, size_t);
        reinterpret_cast<Free>(core_address(core_base, 0x74ab8e0))(allocation, string->capacity + 1);
    }
    *string = {};
    string->capacity = 15;
}

static void write_json_string(FILE* log, const char* text) {
    std::fputc('"', log);
    for (const unsigned char* current = reinterpret_cast<const unsigned char*>(text); *current; ++current) {
        if (*current == '"' || *current == '\\') { std::fputc('\\', log); std::fputc(*current, log); }
        else if (*current < 32) std::fprintf(log, "\\u%04x", *current);
        else std::fputc(*current, log);
    }
    std::fputc('"', log);
}

static void native_test(uintptr_t base, void* manager, TestResult* report) {
    // Keep this SEH boundary free of C++ objects that require unwinding.
    SharedMessage message{};
    alignas(16) unsigned char result[128]{};
    __try {
        report->stage = 1;
        using Factory = SharedMessage* (__fastcall*)(SharedMessage*);
        reinterpret_cast<Factory>(core_address(base, 0x6f4c70))(&message);
        if (!message.object || !message.control) { report->stage = 11; return; }
        auto object = static_cast<unsigned char*>(message.object);
        report->vtable_rva = *reinterpret_cast<uintptr_t*>(object) - base;
        if (report->vtable_rva != core_rva(0x8e53588)) { report->stage = 12; release_shared(message); return; }
        if (!set_inline(reinterpret_cast<SmallString*>(object + 0xb0), "filehelper") ||
            !set_inline(reinterpret_cast<SmallString*>(object + 0x758), "WXNATIVE_TEST_2")) {
            report->stage = 13; release_shared(message); return;
        }
        *reinterpret_cast<uint32_t*>(object + 0x118) = 1;
        // The client text builder writes only type, receiver and body. Keep all
        // other fields at the constructor's defaults rather than borrowing
        // unverified offsets from an older version.
        report->stage = 2;
        auto send = original_send ? original_send : reinterpret_cast<SendFunction>(core_address(base, 0x178f100));
        // The native function consumes one strong reference to its argument.
        // Retain the factory's reference separately from that transferred copy.
        SharedMessage transferred = message;
        _InterlockedIncrement(reinterpret_cast<volatile LONG*>(
            static_cast<unsigned char*>(message.control) + 8));
        send(manager, result, &transferred, 1);
        report->stage = 3;
        report->native_status = *reinterpret_cast<uint32_t*>(result);
        report->native_status_high = *reinterpret_cast<uint32_t*>(result + 4);
        release_shared(message);
        auto result_message = reinterpret_cast<SharedMessage*>(result + 40);
        release_shared(*result_message);
        // This result contains a native std::string after its eight-byte status field.
        auto error_text = reinterpret_cast<SmallString*>(result + 8);
        if (error_text->capacity > 15 && error_text->capacity < 1024 * 1024) {
            void* allocation = *reinterpret_cast<void**>(error_text->data);
            size_t allocation_size = static_cast<size_t>(error_text->capacity + 1);
            if (allocation_size >= 4096) {
                allocation = *(reinterpret_cast<void**>(allocation) - 1);
                allocation_size += 39;
            }
            using Free = void(__fastcall*)(void*, size_t);
            reinterpret_cast<Free>(core_address(base, 0x74ab8e0))(allocation, allocation_size);
        }
        report->stage = 4;
    } __except (record_exception(GetExceptionInformation(), report)) {
    }
}

static void write_report(const TestConfig& config, const TestResult& result,
                         const char* marker = "WXNATIVE_TEST_2") {
    FILE* log = nullptr;
    if (_wfopen_s(&log, config.log_path, L"wb") == 0 && log) {
        std::fprintf(log, "{\"stage\":%lu,\"exception\":%lu,\"vtable_rva\":\"0x%llx\","
                          "\"native_status\":%u,\"exception_pc\":\"0x%llx\","
                          "\"exception_rva\":\"0x%llx\",\"fault_address\":\"0x%llx\","
                          "\"access_kind\":%llu,\"native_status_high\":%u,\"target\":\"filehelper\","
                          "\"text\":\"%s\"}\n",
                     result.stage, result.exception, result.vtable_rva, result.native_status,
                     result.exception_pc, result.exception_pc ? result.exception_pc - core_base : 0,
                     result.fault_address, result.access_kind, result.native_status_high, marker);
        std::fclose(log);
    }
}

static bool is_trigger(SharedMessage* message) {
    __try {
        if (!message || !message->object) return false;
        auto object = static_cast<unsigned char*>(message->object);
        const auto receiver = reinterpret_cast<const SmallString*>(object + 0xb0);
        const auto text = reinterpret_cast<const SmallString*>(object + 0x758);
        if (receiver->length != 10 || receiver->capacity != 15 ||
            std::memcmp(receiver->data, "filehelper", 10) != 0 || text->length != 16) return false;
        const char* buffer = text->capacity < 16 ? text->data : *reinterpret_cast<const char* const*>(text->data);
        return std::memcmp(buffer, "WXNATIVE_TRIGGER", 16) == 0;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

static void* __fastcall hooked_send(void* manager, void* output, SharedMessage* message, uint8_t flag) {
    _InterlockedIncrement(&active_callbacks);
    const bool trigger = is_trigger(message);
    void* result = nullptr;
    __try {
        result = original_send(manager, output, message, flag);
        if (trigger && _InterlockedCompareExchange(&triggered_once, 1, 0) == 0) {
            TestResult report{};
            native_test(core_base, manager, &report);
            write_report(test_config, report);
        }
    } __finally { _InterlockedDecrement(&active_callbacks); }
    return result;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxArmSelfTest(void* parameter) {
    if (!parameter) return 2;
    const TestConfig config = *static_cast<const TestConfig*>(parameter);
    if (config.size != sizeof(TestConfig) || config.pid != GetCurrentProcessId()) return 3;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 4;
    core_base = reinterpret_cast<uintptr_t>(core);
    test_config = config;
    triggered_once = 0;
    MH_STATUS status = MH_Initialize();
    if (status != MH_OK) return 600 + status;
    status = MH_CreateHook(reinterpret_cast<void*>(core_address(core_base, 0x178f100)),
                           reinterpret_cast<void*>(&hooked_send), reinterpret_cast<void**>(&original_send));
    if (status != MH_OK) { MH_Uninitialize(); return 600 + status; }
    status = MH_EnableHook(reinterpret_cast<void*>(core_address(core_base, 0x178f100)));
    if (status != MH_OK) { MH_RemoveHook(reinterpret_cast<void*>(core_address(core_base, 0x178f100))); MH_Uninitialize(); }
    hook_created = status == MH_OK;
    return status == MH_OK ? 0 : 600 + status;
}

// The client's text builder accepts this intrusive completion object. Its ABI
// has a reference count at +0, an invoke pointer at +8, and a destroy pointer
// at +16, as confirmed in 0x5429c30 and 0x107050.
struct Completion {
    volatile LONG references;
    uint32_t padding;
    void (__fastcall* invoke)(Completion*, const unsigned char*);
    void (__fastcall* destroy)(Completion*);
    TestConfig config;
};

static void __fastcall completion_invoke(Completion* self, const unsigned char* result) {
    _InterlockedIncrement(&active_callbacks);
    __try {
        TestResult report{};
        report.stage = 21;
        report.native_status = result && result[0] == 1 ? 0 : 1;
        FILE* log = nullptr;
        if (_wfopen_s(&log, self->config.log_path, L"wb") == 0 && log) {
            std::fprintf(log, "{\"stage\":21,\"exception\":0,\"native_status\":%u,"
                              "\"target\":\"filehelper\",\"text\":", report.native_status);
            write_json_string(log, self->config.text);
            std::fprintf(log, ",\"thread_id\":%lu,\"completion_success\":%s}\n", GetCurrentThreadId(),
                         report.native_status == 0 ? "true" : "false");
            std::fclose(log);
        }
    } __finally { _InterlockedDecrement(&active_callbacks); }
}

static void __fastcall completion_destroy(Completion* self) {
    delete self;
    _InterlockedDecrement(&live_completions);
}

static DWORD dispatch_on_native_task(void* parameter) {
    if (!parameter || live_completions != 0) return ERROR_BUSY;
    const TestConfig config = *static_cast<const TestConfig*>(parameter);
    if (config.size != sizeof(TestConfig) || config.pid != GetCurrentProcessId()) return 3;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 4;
    core_base = reinterpret_cast<uintptr_t>(core);
    struct TextRequest { SmallString receiver; SmallString body; } request{};
    request.receiver.capacity = request.body.capacity = 15;
    SmallString description{};
    description.capacity = 15;
    if (!set_inline(&request.receiver, "filehelper") ||
        !set_native_string(&request.body, config.text) ||
        !set_inline(&description, "wxbot.selftest")) return 13;
    Completion* completion = new Completion{1, 0, completion_invoke, completion_destroy, config};
    _InterlockedIncrement(&live_completions);
    TestResult report{};
    __try {
        // The native builder owns scheduling, message allocation and the
        // completion reference. No UI input or user message is required.
        using Builder = void (__fastcall*)(void*, TextRequest*, SmallString*, Completion**);
        reinterpret_cast<Builder>(core_address(core_base, 0x54b02f0))(nullptr, &request, &description, &completion);
        report.stage = 20;
    } __except (record_exception(GetExceptionInformation(), &report)) {
        write_report(config, report, "WXNATIVE_TEST_3");
    }
    if (completion) completion_destroy(completion);
    free_native_string(&request.body);
    return report.exception ? 5 : 0;
}

struct TaskLease {
    TestConfig config;
    explicit TaskLease(const TestConfig& value) : config(value) {
        _InterlockedIncrement(&live_tasks);
    }
    ~TaskLease() { _InterlockedDecrement(&live_tasks); }
};

static DWORD schedule_task(const TestConfig& config, std::function<void()>* callable) {
    static_assert(sizeof(std::function<void()>) == 64, "Unexpected function ABI");
    struct Location { const char* function; const char* file; uint32_t line; uint32_t pad; void* caller; };
    static_assert(sizeof(Location) == 32, "Unexpected source location ABI");
    Location location{"wxbot.selftest", "selftest_bridge.cpp", 1, 0, nullptr};
    SharedMessage scheduler{}, future{};
    TestResult error{};
    __try {
        __try {
            // 0x45d10 reads the process-wide application pointer; 0x47310 copies
            // its owned scheduler. These helpers do not require a task-local TLS.
            using App = void* (__fastcall*)();
            using Scheduler = SharedMessage* (__fastcall*)(void*, SharedMessage*);
            void* app = reinterpret_cast<App>(core_address(core_base, 0x45d10))();
            if (!app) return ERROR_NOT_READY;
            reinterpret_cast<Scheduler>(core_address(core_base, 0x47310))(app, &scheduler);
            if (!scheduler.object || !scheduler.control) return ERROR_NOT_READY;
            using Schedule = SharedMessage* (__fastcall*)(void*, SharedMessage*, Location*, std::function<void()>*, uint32_t);
            reinterpret_cast<Schedule>(core_address(core_base, 0x2fb010))(scheduler.object, &future, &location, callable, 1);
        } __finally {
            guarded_release_shared(&future, &error);
            guarded_release_shared(&scheduler, &error);
        }
    } __except (record_exception(GetExceptionInformation(), &error)) {
    }
    if (error.exception) {
        write_report(config, error, "WXNATIVE_TEST_3");
        return 5;
    }
    return 0;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxDispatchSelfTest(void* parameter) {
    if (!parameter || live_completions != 0 || live_tasks != 0) return ERROR_BUSY;
    const TestConfig config = *static_cast<const TestConfig*>(parameter);
    if (config.size != sizeof(TestConfig) || config.pid != GetCurrentProcessId()) return 3;
    const size_t text_length = strnlen_s(config.text, sizeof(config.text));
    if (!text_length || text_length >= sizeof(config.text) ||
        !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, config.text, static_cast<int>(text_length), nullptr, 0)) return 13;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 4;
    core_base = reinterpret_cast<uintptr_t>(core);
    auto lease = std::make_shared<TaskLease>(config);
    std::function<void()> task = [lease]() {
        _InterlockedIncrement(&active_callbacks);
        dispatch_on_native_task(&lease->config);
        _InterlockedDecrement(&active_callbacks);
    };
    return schedule_task(config, &task);
}

static bool read_pb_string(const unsigned char* message, size_t offset, char* output, size_t capacity) {
    const auto wrapper = *reinterpret_cast<const unsigned char* const*>(message + offset);
    if (!wrapper) return false;
    const auto string = *reinterpret_cast<const SmallString* const*>(wrapper + 8);
    if (!string || string->length >= capacity || string->capacity < string->length) return false;
    const char* bytes = string->capacity > 15 ? *reinterpret_cast<const char* const*>(string->data) : string->data;
    std::memcpy(output, bytes, static_cast<size_t>(string->length));
    output[string->length] = 0;
    return true;
}

static void sample_receive(const uintptr_t* batch) {
    __try {
        if (!batch || batch[1] < batch[0] || (batch[1] - batch[0]) % 0x78 != 0 ||
            (batch[1] - batch[0]) / 0x78 > 512) return;
        for (uintptr_t address = batch[0]; address < batch[1]; address += 0x78) {
            const auto message = reinterpret_cast<const unsigned char*>(address);
            if (*reinterpret_cast<const uint32_t*>(message + 0x14) != 1) continue;
            char sender[128]{}, receiver[128]{}, text[1024]{};
            if (!read_pb_string(message, 8, sender, sizeof(sender)) ||
                !read_pb_string(message, 0x18, receiver, sizeof(receiver)) ||
                (std::strcmp(sender, "filehelper") && std::strcmp(receiver, "filehelper")) ||
                !read_pb_string(message, 0x20, text, sizeof(text))) continue;
            static constexpr char prefix[] = "WXBOT_RECV_411513_";
            if (receive_queue_mode) {
                enqueue_received(sender, receiver, text);
                continue;
            }
            if (std::strncmp(text, prefix, sizeof(prefix) - 1) != 0) continue;
            FILE* log = nullptr;
            if (_wfopen_s(&log, receive_config.log_path, L"wb") == 0 && log) {
                std::fprintf(log, "{\"event\":\"receive_candidate_hit\",\"rva\":\"0x%llx\","
                                  "\"message_type\":1,\"target\":\"filehelper\",\"text\":",
                                  static_cast<unsigned long long>(core_rva(0x17b9010)));
                write_json_string(log, text);
                std::fprintf(log, ",\"thread_id\":%lu,\"batch_stride\":120,\"body_wrapper_offset\":32}\n", GetCurrentThreadId());
                std::fclose(log);
            }
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        // A static layout mismatch must not affect the client's genuine receive.
    }
}

static uintptr_t __fastcall hooked_receive(void* manager, const uintptr_t* batch, uint8_t flag1, uint8_t flag2) {
    _InterlockedIncrement(&active_callbacks);
    uintptr_t result = 0;
    __try {
        sample_receive(batch);
        result = original_receive(manager, batch, flag1, flag2);
    } __finally { _InterlockedDecrement(&active_callbacks); }
    return result;
}

static DWORD start_receive(void* parameter, bool queue_mode) {
    if (!parameter || hook_created || receive_hook_created) return ERROR_BUSY;
    const TestConfig config = *static_cast<const TestConfig*>(parameter);
    if (config.size != sizeof(TestConfig) || config.pid != GetCurrentProcessId()) return 3;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 4;
    core_base = reinterpret_cast<uintptr_t>(core);
    receive_config = config;
    receive_queue_mode = queue_mode;
    AcquireSRWLockExclusive(&receive_lock);
    receive_head = receive_count = receive_dropped = 0;
    receive_sequence = 0;
    ReleaseSRWLockExclusive(&receive_lock);
    MH_STATUS status = MH_Initialize();
    if (status != MH_OK) return 600 + status;
    void* target = reinterpret_cast<void*>(core_address(core_base, 0x17b9010));
    status = MH_CreateHook(target, reinterpret_cast<void*>(&hooked_receive), reinterpret_cast<void**>(&original_receive));
    if (status != MH_OK) { MH_Uninitialize(); return 600 + status; }
    status = MH_EnableHook(target);
    if (status != MH_OK) { MH_RemoveHook(target); MH_Uninitialize(); return 600 + status; }
    receive_hook_created = true;
    return 0;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxArmReceiveTest(void* parameter) {
    return start_receive(parameter, false);
}

extern "C" __declspec(dllexport) DWORD WINAPI WxStartFileHelperReceiver(void* parameter) {
    return start_receive(parameter, true);
}

static DWORD poll_received(ReceivedMessage* output) {
    if (!output) return ERROR_INVALID_PARAMETER;
    _InterlockedIncrement(&active_callbacks);
    AcquireSRWLockExclusive(&receive_lock);
    *output = {};
    output->size = sizeof(*output);
    if (receive_count) {
        *output = received_messages[receive_head];
        SecureZeroMemory(&received_messages[receive_head], sizeof(ReceivedMessage));
        receive_head = (receive_head + 1) % 64;
        --receive_count;
    }
    output->dropped = receive_dropped;
    ReleaseSRWLockExclusive(&receive_lock);
    _InterlockedDecrement(&active_callbacks);
    return 0;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxPollFileHelperMessage(void* parameter) {
    // This export predates extended text/media receive and always writes V1.
    if (!parameter) return ERROR_INVALID_PARAMETER;
    ReceivedMessage message{};
    const DWORD code = poll_received(&message);
    std::memcpy(parameter, &message, 1312);
    *static_cast<uint32_t*>(parameter) = 1312;
    return code;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxWaitForTasks(void*) {
    for (int attempt = 0; (active_callbacks != 0 || live_completions != 0 || live_tasks != 0) && attempt < 100; ++attempt) Sleep(50);
    return active_callbacks != 0 || live_completions != 0 || live_tasks != 0 ? ERROR_BUSY : 0;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxRemoveSelfTest(void*) {
    const auto target = reinterpret_cast<void*>(core_address(core_base, 0x178f100));
    MH_STATUS status = MH_OK;
    if (hook_created) {
        status = MH_DisableHook(target);
        if (status != MH_OK && status != MH_ERROR_DISABLED) return 600 + status;
    }
    void* receive_target = reinterpret_cast<void*>(core_address(core_base, 0x17b9010));
    if (receive_hook_created) {
        status = MH_DisableHook(receive_target);
        if (status != MH_OK && status != MH_ERROR_DISABLED) return 600 + status;
    }
#ifdef WXBOT_RECALL_RECEIVE
    void* recall_receive_target = reinterpret_cast<void*>(core_address(core_base, 0x229e790));
    if (recall_receive_hook_created) {
        status = MH_DisableHook(recall_receive_target);
        if (status != MH_OK && status != MH_ERROR_DISABLED) return 600 + status;
    }
#endif
#ifdef WXBOT_FRIEND_RECEIVE
    void* friend_receive_target = reinterpret_cast<void*>(core_address(core_base, 0x3866510));
    if (friend_receive_hook_created) {
        status = MH_DisableHook(friend_receive_target);
        if (status != MH_OK && status != MH_ERROR_DISABLED) return 600 + status;
    }
#endif
    void* factory_target = reinterpret_cast<void*>(core_address(core_base, 0x6f4c70));
    if (factory_hook_created) {
        status = MH_DisableHook(factory_target);
        if (status != MH_OK && status != MH_ERROR_DISABLED) return 600 + status;
    }
    void* image_factory_target = reinterpret_cast<void*>(core_address(core_base, 0x5240720));
    if (image_download_factory_hook_created) {
        status = MH_DisableHook(image_factory_target);
        if (status != MH_OK && status != MH_ERROR_DISABLED) return 600 + status;
    }
    for (int attempt = 0; (active_callbacks != 0 || live_completions != 0 || live_tasks != 0) && attempt < 100; ++attempt) Sleep(50);
    if (active_callbacks != 0 || live_completions != 0 || live_tasks != 0) return ERROR_BUSY;
    bool hooks_present = hook_created || receive_hook_created || factory_hook_created ||
        image_download_factory_hook_created;
#ifdef WXBOT_RECALL_RECEIVE
    hooks_present = hooks_present || recall_receive_hook_created;
#endif
#ifdef WXBOT_FRIEND_RECEIVE
    hooks_present = hooks_present || friend_receive_hook_created;
#endif
    if (!hooks_present) return 0;
    if (hook_created) {
        status = MH_RemoveHook(target);
        if (status != MH_OK) return 600 + status;
    }
    if (receive_hook_created) {
        status = MH_RemoveHook(receive_target);
        if (status != MH_OK) return 600 + status;
    }
#ifdef WXBOT_RECALL_RECEIVE
    if (recall_receive_hook_created) {
        status = MH_RemoveHook(recall_receive_target);
        if (status != MH_OK) return 600 + status;
    }
#endif
#ifdef WXBOT_FRIEND_RECEIVE
    if (friend_receive_hook_created) {
        status = MH_RemoveHook(friend_receive_target);
        if (status != MH_OK) return 600 + status;
    }
#endif
    if (factory_hook_created) {
        status = MH_RemoveHook(factory_target);
        if (status != MH_OK) return 600 + status;
    }
    if (image_download_factory_hook_created) {
        status = MH_RemoveHook(image_factory_target);
        if (status != MH_OK) return 600 + status;
    }
    status = MH_Uninitialize();
    if (status == MH_OK) {
        hook_created = false; receive_hook_created = false; factory_hook_created = false;
        image_download_factory_hook_created = false;
#ifdef WXBOT_RECALL_RECEIVE
        recall_receive_hook_created = false;
#endif
#ifdef WXBOT_FRIEND_RECEIVE
        friend_receive_hook_created = false;
#endif
        if (factory_tls != TLS_OUT_OF_INDEXES) { TlsFree(factory_tls); factory_tls = TLS_OUT_OF_INDEXES; }
    }
    return status == MH_OK ? 0 : 600 + status;
}

BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, void*) {
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(module);
    return TRUE;
}
