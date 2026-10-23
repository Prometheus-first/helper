#pragma once

#include <cstddef>
#include <cwchar>
#include <new>

// Included by text_bridge.cpp after the shared native allocator/scheduler helpers.
// Every RVA below was inspected in the fingerprint-locked Weixin 4.1.15.13 DLL.
struct ImageDownloadConfig {
    TestConfig base;
    char filekey[1024];
    char aeskey[65];
    uint32_t image_type;
};
static_assert(sizeof(ImageDownloadConfig) == 2656, "Unexpected image download config ABI");
static_assert(offsetof(ImageDownloadConfig, filekey) == 1560, "Unexpected download config file key offset");
static_assert(offsetof(ImageDownloadConfig, aeskey) == 2584, "Unexpected download config AES offset");
static_assert(offsetof(ImageDownloadConfig, image_type) == 2652, "Unexpected download config type offset");

struct ImageDownloadWideString {
    wchar_t data[8];
    uint64_t length;
    uint64_t capacity;
};
static_assert(sizeof(wchar_t) == 2, "Image download requires the Windows WCHAR ABI");
static_assert(sizeof(ImageDownloadWideString) == 32, "Unexpected native wstring ABI");

struct ImageDownloadRequest {
    SmallString task_id;
    SmallString filekey;
    SmallString aeskey;
    uint32_t image_type;
    uint32_t padding;
    ImageDownloadWideString destination;
    uint64_t reserved;
    unsigned char flags[2];
    unsigned char tail_padding[6];
};
static_assert(sizeof(ImageDownloadRequest) == 0x98, "Unexpected image download request ABI");
static_assert(offsetof(ImageDownloadRequest, filekey) == 0x20, "Unexpected file key offset");
static_assert(offsetof(ImageDownloadRequest, aeskey) == 0x40, "Unexpected AES key offset");
static_assert(offsetof(ImageDownloadRequest, image_type) == 0x60, "Unexpected image type offset");
static_assert(offsetof(ImageDownloadRequest, destination) == 0x68, "Unexpected destination offset");
static_assert(offsetof(ImageDownloadRequest, reserved) == 0x88, "Unexpected download size offset");
static_assert(offsetof(ImageDownloadRequest, flags) == 0x90, "Unexpected download flags offset");

struct ImageDownloadNativeStatus {
    uintptr_t vtable;
    int32_t code;
    uint32_t padding;
    SharedMessage details;
};
static_assert(sizeof(ImageDownloadNativeStatus) == 32, "Unexpected native download status ABI");
static_assert(offsetof(ImageDownloadNativeStatus, details) == 0x10, "Unexpected status ownership ABI");

static volatile LONG64 image_download_sequence = 0;
static volatile LONG image_download_admission = 0;

using ImageCdnFactory = void* (__fastcall*)(void*, void*);
static ImageCdnFactory original_image_cdn_factory = nullptr;

static void* __fastcall image_cdn_factory(void* task, void* output) {
    _InterlockedIncrement(&active_callbacks);
    void* result = nullptr;
    __try {
        result = original_image_cdn_factory(task, output);
        // Only our own download tasks use this prefix; the client's requests
        // keep the parameters chosen by its caller.
        const auto object = static_cast<const unsigned char*>(task);
        const auto id = reinterpret_cast<const SmallString*>(object + 0x238);
        static const char prefix[] = "confirm_img_wxbot_";
        if (result && *reinterpret_cast<const uintptr_t*>(object) == core_address(core_base, 0x97ca198) &&
            id->length >= sizeof(prefix) - 1 && id->length < 1024 && id->capacity >= id->length) {
            const char* bytes = id->capacity > 15 ? *reinterpret_cast<const char* const*>(id->data) : id->data;
            if (std::memcmp(bytes, prefix, sizeof(prefix) - 1) == 0) {
                // C2C image keys require app=1, biz=1. The generic task's
                // biz=2 default returns -6101 for real received image keys.
                *reinterpret_cast<int32_t*>(static_cast<unsigned char*>(result) + 0xd4) = 1;
                *reinterpret_cast<int32_t*>(static_cast<unsigned char*>(result) + 0xd8) = 1;
            }
        }
    } __finally { _InterlockedDecrement(&active_callbacks); }
    return result;
}

