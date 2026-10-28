#pragma once
#include <vector>

// Fingerprint-locked Weixin 4.1.15.13. Ordinary-file branch at 5c9bf98:
// factory 2bab360 / ctor 766780, type 49 with app subtype 6.
struct FileConfig {
    TextConfig base;
    char account[128];
    uint32_t send;
    uint32_t reserved;
};
static_assert(sizeof(FileConfig) == 1824, "Unexpected file config ABI");
static_assert(offsetof(FileConfig, account) == 1688, "Unexpected file account offset");
static_assert(offsetof(FileConfig, send) == 1816, "Unexpected file mode offset");

struct FileWideString {
    wchar_t data[8]{};
    uint64_t length = 0;
    uint64_t capacity = 7;
};
static_assert(sizeof(FileWideString) == 32, "Unexpected native wide string ABI");

struct FileTaskLease {
    FileConfig config;
    HANDLE source = INVALID_HANDLE_VALUE;
    uint64_t size = 0;
    char name[1024]{};
    explicit FileTaskLease(const FileConfig& value) : config(value) {
        _InterlockedIncrement(&live_tasks);
    }
    ~FileTaskLease() {
        if (source != INVALID_HANDLE_VALUE) CloseHandle(source);
        _InterlockedDecrement(&live_tasks);
    }
};

static volatile LONG file_send_admission = 0;

static DWORD file_read_source(FileTaskLease& lease) {
    QtStaticUtf16 path{};
    if (!image_file_argument(lease.config.base, &path)) return ERROR_FILE_NOT_FOUND;
    if (!((path.characters[0] >= L'A' && path.characters[0] <= L'Z') ||
          (path.characters[0] >= L'a' && path.characters[0] <= L'z')) ||
        path.characters[1] != L':' ||
        (path.characters[2] != L'\\' && path.characters[2] != L'/') ||
        wcschr(path.characters + 2, L':')) return ERROR_BAD_PATHNAME;
    wchar_t drive[] = {path.characters[0], L':', L'\\', 0};
    if (GetDriveTypeW(drive) == DRIVE_REMOTE) return ERROR_BAD_PATHNAME;
    const char* name = lease.config.base.base.text;
    for (const char* cursor = name; *cursor; ++cursor)
        if (*cursor == '\\' || *cursor == '/') name = cursor + 1;
    if (!*name || !std::strcmp(name, ".") || !std::strcmp(name, ".."))
        return ERROR_INVALID_NAME;
    strcpy_s(lease.name, name);
    // Retain a read handle across the asynchronous callback. Sharing deletion
    // allows the Python snapshot to be cleaned up after a timeout; denying
    // write sharing keeps the bytes stable until the copy completes.
    lease.source = CreateFileW(path.characters, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE,
        nullptr, OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    if (lease.source == INVALID_HANDLE_VALUE) return GetLastError();
    BY_HANDLE_FILE_INFORMATION information{};
    LARGE_INTEGER size{};
    if (GetFileType(lease.source) != FILE_TYPE_DISK ||
        !GetFileInformationByHandle(lease.source, &information) ||
        !GetFileSizeEx(lease.source, &size)) return ERROR_INVALID_DATA;
    if (information.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT) ||
        size.QuadPart <= 0) return ERROR_INVALID_DATA;
    lease.size = static_cast<uint64_t>(size.QuadPart);
    return ERROR_SUCCESS;
}

static const wchar_t* file_wide_data(const FileWideString& value) {
    if (!value.length || value.length >= 1024 || value.capacity < value.length ||
        value.capacity > 1024 * 1024) return nullptr;
    const wchar_t* data = value.capacity > 7 ?
        *reinterpret_cast<const wchar_t* const*>(value.data) : value.data;
    return data && !data[value.length] ? data : nullptr;
}

static void file_release_wide(FileWideString* value, TestResult* report) {
    __try {
        using Destroy = void (__fastcall*)(void*);
        reinterpret_cast<Destroy>(core_address(core_base, 0x7a60))(value);
    } __except (record_exception(GetExceptionInformation(), report)) {}
}

static void file_write_report(const FileTaskLease& lease, const TestResult& report,
                              DWORD status, bool sent, const char* prepared) {
    FILE* log = nullptr;
    const auto& config = lease.config;
    if (_wfopen_s(&log, config.base.base.log_path, L"wb") || !log) return;
    const bool success = !status && !report.exception &&
        !report.native_status && !report.native_status_high;
    std::fprintf(log, "{\"operation\":\"%s\",\"message_type\":49,\"app_type\":6,"
        "\"sent\":%s,\"stage\":%lu,\"exception\":%lu,\"status\":%lu,"
        "\"native_status\":%u,\"native_status_high\":%u,\"bytes\":%llu,"
        "\"completion_success\":%s,\"target\":",
        config.send ? "file_send" : "file_probe", sent ? "true" : "false",
        report.stage, report.exception, status, report.native_status, report.native_status_high,
        static_cast<unsigned long long>(lease.size), success ? "true" : "false");
    write_json_string(log, config.base.target);
    std::fputs(",\"text\":", log);
    write_json_string(log, config.base.base.text);
    std::fputs(",\"file_path\":", log);
    write_json_string(log, config.base.base.text);
    std::fputs(",\"file_name\":", log);
    write_json_string(log, lease.name);
    std::fputs(",\"prepared_path\":", log);
    write_json_string(log, prepared);
    std::fputs("}\n", log);
    std::fclose(log);
}

