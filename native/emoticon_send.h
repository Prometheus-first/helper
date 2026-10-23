#pragma once

// Included after the common task, allocator, target and image helpers.
// Fingerprint-locked Weixin 4.1.15.13: 54973d0's GIF/type-47 branch.
struct EmoticonConfig {
    TextConfig base;
    char account[128];
    uint32_t send;
    uint32_t reserved;
};
static_assert(sizeof(EmoticonConfig) == 1824, "Unexpected emoticon config ABI");
static_assert(offsetof(EmoticonConfig, account) == 1688, "Unexpected emoticon account offset");
static_assert(offsetof(EmoticonConfig, send) == 1816, "Unexpected emoticon mode offset");

struct EmoticonBorrowedWide {
    const wchar_t* data;
    uint64_t unused;
    uint64_t length;
    uint64_t capacity;
};
static_assert(sizeof(EmoticonBorrowedWide) == 32, "Unexpected native wstring ABI");

struct EmoticonFileProbe {
    uint8_t valid;
    uint8_t padding[7];
    unsigned char image[16];
    uint32_t width;
    uint32_t height;
};
static_assert(sizeof(EmoticonFileProbe) == 32, "Unexpected emoticon probe ABI");
static_assert(offsetof(EmoticonFileProbe, width) == 0x18, "Unexpected emoticon dimensions");

static volatile LONG emoticon_admission = 0;

static bool emoticon_account_matches(const SharedMessage& context, const char* account) {
    const auto root = *reinterpret_cast<const uintptr_t*>(core_address(core_base, 0xb912690));
    if (!root || *reinterpret_cast<const uintptr_t*>(root) != core_address(core_base, 0x8d804e8) ||
        !context.object || !context.control ||
        *reinterpret_cast<void* const*>(root + 0x68) != context.object ||
        *static_cast<const uintptr_t*>(context.object) != core_address(core_base, 0x8dfe2f8))
        return false;
    const auto identity = reinterpret_cast<const SmallString*>(
        static_cast<const unsigned char*>(context.object) + 0x48);
    const size_t length = strnlen_s(account, 128);
    if (!length || length >= 128 || identity->length != length ||
        identity->capacity < length || identity->capacity > 1024 * 1024)
        return false;
    const char* bytes = identity->capacity > 15 ?
        *reinterpret_cast<const char* const*>(identity->data) : identity->data;
    return bytes && std::memcmp(bytes, account, length) == 0 &&
        *reinterpret_cast<const uintptr_t*>(core_address(core_base, 0xb912690)) == root &&
        *reinterpret_cast<void* const*>(root + 0x68) == context.object;
}

static DWORD emoticon_check_file(const wchar_t* path) {
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, nullptr,
                              OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return GetLastError();
    BY_HANDLE_FILE_INFORMATION information{};
    LARGE_INTEGER size{};
    unsigned char header[10]{};
    DWORD read = 0;
    DWORD status = ERROR_SUCCESS;
    if (!GetFileInformationByHandle(file, &information) || !GetFileSizeEx(file, &size) ||
        !ReadFile(file, header, sizeof(header), &read, nullptr)) {
        status = GetLastError();
    } else if ((information.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) ||
               size.QuadPart <= 0 || size.QuadPart > 1024 * 1024 || read != sizeof(header) ||
               (std::memcmp(header, "GIF87a", 6) && std::memcmp(header, "GIF89a", 6))) {
        status = ERROR_INVALID_DATA;
    } else {
        const unsigned width = header[6] | (static_cast<unsigned>(header[7]) << 8);
        const unsigned height = header[8] | (static_cast<unsigned>(header[9]) << 8);
        if (!width || !height || width > 1024 || height > 1024) status = ERROR_INVALID_DATA;
    }
    CloseHandle(file);
    return status;
}

static void emoticon_probe_release(EmoticonFileProbe* probe, TestResult* report) {
    __try {
        // This image uses one intrusive reference count, not shared_ptr's two.
        using Release = void (__fastcall*)(void*);
        reinterpret_cast<Release>(core_address(core_base, 0x4e6360))(probe->image);
    } __except (record_exception(GetExceptionInformation(), report)) {
    }
}