static DWORD image_download_ensure_factory() {
    if (image_download_factory_hook_created) return ERROR_SUCCESS;
    MH_STATUS code = MH_Initialize();
    const bool initialized_here = code == MH_OK;
    if (code != MH_OK && code != MH_ERROR_ALREADY_INITIALIZED) return 600 + code;
    void* address = reinterpret_cast<void*>(core_address(core_base, 0x5240720));
    code = MH_CreateHook(address, reinterpret_cast<void*>(&image_cdn_factory),
                         reinterpret_cast<void**>(&original_image_cdn_factory));
    if (code == MH_OK) {
        code = MH_EnableHook(address);
        if (code != MH_OK) MH_RemoveHook(address);
    }
    if (code != MH_OK) {
        if (initialized_here) MH_Uninitialize();
        return 600 + code;
    }
    image_download_factory_hook_created = true;
    return ERROR_SUCCESS;
}

static DWORD image_download_copy_config(void* parameter, ImageDownloadConfig* output) {
    if (!parameter || !output) return ERROR_INVALID_PARAMETER;
    __try {
        *output = *static_cast<const ImageDownloadConfig*>(parameter);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return ERROR_INVALID_PARAMETER;
    }
    return ERROR_SUCCESS;
}

static DWORD image_download_decode_path(const ImageDownloadConfig& config, wchar_t (&path)[1024]) {
    const size_t length = strnlen_s(config.base.text, sizeof(config.base.text));
    if (!length || length >= sizeof(config.base.text)) return ERROR_INVALID_PARAMETER;
    if (!MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, config.base.text, -1,
                             path, static_cast<int>(std::size(path))))
        return ERROR_INVALID_PARAMETER;
    const size_t wide_length = std::wcslen(path);
    const bool drive_path = wide_length >= 3 &&
        ((path[0] >= L'A' && path[0] <= L'Z') || (path[0] >= L'a' && path[0] <= L'z')) &&
        path[1] == L':' && (path[2] == L'\\' || path[2] == L'/');
    const bool unc_path = wide_length >= 5 && path[0] == L'\\' && path[1] == L'\\';
    if (!drive_path && !unc_path) return ERROR_INVALID_PARAMETER;
    return ERROR_SUCCESS;
}

static DWORD image_download_require_new_path(const wchar_t* path) {
    if (GetFileAttributesW(path) != INVALID_FILE_ATTRIBUTES) return ERROR_FILE_EXISTS;
    const DWORD error = GetLastError();
    // The Python side creates the containing directory before dispatching.
    return error == ERROR_FILE_NOT_FOUND ? ERROR_SUCCESS : error;
}

static DWORD image_download_validate_config(const ImageDownloadConfig& config) {
    if (config.base.size != sizeof(ImageDownloadConfig) || config.base.pid != GetCurrentProcessId())
        return ERROR_INVALID_PARAMETER;
    const size_t log_length = wcsnlen_s(config.base.log_path, std::size(config.base.log_path));
    const size_t key_length = strnlen_s(config.filekey, sizeof(config.filekey));
    const size_t aes_length = strnlen_s(config.aeskey, sizeof(config.aeskey));
    if (!log_length || log_length >= std::size(config.base.log_path) ||
        !key_length || key_length >= sizeof(config.filekey) ||
        (aes_length != 32 && aes_length != 64) ||
        (config.image_type != 1 && config.image_type != 2 && config.image_type != 3))
        return ERROR_INVALID_PARAMETER;
    for (size_t index = 0; index < aes_length; ++index) {
        const char ch = config.aeskey[index];
        if (!((ch >= '0' && ch <= '9') || (ch >= 'a' && ch <= 'f') ||
              (ch >= 'A' && ch <= 'F')))
            return ERROR_INVALID_PARAMETER;
    }
    wchar_t path[1024]{};
    const DWORD status = image_download_decode_path(config, path);
    if (status != ERROR_SUCCESS) return status;
    if (_wcsicmp(path, config.base.log_path) == 0) return ERROR_INVALID_PARAMETER;
    return image_download_require_new_path(path);
}