static void file_send_on_task(const FileTaskLease& lease) {
    const FileConfig& config = lease.config;
    SharedMessage context{}, provider{}, manager{}, message{};
    FileWideString directory{}, prepared{};
    SmallString token{};
    token.capacity = 15;
    alignas(16) unsigned char result[128]{};
    char prepared_utf8[3072]{};
    wchar_t prepared_copy[1024]{};
    HANDLE staged_handle = INVALID_HANDLE_VALUE;
    bool staged_created = false, handed_to_manager = false, sent = false;
    TestResult report{};
    report.native_status = 0xffffffffu;
    DWORD status = ERROR_GEN_FAILURE;
    _InterlockedIncrement(&active_callbacks);
    __try {
        __try {
            report.stage = 70;
            using GetContext = SharedMessage* (__fastcall*)(SharedMessage*);
            using GetService = SharedMessage* (__fastcall*)(void*, SharedMessage*);
            reinterpret_cast<GetContext>(core_address(core_base, 0x47220))(&context);
            if (!emoticon_account_matches(context, config.account)) {
                status = ERROR_NOT_READY; __leave;
            }
            // Allocate a fresh path in this account's Weixin InputTemp directory.
            // This is the same owned staging-path allocator as the image factory.
            using GetDirectory = FileWideString* (__fastcall*)(FileWideString*);
            using EnsureDirectory = uint32_t (__fastcall*)(const FileWideString*);
            using MakeToken = SmallString* (__fastcall*)(SmallString*);
            using Join = FileWideString* (__fastcall*)(const FileWideString*, FileWideString*, const void*);
            reinterpret_cast<GetDirectory>(core_address(core_base, 0x22d8390))(&directory);
            if (!file_wide_data(directory) ||
                !reinterpret_cast<EnsureDirectory>(core_address(core_base, 0x389830))(&directory)) {
                status = ERROR_PATH_NOT_FOUND; __leave;
            }
            reinterpret_cast<MakeToken>(core_address(core_base, 0x57fe90))(&token);
            if (!token.length || token.length >= 128 || token.capacity < token.length) {
                status = ERROR_INVALID_DATA; __leave;
            }
            const char* token_data = token.capacity > 15 ?
                *reinterpret_cast<const char* const*>(token.data) : token.data;
            struct StringView { const char* data; uint64_t length; } token_view{token_data, token.length};
            reinterpret_cast<Join>(core_address(core_base, 0x9f4b0))(&directory, &prepared, &token_view);
            const wchar_t* path = file_wide_data(prepared);
            const wchar_t* root = file_wide_data(directory);
            if (!path || !root || prepared.length <= directory.length ||
                wcsncmp(path, root, static_cast<size_t>(directory.length)) ||
                (path[directory.length] != L'\\' && path[directory.length] != L'/')) {
                status = ERROR_INVALID_DATA; __leave;
            }
            wcscpy_s(prepared_copy, path);
            if (!WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, path, -1,
                prepared_utf8, sizeof(prepared_utf8), nullptr, nullptr)) {
                status = ERROR_INVALID_DATA; __leave;
            }
            report.stage = 71;
            using Factory = SharedMessage* (__fastcall*)(SharedMessage*);
            reinterpret_cast<Factory>(core_address(core_base, 0x2bab360))(&message);
            if (!message.object || !message.control ||
                *static_cast<const uintptr_t*>(message.object) != core_address(core_base, 0x8e53718)) {
                status = ERROR_INVALID_DATA; __leave;
            }
            auto object = static_cast<unsigned char*>(message.object);
            *reinterpret_cast<uint64_t*>(object + 0x118) = 0x600000031ull;
            using CopyWide = void* (__fastcall*)(void*, const FileWideString*);
            reinterpret_cast<CopyWide>(core_address(core_base, 0x9b5d0))(object + 0x120, &prepared);
            *reinterpret_cast<uint64_t*>(object + 0x1c8) = lease.size;
            if (!set_native_string(reinterpret_cast<SmallString*>(object + 0x1a8), lease.name) ||
                !set_native_string(reinterpret_cast<SmallString*>(object + 0xb0), config.base.target)) {
                status = ERROR_NOT_ENOUGH_MEMORY; __leave;
            }
            report.stage = 72;
            staged_handle = CreateFileW(prepared_copy, GENERIC_WRITE, FILE_SHARE_READ, nullptr,
                CREATE_NEW, FILE_ATTRIBUTE_NORMAL, nullptr);
            if (staged_handle == INVALID_HANDLE_VALUE) { status = GetLastError(); __leave; }
            staged_created = true;
            unsigned char chunk[65536]{};
            uint64_t copied = 0;
            while (copied < lease.size) {
                const DWORD requested = static_cast<DWORD>(std::min<uint64_t>(sizeof(chunk), lease.size - copied));
                DWORD read = 0, written = 0;
                if (!ReadFile(lease.source, chunk, requested, &read, nullptr) || read != requested ||
                    !WriteFile(staged_handle, chunk, read, &written, nullptr) || written != read) {
                    status = ERROR_WRITE_FAULT; __leave;
                }
                copied += read;
            }
            CloseHandle(staged_handle);
            staged_handle = INVALID_HANDLE_VALUE;
            if (!emoticon_account_matches(context, config.account)) {
                status = ERROR_NOT_READY; __leave;
            }
            if (!config.send) {
                report.stage = 73;
                report.native_status = 0;
                status = ERROR_SUCCESS; __leave;
            }
            report.stage = 74;
            reinterpret_cast<GetService>(core_address(core_base, 0x36c740))(context.object, &provider);
            if (!provider.object || !provider.control) { status = ERROR_NOT_READY; __leave; }
            reinterpret_cast<GetService>(core_address(core_base, 0x6f4d90))(provider.object, &manager);
            if (!manager.object || !manager.control || !emoticon_account_matches(context, config.account)) {
                status = ERROR_NOT_READY; __leave;
            }
            SharedMessage transferred = message;
            _InterlockedIncrement(reinterpret_cast<volatile LONG*>(
                static_cast<unsigned char*>(message.control) + 8));
            handed_to_manager = true; // The uploader may retain InputTemp after this call returns.
            reinterpret_cast<SendFunction>(core_address(core_base, 0x178f100))(manager.object, result, &transferred, 1);
            transferred = {};
            report.native_status = *reinterpret_cast<const uint32_t*>(result);
            report.native_status_high = *reinterpret_cast<const uint32_t*>(result + 4);
            sent = !report.native_status && !report.native_status_high;
            report.stage = 75;
            status = sent ? ERROR_SUCCESS : ERROR_GEN_FAILURE;
        } __except (record_exception(GetExceptionInformation(), &report)) {
            status = ERROR_GEN_FAILURE;
        }
        if (staged_handle != INVALID_HANDLE_VALUE) CloseHandle(staged_handle);
        if (staged_created && !handed_to_manager) DeleteFileW(prepared_copy);
        guarded_release_shared(reinterpret_cast<SharedMessage*>(result + 40), &report);
        image_free_string(reinterpret_cast<SmallString*>(result + 8), &report);
        guarded_release_shared(&message, &report);
        guarded_release_shared(&manager, &report);
        guarded_release_shared(&provider, &report);
        guarded_release_shared(&context, &report);
        image_free_string(&token, &report);
        file_release_wide(&prepared, &report);
        file_release_wide(&directory, &report);
        file_write_report(lease, report, status, sent, prepared_utf8);
    } __finally { _InterlockedDecrement(&active_callbacks); }
}

