#pragma once

// Included after image_download.h and emoticon_send.h.
// Weixin 4.1.15.13's DownloadFile caller (RVA 2ba9090) uses the same
// 0x98 CDN request as image download, with file type 5 and category 14.
// The ordinary-file request type is set to 5 at RVA 2ba9686.
struct FileDownloadConfig {
    TestConfig base;
    char filekey[1024];
    char aeskey[65];
    uint8_t padding[7];
    uint64_t expected_size; // Require a declared size before starting a download.
    char account[128];
};
static_assert(sizeof(FileDownloadConfig) == 2792, "Unexpected file download config ABI");
static_assert(offsetof(FileDownloadConfig, filekey) == 1560, "Unexpected file key offset");
static_assert(offsetof(FileDownloadConfig, aeskey) == 2584, "Unexpected file AES offset");
static_assert(offsetof(FileDownloadConfig, expected_size) == 2656, "Unexpected file size offset");
static_assert(offsetof(FileDownloadConfig, account) == 2664, "Unexpected file account offset");

static constexpr uint64_t file_download_max_bytes = 0x7fffffffffffffffull;
static volatile LONG64 file_download_sequence = 0;
static volatile LONG file_download_admission = 0;

static DWORD file_download_copy_config(void* parameter, FileDownloadConfig* output) {
    if (!parameter || !output) return ERROR_INVALID_PARAMETER;
    __try { *output = *static_cast<const FileDownloadConfig*>(parameter); }
    __except (EXCEPTION_EXECUTE_HANDLER) { return ERROR_INVALID_PARAMETER; }
    return ERROR_SUCCESS;
}

static DWORD file_download_decode_path(const FileDownloadConfig& config, wchar_t (&path)[1024]) {
    ImageDownloadConfig image_config{};
    image_config.base = config.base;
    DWORD status = image_download_decode_path(image_config, path);
    if (status != ERROR_SUCCESS) return status;
    // Only ordinary local drive paths; no device/UNC or alternate data streams.
    if (!((path[0] >= L'A' && path[0] <= L'Z') || (path[0] >= L'a' && path[0] <= L'z')) ||
        path[1] != L':' || (path[2] != L'\\' && path[2] != L'/') ||
        std::wcschr(path + 2, L':'))
        return ERROR_BAD_PATHNAME;
    return ERROR_SUCCESS;
}

static DWORD file_download_validate_config(const FileDownloadConfig& config) {
    const size_t log_length = wcsnlen_s(config.base.log_path, std::size(config.base.log_path));
    const size_t key_length = strnlen_s(config.filekey, sizeof(config.filekey));
    const size_t aes_length = strnlen_s(config.aeskey, sizeof(config.aeskey));
    const size_t account_length = strnlen_s(config.account, sizeof(config.account));
    if (config.base.size != sizeof(config) || config.base.pid != GetCurrentProcessId() ||
        !log_length || log_length >= std::size(config.base.log_path) ||
        !key_length || key_length >= sizeof(config.filekey) ||
        (aes_length != 32 && aes_length != 64) ||
        !account_length || account_length >= sizeof(config.account) ||
        !valid_target(config.account) || !config.expected_size ||
        config.expected_size > file_download_max_bytes)
        return ERROR_INVALID_PARAMETER;
    for (const uint8_t value : config.padding)
        if (value) return ERROR_INVALID_PARAMETER;
    for (size_t index = 0; index < aes_length; ++index) {
        const char ch = config.aeskey[index];
        if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f') ||
              (ch >= 'A' && ch <= 'F')))
            return ERROR_INVALID_PARAMETER;
    }
    wchar_t path[1024]{};
    const DWORD status = file_download_decode_path(config, path);
    if (status != ERROR_SUCCESS) return status;
    if (_wcsicmp(path, config.base.log_path) == 0) return ERROR_INVALID_PARAMETER;
    return image_download_require_new_path(path);
}

// Validate before copying out of Weixin's staging directory. The fixed cap is
// enforced on declared metadata and the completed file, not by a CDN callback.
static DWORD file_download_check_file(const wchar_t* path, uint64_t expected_size,
                                      uint64_t* byte_count) {
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                              FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    if (file == INVALID_HANDLE_VALUE) return GetLastError();
    BY_HANDLE_FILE_INFORMATION information{};
    LARGE_INTEGER size{};
    DWORD status = ERROR_SUCCESS;
    if (!GetFileInformationByHandle(file, &information) || !GetFileSizeEx(file, &size)) {
        status = GetLastError();
    } else if ((information.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) ||
               size.QuadPart < 0 || static_cast<uint64_t>(size.QuadPart) > file_download_max_bytes ||
               (expected_size && static_cast<uint64_t>(size.QuadPart) != expected_size)) {
        status = ERROR_INVALID_DATA;
    } else {
        *byte_count = static_cast<uint64_t>(size.QuadPart);
    }
    CloseHandle(file);
    return status;
}