static bool image_download_set_wide_string(ImageDownloadWideString* output, const wchar_t* path) {
    const size_t length = std::wcslen(path);
    if (!length || length >= 1024 || output->length != 0 || output->capacity != 7) return false;
    if (length <= 7) {
        std::memcpy(output->data, path, (length + 1) * sizeof(wchar_t));
        output->length = length;
        return true;
    }
    // At most 2048 bytes: below the native allocator's 4096-byte alignment branch.
    using Allocate = void* (__fastcall*)(size_t);
    void* allocation = reinterpret_cast<Allocate>(core_address(core_base, 0x74ab89c))((length + 1) * sizeof(wchar_t));
    if (!allocation) return false;
    std::memcpy(allocation, path, (length + 1) * sizeof(wchar_t));
    std::memcpy(output->data, &allocation, sizeof(allocation));
    output->length = output->capacity = length;
    return true;
}

static void image_download_release_shared(SharedMessage* value, TestResult* report) {
    __try {
        release_shared(*value);
    } __except (record_exception(GetExceptionInformation(), report)) {
        // Continue releasing the independent references if native cleanup faults.
    }
}

static void image_download_destroy_request(ImageDownloadRequest* request, TestResult* report) {
    __try {
        using Destroy = void (__fastcall*)(ImageDownloadRequest*);
        reinterpret_cast<Destroy>(core_address(core_base, 0x2ba7940))(request);
    } __except (record_exception(GetExceptionInformation(), report)) {
    }
}

static DWORD image_download_check_file(const wchar_t* path, uint64_t* byte_count) {
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                              nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (file == INVALID_HANDLE_VALUE) return GetLastError();
    BY_HANDLE_FILE_INFORMATION information{};
    LARGE_INTEGER size{};
    DWORD status = ERROR_SUCCESS;
    if (!GetFileInformationByHandle(file, &information) || !GetFileSizeEx(file, &size)) {
        status = GetLastError();
    } else if ((information.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) || size.QuadPart <= 0) {
        status = ERROR_INVALID_DATA;
    } else {
        *byte_count = static_cast<uint64_t>(size.QuadPart);
    }
    CloseHandle(file);
    return status;
}

static DWORD image_download_staging_path(wchar_t (&path)[1024], TestResult* report) {
    ImageDownloadWideString root{};
    root.capacity = 7;
    DWORD status = ERROR_INVALID_DATA;
    __try {
        using GetRoot = ImageDownloadWideString* (__fastcall*)(ImageDownloadWideString*, uint32_t);
        reinterpret_cast<GetRoot>(core_address(core_base, 0x8edb0))(&root, 2);
        if (!root.length || root.length > 900 || root.capacity < root.length) return status;
        const wchar_t* characters = root.capacity < 8 ? root.data :
            *reinterpret_cast<wchar_t* const*>(root.data);
        if (!characters) return status;
        std::memcpy(path, characters, static_cast<size_t>(root.length) * sizeof(wchar_t));
        path[root.length] = 0;
        const DWORD attributes = GetFileAttributesW(path);
        if (attributes == INVALID_FILE_ATTRIBUTES || !(attributes & FILE_ATTRIBUTE_DIRECTORY))
            return ERROR_PATH_NOT_FOUND;
        unsigned char random[16]{};
        if (BCryptGenRandom(nullptr, random, sizeof(random), BCRYPT_USE_SYSTEM_PREFERRED_RNG) < 0)
            return ERROR_GEN_FAILURE;
        wchar_t suffix[80]{};
        swprintf_s(suffix, L"\\wxbot_img_%02x%02x%02x%02x%02x%02x%02x%02x%02x%02x%02x%02x%02x%02x%02x%02x.bin",
            random[0], random[1], random[2], random[3], random[4], random[5], random[6], random[7],
            random[8], random[9], random[10], random[11], random[12], random[13], random[14], random[15]);
        if (wcscat_s(path, suffix) != 0) return ERROR_BUFFER_OVERFLOW;
        status = image_download_require_new_path(path);
    } __finally {
        __try {
            using DestroyWide = void (__fastcall*)(ImageDownloadWideString*);
            reinterpret_cast<DestroyWide>(core_address(core_base, 0x7a60))(&root);
        } __except (record_exception(GetExceptionInformation(), report)) {
        }
    }
    return status;
}