static DWORD file_copy_config(void* parameter, FileConfig* config) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    __try { *config = *static_cast<const FileConfig*>(parameter); }
    __except (EXCEPTION_EXECUTE_HANDLER) { return ERROR_INVALID_PARAMETER; }
    return ERROR_SUCCESS;
}

static DWORD file_send_dispatch(void* parameter, bool send) {
    if (live_tasks || live_completions) return ERROR_BUSY;
    FileConfig config{};
    DWORD status = file_copy_config(parameter, &config);
    if (status) return status;
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
    try {
        auto lease = std::make_shared<FileTaskLease>(config);
        // The queued callback owns the open snapshot handle even after timeout.
        status = file_read_source(*lease);
        if (status) return status;
        std::function<void()> task = [lease]() { file_send_on_task(*lease); };
        return schedule_task(config.base.base, &task);
    } catch (const std::bad_alloc&) { return ERROR_NOT_ENOUGH_MEMORY; }
    catch (...) { return ERROR_GEN_FAILURE; }
}

static DWORD file_send_export(void* parameter, bool send) {
    _InterlockedIncrement(&active_callbacks);
    bool admitted = false;
    __try {
        if (_InterlockedCompareExchange(&file_send_admission, 1, 0) != 0) return ERROR_BUSY;
        admitted = true;
        return file_send_dispatch(parameter, send);
    } __finally {
        if (admitted) _InterlockedExchange(&file_send_admission, 0);
        _InterlockedDecrement(&active_callbacks);
    }
}

extern "C" __declspec(dllexport) DWORD WINAPI WxInspectFilePath(void* parameter) {
    return file_send_export(parameter, false);
}
extern "C" __declspec(dllexport) DWORD WINAPI WxSendFile(void* parameter) {
    return file_send_export(parameter, true);
}