// Keep SEH/native calls separate from C++ ownership (MSVC C2712).
static DWORD file_download_on_native_task(const FileDownloadConfig* config,
                                          TestResult* report, uint64_t* byte_count) {
    wchar_t path[1024]{};
    DWORD status = file_download_decode_path(*config, path);
    if (status != ERROR_SUCCESS) return status;
    status = image_download_require_new_path(path);
    if (status != ERROR_SUCCESS) return status;
    wchar_t staging[1024]{};
    status = image_download_staging_path(staging, report);
    if (status != ERROR_SUCCESS || report->exception) return status;

    ImageDownloadRequest request{};
    request.task_id.capacity = request.aeskey.capacity = request.filekey.capacity = 15;
    request.destination.capacity = 7;
    request.image_type = 5; // DownloadFile sets this at RVA 2ba9686.
    SharedMessage app{}, context{}, service{}, file_task{};
    ImageDownloadNativeStatus native_status{};
    int task_argument = 0;
    int category = 14;
    char task_id[96]{};
    const unsigned long long sequence = static_cast<unsigned long long>(
        _InterlockedIncrement64(&file_download_sequence));
    sprintf_s(task_id, "confirm_file_wxbot_%lu_%llu_%llu", GetCurrentProcessId(),
              static_cast<unsigned long long>(GetTickCount64()), sequence);

    __try {
        report->stage = 81;
        if (!set_native_string(&request.task_id, task_id) ||
            !set_native_string(&request.aeskey, config->aeskey) ||
            !set_native_string(&request.filekey, config->filekey) ||
            !image_download_set_wide_string(&request.destination, staging))
            return ERROR_NOT_ENOUGH_MEMORY;
        using GetApp = SharedMessage* (__fastcall*)(SharedMessage*);
        using GetService = SharedMessage* (__fastcall*)(void*, SharedMessage*);
        using GetDispatcher = void* (__fastcall*)(void*);
        reinterpret_cast<GetApp>(core_address(core_base, 0x47220))(&app);
        if (!emoticon_account_matches(app, config->account)) return ERROR_NOT_READY;
        reinterpret_cast<GetService>(core_address(core_base, 0x36c740))(app.object, &context);
        if (!context.object || !context.control) return ERROR_NOT_READY;
        reinterpret_cast<GetService>(core_address(core_base, 0x356dd0))(context.object, &service);
        if (!service.object || !service.control) return ERROR_NOT_READY;
        void* dispatcher = reinterpret_cast<GetDispatcher>(core_address(core_base, 0x10e390))(service.object);
        if (!dispatcher || !emoticon_account_matches(app, config->account)) return ERROR_NOT_READY;

        report->stage = 82;
        using MakeTask = SharedMessage* (__fastcall*)(SharedMessage*, const ImageDownloadRequest*, const int*);
        reinterpret_cast<MakeTask>(core_address(core_base, 0x2ba5330))(&file_task, &request, &task_argument);
        if (!file_task.object || !file_task.control ||
            *static_cast<const uintptr_t*>(file_task.object) != core_address(core_base, 0x97ca198))
            return ERROR_INVALID_DATA;
        if (!emoticon_account_matches(app, config->account)) return ERROR_NOT_READY;
        report->stage = 83;
        using Dispatch = ImageDownloadNativeStatus* (__fastcall*)(
            void*, ImageDownloadNativeStatus*, const int*, const SharedMessage*);
        reinterpret_cast<Dispatch>(core_address(core_base, 0xe5c720))(dispatcher, &native_status, &category, &file_task);
        report->native_status = static_cast<uint32_t>(native_status.code);
        report->stage = 84;
        if (native_status.vtable != core_address(core_base, 0x8dfb4b8)) return ERROR_INVALID_DATA;
        if (native_status.code != 0) return ERROR_GEN_FAILURE;
        if (!emoticon_account_matches(app, config->account)) return ERROR_NOT_READY;
        status = file_download_check_file(staging, config->expected_size, byte_count);
        if (status != ERROR_SUCCESS) return status;
        if (!CopyFileW(staging, path, TRUE)) return GetLastError();
        status = file_download_check_file(path, config->expected_size, byte_count);
        if (status != ERROR_SUCCESS) DeleteFileW(path);
        return status;
    } __finally {
        image_download_release_shared(&native_status.details, report);
        image_download_release_shared(&file_task, report);
        image_download_release_shared(&service, report);
        image_download_release_shared(&context, report);
        image_download_release_shared(&app, report);
        image_download_destroy_request(&request, report);
        DeleteFileW(staging);
        wchar_t temporary[1030]{};
        if (swprintf_s(temporary, L"%s.tmp", staging) > 0) DeleteFileW(temporary);
    }
}