// Keep this SEH function free of C++ objects with nontrivial destructors.
static DWORD image_download_on_native_task(const ImageDownloadConfig* config,
                                           TestResult* report, uint64_t* byte_count) {
    wchar_t path[1024]{};
    DWORD status = image_download_decode_path(*config, path);
    if (status != ERROR_SUCCESS) return status;
    status = image_download_require_new_path(path);
    if (status != ERROR_SUCCESS) return status;
    // CDN mode 1 accepts only the client's storage roots (090b60/228db10).
    // Download to a unique client-owned path, then copy to the SDK's destination.
    wchar_t staging[1024]{};
    status = image_download_staging_path(staging, report);
    if (status != ERROR_SUCCESS || report->exception) return status;

    ImageDownloadRequest request{};
    request.task_id.capacity = request.aeskey.capacity = request.filekey.capacity = 15;
    request.destination.capacity = 7;
    request.image_type = config->image_type;
    SharedMessage app{}, context{}, service{}, image_task{};
    ImageDownloadNativeStatus native_status{};
    int task_argument = 0;
    int category = 14;
    char task_id[96]{};
    const unsigned long long sequence = static_cast<unsigned long long>(
        _InterlockedIncrement64(&image_download_sequence));
    sprintf_s(task_id, "confirm_img_wxbot_%lu_%llu_%llu", GetCurrentProcessId(),
              static_cast<unsigned long long>(GetTickCount64()), sequence);

    __try {
        report->stage = 31;
        if (!set_native_string(&request.task_id, task_id) ||
            !set_native_string(&request.aeskey, config->aeskey) ||
            !set_native_string(&request.filekey, config->filekey) ||
            !image_download_set_wide_string(&request.destination, staging))
            return ERROR_NOT_ENOUGH_MEMORY;

        using GetApp = SharedMessage* (__fastcall*)(SharedMessage*);
        using GetService = SharedMessage* (__fastcall*)(void*, SharedMessage*);
        using GetDispatcher = void* (__fastcall*)(void*);
        reinterpret_cast<GetApp>(core_address(core_base, 0x47220))(&app);
        if (!app.object || !app.control) return ERROR_NOT_READY;
        reinterpret_cast<GetService>(core_address(core_base, 0x36c740))(app.object, &context);
        if (!context.object || !context.control) return ERROR_NOT_READY;
        reinterpret_cast<GetService>(core_address(core_base, 0x356dd0))(context.object, &service);
        if (!service.object || !service.control) return ERROR_NOT_READY;
        void* dispatcher = reinterpret_cast<GetDispatcher>(core_address(core_base, 0x10e390))(service.object);
        if (!dispatcher) return ERROR_NOT_READY;

        report->stage = 32;
        using MakeTask = SharedMessage* (__fastcall*)(SharedMessage*, const ImageDownloadRequest*, const int*);
        reinterpret_cast<MakeTask>(core_address(core_base, 0x2ba5330))(&image_task, &request, &task_argument);
        if (!image_task.object || !image_task.control) return ERROR_NOT_READY;
        if (*static_cast<const uintptr_t*>(image_task.object) != core_address(core_base, 0x97ca198))
            return ERROR_INVALID_DATA;

        report->stage = 33;
        // Native task dispatch installs its own CDN callbacks and awaits completion.
        // This path has no recipient and never calls a message-send builder.
        using Dispatch = ImageDownloadNativeStatus* (__fastcall*)(
            void*, ImageDownloadNativeStatus*, const int*, const SharedMessage*);
        reinterpret_cast<Dispatch>(core_address(core_base, 0xe5c720))(dispatcher, &native_status, &category, &image_task);
        report->native_status = static_cast<uint32_t>(native_status.code);
        report->stage = 34;
        if (native_status.vtable != core_address(core_base, 0x8dfb4b8)) return ERROR_INVALID_DATA;
        if (native_status.code != 0) return ERROR_GEN_FAILURE;
        if (!CopyFileW(staging, path, TRUE)) return GetLastError();
        return image_download_check_file(path, byte_count);
    } __finally {
        image_download_release_shared(&native_status.details, report);
        image_download_release_shared(&image_task, report);
        image_download_release_shared(&service, report);
        image_download_release_shared(&context, report);
        image_download_release_shared(&app, report);
        image_download_destroy_request(&request, report);
        DeleteFileW(staging);
        wchar_t temporary[1030]{};
        if (swprintf_s(temporary, L"%s.tmp", staging) > 0) DeleteFileW(temporary);
    }
}