static void emoticon_write_report(const EmoticonConfig& config, const TestResult& report,
                                  DWORD status, int validation, const EmoticonFileProbe& probe,
                                  bool sent) {
    FILE* log = nullptr;
    if (_wfopen_s(&log, config.base.base.log_path, L"wb") != 0 || !log) return;
    const bool success = status == ERROR_SUCCESS && report.exception == 0 &&
        report.native_status == 0 && report.native_status_high == 0;
    std::fprintf(log,
        "{\"operation\":\"%s\",\"sent\":%s,\"message_type\":47,\"stage\":%lu,"
        "\"exception\":%lu,\"status\":%lu,\"native_status\":%u,\"native_status_high\":%u,"
        "\"validation\":%d,\"width\":%u,\"height\":%u,\"completion_success\":%s,\"target\":",
        config.send ? "emoticon_send" : "emoticon_probe", sent ? "true" : "false",
        report.stage, report.exception, status, report.native_status, report.native_status_high,
        validation, probe.width, probe.height, success ? "true" : "false");
    write_json_string(log, config.base.target);
    std::fputs(",\"text\":", log);
    write_json_string(log, config.base.base.text);
    std::fputs(",\"emoticon_path\":", log);
    write_json_string(log, config.base.base.text);
    std::fputs("}\n", log);
    std::fclose(log);
}

// Keep native SEH boundaries separate from C++ lease and callable ownership.
static void emoticon_on_task(const EmoticonConfig& config) {
    SharedMessage context{}, provider{}, manager{}, message{};
    alignas(16) unsigned char result[128]{};
    QtStaticUtf16 utf16{};
    EmoticonFileProbe probe{};
    TestResult report{};
    report.native_status = 0xffffffffu;
    DWORD status = ERROR_GEN_FAILURE;
    int validation = 0;
    bool probe_initialized = false;
    bool sent = false;
    _InterlockedIncrement(&active_callbacks);
    __try {
        __try {
            report.stage = 40;
            if (!image_file_argument(config.base, &utf16)) {
                status = ERROR_FILE_NOT_FOUND;
                __leave;
            }
            status = emoticon_check_file(utf16.characters);
            if (status != ERROR_SUCCESS) __leave;
            EmoticonBorrowedWide path{utf16.characters, 0, static_cast<uint64_t>(utf16.length), 1023};
            using GetContext = SharedMessage* (__fastcall*)(SharedMessage*);
            using GetService = SharedMessage* (__fastcall*)(void*, SharedMessage*);
            reinterpret_cast<GetContext>(core_address(core_base, 0x47220))(&context);
            if (!emoticon_account_matches(context, config.account)) {
                status = ERROR_NOT_READY;
                __leave;
            }

            report.stage = 41;
            using Validate = int (__fastcall*)(const EmoticonBorrowedWide*);
            validation = reinterpret_cast<Validate>(core_address(core_base, 0xe7a8e0))(&path);
            if (validation != 2) {
                status = ERROR_NOT_SUPPORTED;
                __leave;
            }
            using Probe = EmoticonFileProbe* (__fastcall*)(EmoticonFileProbe*, const EmoticonBorrowedWide*);
            probe_initialized = true;
            reinterpret_cast<Probe>(core_address(core_base, 0x5309c0))(&probe, &path);
            if (!probe.valid || !probe.width || !probe.height ||
                probe.width > 1024 || probe.height > 1024) {
                status = ERROR_INVALID_DATA;
                __leave;
            }

            report.stage = 42;
            using Factory = SharedMessage* (__fastcall*)(SharedMessage*);
            reinterpret_cast<Factory>(core_address(core_base, 0x1bfb1c0))(&message);
            if (!message.object || !message.control ||
                *static_cast<const uintptr_t*>(message.object) != core_address(core_base, 0x90daff8)) {
                status = ERROR_INVALID_DATA;
                __leave;
            }
            auto object = static_cast<unsigned char*>(message.object);
            *reinterpret_cast<uint32_t*>(object + 0x118) = 47;
            *reinterpret_cast<uint64_t*>(object + 0x778) =
                static_cast<uint64_t>(probe.width) | (static_cast<uint64_t>(probe.height) << 32);
            using CopyWide = void* (__fastcall*)(void*, const EmoticonBorrowedWide*);
            reinterpret_cast<CopyWide>(core_address(core_base, 0x9b5d0))(object + 0x120, &path);
            if (!set_native_string(reinterpret_cast<SmallString*>(object + 0xb0), config.base.target)) {
                status = ERROR_NOT_ENOUGH_MEMORY;
                __leave;
            }
            if (!emoticon_account_matches(context, config.account)) {
                status = ERROR_NOT_READY;
                __leave;
            }

            if (!config.send) {
                report.stage = 43;
                report.native_status = 0;
                status = ERROR_SUCCESS;
                __leave;
            }

            report.stage = 44;
            reinterpret_cast<GetService>(core_address(core_base, 0x36c740))(context.object, &provider);
            if (!provider.object || !provider.control) {
                status = ERROR_NOT_READY;
                __leave;
            }
            reinterpret_cast<GetService>(core_address(core_base, 0x6f4d90))(provider.object, &manager);
            if (!manager.object || !manager.control ||
                !emoticon_account_matches(context, config.account)) {
                status = ERROR_NOT_READY;
                __leave;
            }
            SharedMessage transferred = message;
            _InterlockedIncrement(reinterpret_cast<volatile LONG*>(
                static_cast<unsigned char*>(message.control) + 8));
            reinterpret_cast<SendFunction>(core_address(core_base, 0x178f100))(manager.object, result, &transferred, 1);
            // The callee consumes this copy; keep and release our factory reference.
            transferred = {};
            report.native_status = *reinterpret_cast<const uint32_t*>(result);
            report.native_status_high = *reinterpret_cast<const uint32_t*>(result + 4);
            sent = report.native_status == 0 && report.native_status_high == 0;
            report.stage = 45;
            status = sent ? ERROR_SUCCESS : ERROR_GEN_FAILURE;
        } __except (record_exception(GetExceptionInformation(), &report)) {
            status = ERROR_GEN_FAILURE;
        }
        guarded_release_shared(reinterpret_cast<SharedMessage*>(result + 40), &report);
        image_free_string(reinterpret_cast<SmallString*>(result + 8), &report);
        guarded_release_shared(&message, &report);
        if (probe_initialized) emoticon_probe_release(&probe, &report);
        guarded_release_shared(&manager, &report);
        guarded_release_shared(&provider, &report);
        guarded_release_shared(&context, &report);
        emoticon_write_report(config, report, status, validation, probe, sent);
    } __finally {
        _InterlockedDecrement(&active_callbacks);
    }
}