static void file_download_write_report(const FileDownloadConfig* config, const TestResult* report,
                                       DWORD status, uint64_t byte_count) {
    FILE* log = nullptr;
    if (_wfopen_s(&log, config->base.log_path, L"wb") != 0 || !log) return;
    const bool success = status == ERROR_SUCCESS && !report->exception && !report->native_status;
    std::fprintf(log, "{\"operation\":\"file_download\",\"stage\":%lu,\"exception\":%lu,"
                     "\"native_status\":%u,\"status\":%lu,\"completion_success\":%s,\"file_path\":",
                 report->stage, report->exception, report->native_status, status,
                 success ? "true" : "false");
    write_json_string(log, config->base.text);
    std::fprintf(log, ",\"file_size\":%llu,\"bytes_written\":%llu,\"thread_id\":%lu}\n",
                 static_cast<unsigned long long>(byte_count),
                 static_cast<unsigned long long>(byte_count), GetCurrentThreadId());
    std::fclose(log);
}

static void file_download_execute(const FileDownloadConfig* config) {
    TestResult report{};
    report.stage = 80;
    report.native_status = 0xffffffffu;
    DWORD status = ERROR_GEN_FAILURE;
    uint64_t byte_count = 0;
    _InterlockedIncrement(&active_callbacks);
    __try {
        __try { status = file_download_on_native_task(config, &report, &byte_count); }
        __except (record_exception(GetExceptionInformation(), &report)) { status = ERROR_GEN_FAILURE; }
        if (report.exception && status == ERROR_SUCCESS) status = ERROR_GEN_FAILURE;
        file_download_write_report(config, &report, status, byte_count);
    } __finally { _InterlockedDecrement(&active_callbacks); }
}

struct FileDownloadTaskLease {
    FileDownloadConfig config;
    explicit FileDownloadTaskLease(const FileDownloadConfig& value) : config(value) {
        _InterlockedIncrement(&live_tasks);
    }
    ~FileDownloadTaskLease() { _InterlockedDecrement(&live_tasks); }
};

static DWORD file_download_schedule(const FileDownloadConfig& config) {
    try {
        auto lease = std::make_shared<FileDownloadTaskLease>(config);
        std::function<void()> task = [lease]() { file_download_execute(&lease->config); };
        return schedule_task(config.base, &task);
    } catch (const std::bad_alloc&) { return ERROR_NOT_ENOUGH_MEMORY; }
    catch (...) { return ERROR_GEN_FAILURE; }
}

extern "C" __declspec(dllexport) DWORD WINAPI WxDownloadFile(void* parameter) {
    _InterlockedIncrement(&active_callbacks);
    DWORD status = ERROR_GEN_FAILURE;
    __try {
        if (_InterlockedCompareExchange(&file_download_admission, 1, 0) != 0) return ERROR_BUSY;
        __try {
            if (live_completions || live_tasks) return ERROR_BUSY;
            FileDownloadConfig config{};
            status = file_download_copy_config(parameter, &config);
            if (status != ERROR_SUCCESS) return status;
            status = file_download_validate_config(config);
            if (status != ERROR_SUCCESS) return status;
            HMODULE core = GetModuleHandleW(L"Weixin.dll");
            if (!core || !core_fingerprint(core)) return ERROR_NOT_SUPPORTED;
            core_base = reinterpret_cast<uintptr_t>(core);
            // The built-in file type selects its native CDN app/biz itself;
            // no image factory hook is installed for ordinary files.
            status = file_download_schedule(config);
        } __finally { _InterlockedExchange(&file_download_admission, 0); }
    } __finally { _InterlockedDecrement(&active_callbacks); }
    return status;
}