static void image_download_write_report(const ImageDownloadConfig* config, const TestResult* report,
                                        DWORD status, uint64_t byte_count) {
    FILE* log = nullptr;
    if (_wfopen_s(&log, config->base.log_path, L"wb") != 0 || !log) return;
    const bool success = status == ERROR_SUCCESS && report->exception == 0 &&
        report->native_status == 0 && byte_count > 0;
    std::fprintf(log, "{\"stage\":%lu,\"exception\":%lu,\"native_status\":%u,\"status\":%lu,"
                     "\"completion_success\":%s,\"operation\":\"image_download\",\"image_path\":",
                 report->stage, report->exception, report->native_status, status,
                 success ? "true" : "false");
    write_json_string(log, config->base.text);
    std::fprintf(log, ",\"bytes_written\":%llu,\"thread_id\":%lu}\n",
                 static_cast<unsigned long long>(byte_count), GetCurrentThreadId());
    std::fclose(log);
}

static void image_download_execute(const ImageDownloadConfig* config) {
    TestResult report{};
    report.stage = 30;
    report.native_status = 0xffffffffu;
    DWORD status = ERROR_GEN_FAILURE;
    uint64_t byte_count = 0;
    _InterlockedIncrement(&active_callbacks);
    __try {
        __try {
            status = image_download_on_native_task(config, &report, &byte_count);
        } __except (record_exception(GetExceptionInformation(), &report)) {
            status = ERROR_GEN_FAILURE;
        }
        if (report.exception && status == ERROR_SUCCESS) status = ERROR_GEN_FAILURE;
        image_download_write_report(config, &report, status, byte_count);
    } __finally {
        _InterlockedDecrement(&active_callbacks);
    }
}

struct ImageDownloadTaskLease {
    ImageDownloadConfig config;
    explicit ImageDownloadTaskLease(const ImageDownloadConfig& value) : config(value) {
        _InterlockedIncrement(&live_tasks);
    }
    ~ImageDownloadTaskLease() { _InterlockedDecrement(&live_tasks); }
};

// Separate C++ ownership from the SEH/native invocation above (MSVC C2712).
static DWORD image_download_schedule(const ImageDownloadConfig& config) {
    try {
        auto lease = std::make_shared<ImageDownloadTaskLease>(config);
        std::function<void()> task = [lease]() { image_download_execute(&lease->config); };
        return schedule_task(config.base, &task);
    } catch (const std::bad_alloc&) {
        return ERROR_NOT_ENOUGH_MEMORY;
    } catch (...) {
        return ERROR_GEN_FAILURE;
    }
}

extern "C" __declspec(dllexport) DWORD WINAPI WxDownloadImage(void* parameter) {
    _InterlockedIncrement(&active_callbacks);
    DWORD status = ERROR_GEN_FAILURE;
    __try {
        if (_InterlockedCompareExchange(&image_download_admission, 1, 0) != 0) return ERROR_BUSY;
        __try {
            if (live_completions != 0 || live_tasks != 0) return ERROR_BUSY;
            ImageDownloadConfig config{};
            status = image_download_copy_config(parameter, &config);
            if (status != ERROR_SUCCESS) return status;
            status = image_download_validate_config(config);
            if (status != ERROR_SUCCESS) return status;
            HMODULE core = GetModuleHandleW(L"Weixin.dll");
            if (!core || !core_fingerprint(core)) return ERROR_NOT_SUPPORTED;
            core_base = reinterpret_cast<uintptr_t>(core);
            status = image_download_ensure_factory();
            if (status != ERROR_SUCCESS) return status;
            status = image_download_schedule(config);
        } __finally {
            _InterlockedExchange(&image_download_admission, 0);
        }
    } __finally {
        _InterlockedDecrement(&active_callbacks);
    }
    return status;
}