struct EmoticonTaskLease {
    EmoticonConfig config;
    explicit EmoticonTaskLease(const EmoticonConfig& value) : config(value) {
        _InterlockedIncrement(&live_tasks);
    }
    ~EmoticonTaskLease() { _InterlockedDecrement(&live_tasks); }
};

static DWORD emoticon_schedule(const EmoticonConfig& config) {
    try {
        auto lease = std::make_shared<EmoticonTaskLease>(config);
        std::function<void()> task = [lease]() { emoticon_on_task(lease->config); };
        return schedule_task(config.base.base, &task);
    } catch (const std::bad_alloc&) {
        return ERROR_NOT_ENOUGH_MEMORY;
    } catch (...) {
        return ERROR_GEN_FAILURE;
    }
}

static DWORD emoticon_copy_config(void* parameter, EmoticonConfig* output) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    __try {
        *output = *static_cast<const EmoticonConfig*>(parameter);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return ERROR_INVALID_PARAMETER;
    }
    return ERROR_SUCCESS;
}

static DWORD emoticon_dispatch(void* parameter, bool send) {
    if (live_tasks || live_completions) return ERROR_BUSY;
    EmoticonConfig config{};
    DWORD status = emoticon_copy_config(parameter, &config);
    if (status != ERROR_SUCCESS) return status;
    const size_t path_length = strnlen_s(config.base.base.text, sizeof(config.base.base.text));
    const size_t target_length = strnlen_s(config.base.target, sizeof(config.base.target));
    const size_t account_length = strnlen_s(config.account, sizeof(config.account));
    const size_t log_length = wcsnlen_s(config.base.base.log_path, std::size(config.base.base.log_path));
    if (config.base.base.size != sizeof(config) || config.base.base.pid != GetCurrentProcessId() ||
        config.send != (send ? 1u : 0u) || config.reserved || !path_length ||
        path_length >= sizeof(config.base.base.text) || !target_length ||
        target_length >= sizeof(config.base.target) || !account_length ||
        account_length >= sizeof(config.account) || !log_length ||
        log_length >= std::size(config.base.base.log_path) || !valid_target(config.base.target) ||
        !valid_target(config.account) || !std::strncmp(config.account, "gh_", 3) ||
        std::strchr(config.account, '@') || !std::strcmp(config.account, "filehelper"))
        return ERROR_INVALID_PARAMETER;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return ERROR_NOT_SUPPORTED;
    core_base = reinterpret_cast<uintptr_t>(core);
    return emoticon_schedule(config);
}

static DWORD emoticon_export(void* parameter, bool send) {
    _InterlockedIncrement(&active_callbacks);
    bool admitted = false;
    __try {
        if (_InterlockedCompareExchange(&emoticon_admission, 1, 0) != 0) return ERROR_BUSY;
        admitted = true;
        return emoticon_dispatch(parameter, send);
    } __finally {
        if (admitted) _InterlockedExchange(&emoticon_admission, 0);
        _InterlockedDecrement(&active_callbacks);
    }
}

extern "C" __declspec(dllexport) DWORD WINAPI WxInspectEmoticonPath(void* parameter) {
    return emoticon_export(parameter, false);
}

extern "C" __declspec(dllexport) DWORD WINAPI WxSendEmoticon(void* parameter) {
    return emoticon_export(parameter, true);
}
