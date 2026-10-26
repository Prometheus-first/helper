#pragma once
#include <memory>
#include <functional>
#include <new>
#include <cwchar>

// 微信 4.1.15.56 原生朋友圈发布。版本指纹、关键入口和账号均须匹配。
// 已实测仅自己可见的纯文字及双图发布，并核对手机端实际结果。
// WxInspectMoments 只构造/核对；可能写入本地图片缓存，不调用发布接口。
#ifndef WXBOT_MOMENTS_ENABLE_CANDIDATE_SUBMIT
#define WXBOT_MOMENTS_ENABLE_CANDIDATE_SUBMIT 1
#endif
#ifndef WXBOT_MOMENTS_ENABLE_EVENT_PROBE
#define WXBOT_MOMENTS_ENABLE_EVENT_PROBE 0
#endif
#if defined(_ITERATOR_DEBUG_LEVEL) && _ITERATOR_DEBUG_LEVEL != 0
#error Moments candidate requires the same release MSVC STL ABI as Weixin.
#endif

struct MomentsConfig {
    TestConfig base;                 // base.size=sizeof(MomentsConfig); base.text 必须为空
    char self_wxid[128];
    uint32_t visibility;             // 1=public，2=private
    uint32_t image_count;
    uint32_t timeout_ms;             // 10000..300000；超时不自动重发
    uint32_t reserved;
    wchar_t image_paths[9][1024];    // 绝对本地路径；不接受 UNC/设备路径
    char text[12001];               // 最多 12000 UTF-8 字节；图片发布允许空正文
};
static_assert(sizeof(MomentsConfig) == 32144, "Unexpected moments config ABI");
static_assert(offsetof(MomentsConfig, self_wxid) == 1560, "Unexpected moments account offset");
static_assert(offsetof(MomentsConfig, visibility) == 1688, "Unexpected moments privacy offset");
static_assert(offsetof(MomentsConfig, image_paths) == 1704, "Unexpected moments paths offset");
static_assert(offsetof(MomentsConfig, text) == 20136, "Unexpected moments text offset");

namespace wxbot_moments_candidate {
constexpr uintptr_t kModelCtor = 0x44776d0;
constexpr uintptr_t kModelControlTable = 0x9391028;
constexpr uintptr_t kGetSns = 0x108aee0;
constexpr uintptr_t kSubmit = 0x306ade0;
constexpr uintptr_t kImageFactory = 0x2236900;
constexpr uintptr_t kAssignMediaVector = 0xf10a00;
constexpr uintptr_t kConnect = 0x4a8950;
constexpr uintptr_t kDisconnect = 0x4a8850;
constexpr uintptr_t kPruneCancelled = 0x4a8cc0;
constexpr uintptr_t kMoveEventAdapter = 0x19e0c40;
constexpr uintptr_t kMethodEquals = 0x311640;
constexpr size_t kFinishedEvent = 0x2a8;

// 新 RVA 已属于 4.1.15.56，不经过旧版本 core_rva 的 legacy switch。
// 已有公共入口继续使用 core_address，绝不回退到旧版本地址。
static uintptr_t address(uintptr_t rva) {
    return InterlockedCompareExchange(&core_profile_id, 0, 0) == 2 && core_base
        ? core_base + rva : 0;
}

struct VectorView { void* begin; void* end; void* capacity; };
struct WideView { wchar_t data[8]; uint64_t length; uint64_t capacity; };
struct Slot {
    void* receiver;
    void* adapter;
    void* method;
    uintptr_t method_extra;
    uint64_t flags;
    void* method_equals;
};
static_assert(sizeof(Slot) == 48, "Unexpected native event slot ABI");
static_assert(sizeof(WideView) == 32, "Unexpected native wstring ABI");
static_assert(sizeof(std::shared_ptr<int>) == sizeof(SharedMessage), "Unexpected shared_ptr ABI");

struct Snapshot {
    bool matching;
    bool account_matches;
    bool ret_known;
    int32_t ret_code;
    uint64_t sns_id;
};

struct Lease : std::enable_shared_from_this<Lease> {
    MomentsConfig config;
    bool execute;
    TestResult report{};
    SRWLOCK report_lock = SRWLOCK_INIT;
    HANDLE done = nullptr;
    volatile LONG started = 0;
    volatile LONG finalized = 0;
    volatile LONG accepted = 0;
    volatile LONG submitted = 0;
    volatile LONG subscribed = 0;
    volatile LONG quarantine = 0;
    volatile LONG deadline_observed = 0;
    ULONGLONG deadline = 0;
    SharedMessage app{}, kernel{}, service{}, scheduler{}, model{};
    SharedMessage media[9]{};
    void* raw_model_allocation = nullptr;
    bool model_constructed = false;
    char client_id[256]{};
    size_t text_bytes = 0;
    uint32_t media_ready = 0;
    uint32_t cdn_ready_inputs = 0;
    uint32_t native_path_matches = 0;
    uint32_t local_thumb_ready = 0;
    wchar_t staged_images[9][1024]{};
    bool staged_created[9]{};
    explicit Lease(const MomentsConfig& value, bool send) : config(value), execute(send) {
        _InterlockedIncrement(&live_tasks);
    }
    ~Lease() {
        // 原生引用必须在原生任务上清理。quarantine 会保留全局 owner，禁止卸载。
        if (done) CloseHandle(done);
        _InterlockedDecrement(&live_tasks);
    }
};

static SRWLOCK pending_lock = SRWLOCK_INIT;
static std::shared_ptr<Lease> pending;
static volatile LONG admission = 0;

static void retain_pending(const std::shared_ptr<Lease>& lease) {
    AcquireSRWLockExclusive(&pending_lock);
    pending = lease;
    ReleaseSRWLockExclusive(&pending_lock);
}
static void release_pending(Lease* expected) {
    std::shared_ptr<Lease> old;
    AcquireSRWLockExclusive(&pending_lock);
    if (pending.get() == expected) old.swap(pending);
    ReleaseSRWLockExclusive(&pending_lock);
}

static void write_result(Lease& lease, DWORD status, const char* outcome,
                         bool inspected, bool ret_known = false, int32_t ret_code = 0,
                         uint64_t sns_id = 0, bool account_ok = false) {
    const bool accepted = InterlockedCompareExchange(&lease.accepted, 0, 0) != 0;
    const bool success = !std::strcmp(outcome, "completed") && accepted && ret_known &&
        ret_code == 0 && sns_id != 0 && account_ok && !lease.report.exception &&
        !InterlockedCompareExchange(&lease.deadline_observed, 0, 0) &&
        !InterlockedCompareExchange(&lease.quarantine, 0, 0);
    AcquireSRWLockExclusive(&lease.report_lock);
    if ((!std::strcmp(outcome, "accepted") || !std::strcmp(outcome, "timeout_cleanup_pending")) &&
        InterlockedCompareExchange(&lease.finalized, 0, 0)) {
        ReleaseSRWLockExclusive(&lease.report_lock);
        return; // 中间状态不得覆盖已写入的终态。
    }
    FILE* log = nullptr;
    if (_wfopen_s(&log, lease.config.base.log_path, L"wb") == 0 && log) {
        std::fprintf(log, "{\"operation\":\"moments_%s\",\"stage\":%lu,\"exception\":%lu,"
            "\"status\":%lu,\"outcome\":", lease.execute ? "publish" : "inspect",
            lease.report.stage, lease.report.exception, status);
        write_json_string(log, outcome);
        std::fputs(",\"account\":", log); write_json_string(log, lease.config.self_wxid);
        std::fputs(",\"client_id\":", log); write_json_string(log, lease.client_id);
        std::fprintf(log, ",\"visibility\":\"%s\",\"image_count\":%u,\"media_ready\":%u,"
            "\"text_bytes\":%llu,\"inspected\":%s,\"accepted\":%s,"
            "\"completion_success\":%s,\"server_result_known\":%s,\"ret_code\":",
            lease.config.visibility == 2 ? "private" : "public", lease.config.image_count,
            lease.media_ready, static_cast<unsigned long long>(lease.text_bytes),
            inspected ? "true" : "false", accepted ? "true" : "false",
            success ? "true" : "false", ret_known ? "true" : "false");
        if (ret_known) std::fprintf(log, "%d", ret_code); else std::fputs("null", log);
        std::fprintf(log, ",\"sns_id\":\"%llu\",\"account_matches\":%s,"
            "\"unload_blocked\":%s,\"native_submit_enabled\":%s,\"event_probe_enabled\":%s,"
            "\"local_thumbnail_may_be_created\":%s,\"cdn_ready_inputs\":%u,"
            "\"native_path_matches\":%u,\"local_thumb_ready\":%u,\"thread_id\":%lu}\n",
            static_cast<unsigned long long>(sns_id), account_ok ? "true" : "false",
            InterlockedCompareExchange(&lease.quarantine, 0, 0) ? "true" : "false",
            WXBOT_MOMENTS_ENABLE_CANDIDATE_SUBMIT ? "true" : "false",
            WXBOT_MOMENTS_ENABLE_EVENT_PROBE ? "true" : "false",
            lease.config.image_count ? "true" : "false", lease.cdn_ready_inputs,
            lease.native_path_matches, lease.local_thumb_ready, GetCurrentThreadId());
        std::fclose(log);
    }
    ReleaseSRWLockExclusive(&lease.report_lock);
}

static bool anchors_match() {
    struct Anchor { uintptr_t rva; unsigned char prefix[16]; };
    static constexpr Anchor anchors[] = {
        {kModelCtor, {0x55,0x56,0x57,0x48,0x81,0xec,0x60,0x02,0,0,0x48,0x8d,0xac,0x24,0x80,0}},
        {kGetSns, {0x55,0x56,0x57,0x48,0x81,0xec,0x90,0,0,0,0x48,0x8d,0xac,0x24,0x80,0}},
        {kSubmit, {0x55,0x56,0x57,0x48,0x81,0xec,0x80,0,0,0,0x48,0x8d,0xac,0x24,0x80,0}},
        {kConnect, {0x55,0x41,0x56,0x56,0x57,0x53,0x48,0x81,0xec,0x90,0,0,0,0x48,0x8d,0xac}},
        {kPruneCancelled, {0x41,0x56,0x56,0x57,0x53,0x48,0x83,0xec,0x28,0x48,0x89,0xd6,0x80,0xba,0xb4,0}},
    };
    __try {
        for (const auto& anchor : anchors)
            if (std::memcmp(reinterpret_cast<void*>(address(anchor.rva)), anchor.prefix, 16)) return false;
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

static bool read_string(const SmallString* value, char* output, size_t capacity) {
    if (!value || !output || value->length == 0 || value->length >= capacity ||
        value->capacity < value->length || value->capacity > 1024 * 1024) return false;
    const char* bytes = value->capacity > 15 ? *reinterpret_cast<const char* const*>(value->data) : value->data;
    if (!bytes) return false;
    std::memcpy(output, bytes, static_cast<size_t>(value->length));
    output[value->length] = 0;
    return true;
}

static DWORD validate_file(wchar_t* path, size_t capacity) {
    const size_t length = wcsnlen_s(path, capacity);
    if (length < 4 || length >= capacity || path[1] != L':' || path[2] != L'\\' ||
        !((path[0] >= L'A' && path[0] <= L'Z') || (path[0] >= L'a' && path[0] <= L'z')))
        return ERROR_INVALID_PARAMETER;
    const wchar_t* ext = std::wcsrchr(path, L'.');
    if (!ext || (_wcsicmp(ext, L".png") && _wcsicmp(ext, L".jpg") && _wcsicmp(ext, L".jpeg")))
        return ERROR_INVALID_PARAMETER;
    wchar_t volume[4]{path[0], L':', L'\\', 0};
    const UINT drive = GetDriveTypeW(volume);
    if (drive != DRIVE_FIXED && drive != DRIVE_REMOVABLE && drive != DRIVE_RAMDISK)
        return ERROR_INVALID_PARAMETER;
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                               FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
    if (file == INVALID_HANDLE_VALUE) return GetLastError();
    DWORD status = ERROR_SUCCESS, read = 0;
    BY_HANDLE_FILE_INFORMATION info{};
    LARGE_INTEGER size{};
    unsigned char header[12]{};
    wchar_t final_path[1024]{};
    if (!GetFileInformationByHandle(file, &info) || !GetFileSizeEx(file, &size) ||
        !ReadFile(file, header, sizeof(header), &read, nullptr)) status = GetLastError();
    else if ((info.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) ||
             size.QuadPart < 8 || size.QuadPart > 64ll * 1024 * 1024 || read < 8 ||
             (std::memcmp(header, "\x89PNG\r\n\x1a\n", 8) &&
              !(header[0] == 0xff && header[1] == 0xd8 && header[2] == 0xff))) status = ERROR_INVALID_DATA;
    else {
        DWORD count = GetFinalPathNameByHandleW(file, final_path, 1024, FILE_NAME_NORMALIZED | VOLUME_NAME_DOS);
        if (!count || count >= 1024 || std::wcsncmp(final_path, L"\\\\?\\", 4) ||
            final_path[5] != L':' || wcslen(final_path + 4) >= capacity) status = ERROR_INVALID_NAME;
        else wcscpy_s(path, capacity, final_path + 4);
    }
    CloseHandle(file);
    return status;
}

static DWORD validate_config(MomentsConfig& config) {
    const size_t length = strnlen_s(config.text, sizeof(config.text));
    const size_t account_length = strnlen_s(config.self_wxid, sizeof(config.self_wxid));
    const size_t log_length = wcsnlen_s(config.base.log_path, std::size(config.base.log_path));
    if (config.base.size != sizeof(config) || config.base.pid != GetCurrentProcessId() ||
        config.base.text[0] || config.reserved || config.image_count > 9 ||
        (config.visibility != 1 && config.visibility != 2) ||
        config.timeout_ms < 10000 || config.timeout_ms > 300000 ||
        !account_length || account_length >= sizeof(config.self_wxid) || !valid_target(config.self_wxid) ||
        std::strchr(config.self_wxid, '@') || !log_length || log_length >= std::size(config.base.log_path) ||
        length >= sizeof(config.text) || (!length && !config.image_count) ||
        (length && !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, config.text,
                                       static_cast<int>(length), nullptr, 0))) return ERROR_INVALID_PARAMETER;
    for (uint32_t index = 0; index < config.image_count; ++index) {
        const DWORD status = validate_file(config.image_paths[index], std::size(config.image_paths[index]));
        if (status) return status;
        for (uint32_t prior = 0; prior < index; ++prior)
            if (!_wcsicmp(config.image_paths[index], config.image_paths[prior])) return ERROR_INVALID_PARAMETER;
    }
    for (uint32_t index = config.image_count; index < 9; ++index)
        if (config.image_paths[index][0]) return ERROR_INVALID_PARAMETER;
    return ERROR_SUCCESS;
}

static bool check_model(Lease* lease) {
    auto model = static_cast<unsigned char*>(lease->model.object);
    if (!model || !lease->model.control ||
        *static_cast<const uintptr_t*>(lease->model.control) != address(kModelControlTable) ||
        *reinterpret_cast<uint32_t*>(model + 0x30) != (lease->config.image_count ? 1u : 2u) ||
        *reinterpret_cast<uint32_t*>(model + 0x248) != lease->config.visibility) return false;
    const auto text = reinterpret_cast<SmallString*>(model + 0x60);
    const char* body = text->capacity > 15 ? *reinterpret_cast<char* const*>(text->data) : text->data;
    if (text->length != lease->text_bytes || text->capacity < text->length || !body ||
        std::memcmp(body, lease->config.text, lease->text_bytes)) return false;
    const auto vector = reinterpret_cast<const VectorView*>(model + 0x118);
    if (reinterpret_cast<uintptr_t>(vector->end) - reinterpret_cast<uintptr_t>(vector->begin) !=
        16ull * lease->config.image_count ||
        reinterpret_cast<uintptr_t>(vector->capacity) < reinterpret_cast<uintptr_t>(vector->end)) return false;
    return read_string(reinterpret_cast<SmallString*>(model + 0x40), lease->client_id, sizeof(lease->client_id));
}

// 该函数只有平凡局部变量，避免 MSVC C2712（SEH 与 C++ 自动析构混用）。
static bool inspect_local_media_input(const unsigned char* media, const wchar_t* expected, Lease* lease) {
    char value[4096]{};
    wchar_t wide[2048]{};
    if (!read_string(reinterpret_cast<const SmallString*>(media + 0x48), value, sizeof(value)) ||
        !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value, -1, wide, 2048)) return false;
    const size_t length = wcslen(wide);
    WideView view{};
    view.length = length;
    view.capacity = length < 8 ? 7 : length;
    if (length < 8) std::memcpy(view.data, wide, (length + 1) * sizeof(wchar_t));
    else { const wchar_t* pointer = wide; std::memcpy(view.data, &pointer, sizeof(pointer)); }
    if (CompareStringOrdinal(wide, -1, expected, -1, TRUE) == CSTR_EQUAL) ++lease->native_path_matches;
    uint32_t kind = 1;
    using Validate = bool (__fastcall*)(const WideView*, uint32_t*);
    if (reinterpret_cast<Validate>(address(0x90b60))(&view, &kind)) ++lease->cdn_ready_inputs;
    std::memset(value, 0, sizeof(value));
    std::memset(wide, 0, sizeof(wide));
    if (read_string(reinterpret_cast<const SmallString*>(media + 0x68), value, sizeof(value)) &&
        *value && MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, value, -1, wide, 2048) &&
        GetFileAttributesW(wide) != INVALID_FILE_ATTRIBUTES) ++lease->local_thumb_ready;
    return true;
}

static DWORD stage_media_input(Lease* lease, uint32_t index) {
    // CDN accepts paths in Weixin's own cache roots. Allocate InputTemp exactly
    // as ordinary file sending does; never modify the original image or bypass
    // the native CDN path validation.
    FileWideString directory{}, prepared{};
    SmallString token{};
    token.capacity = 15;
    HANDLE source = INVALID_HANDLE_VALUE, output = INVALID_HANDLE_VALUE;
    DWORD status = ERROR_GEN_FAILURE;
    __try {
        __try {
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
            char name[160]{};
            if (!read_string(&token, name, 128)) { status = ERROR_INVALID_DATA; __leave; }
            for (const char* cursor = name; *cursor; ++cursor)
                if (!((*cursor >= 'a' && *cursor <= 'z') || (*cursor >= 'A' && *cursor <= 'Z') ||
                      (*cursor >= '0' && *cursor <= '9') || *cursor == '-' || *cursor == '_')) {
                    status = ERROR_INVALID_NAME; __leave;
                }
            const wchar_t* original = lease->config.image_paths[index];
            const wchar_t* extension = std::wcsrchr(original, L'.');
            strcat_s(name, extension && !_wcsicmp(extension, L".png") ? ".png" : ".jpg");
            struct StringView { const char* data; uint64_t length; } view{name, std::strlen(name)};
            reinterpret_cast<Join>(core_address(core_base, 0x9f4b0))(&directory, &prepared, &view);
            const wchar_t* path = file_wide_data(prepared);
            const wchar_t* root = file_wide_data(directory);
            if (!path || !root || prepared.length <= directory.length ||
                std::wcsncmp(path, root, static_cast<size_t>(directory.length)) ||
                (path[directory.length] != L'\\' && path[directory.length] != L'/') ||
                wcscpy_s(lease->staged_images[index], path)) {
                status = ERROR_INVALID_NAME; __leave;
            }
            source = CreateFileW(original, GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                FILE_FLAG_SEQUENTIAL_SCAN | FILE_FLAG_OPEN_REPARSE_POINT, nullptr);
            if (source == INVALID_HANDLE_VALUE) { status = GetLastError(); __leave; }
            BY_HANDLE_FILE_INFORMATION info{};
            LARGE_INTEGER size{};
            if (!GetFileInformationByHandle(source, &info) || !GetFileSizeEx(source, &size) ||
                (info.dwFileAttributes & (FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT)) ||
                size.QuadPart < 8 || size.QuadPart > 64ll * 1024 * 1024) {
                status = ERROR_INVALID_DATA; __leave;
            }
            output = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ, nullptr, CREATE_NEW,
                                 FILE_ATTRIBUTE_NORMAL, nullptr);
            if (output == INVALID_HANDLE_VALUE) { status = GetLastError(); __leave; }
            lease->staged_created[index] = true;
            unsigned char buffer[65536]{};
            LONGLONG copied = 0;
            while (copied < size.QuadPart) {
                DWORD read = 0, written = 0;
                const DWORD count = static_cast<DWORD>(std::min<LONGLONG>(sizeof(buffer), size.QuadPart - copied));
                if (!ReadFile(source, buffer, count, &read, nullptr) || read != count) {
                    status = ERROR_READ_FAULT; __leave;
                }
                if (!WriteFile(output, buffer, read, &written, nullptr) || written != read) {
                    status = ERROR_WRITE_FAULT; __leave;
                }
                copied += written;
            }
            status = ERROR_SUCCESS;
        } __except (record_exception(GetExceptionInformation(), &lease->report)) {
            status = ERROR_GEN_FAILURE;
        }
    } __finally {
        if (source != INVALID_HANDLE_VALUE) CloseHandle(source);
        if (output != INVALID_HANDLE_VALUE) CloseHandle(output);
        image_free_string(&token, &lease->report);
        file_release_wide(&prepared, &lease->report);
        file_release_wide(&directory, &lease->report);
    }
    return lease->report.exception ? ERROR_GEN_FAILURE : status;
}

static DWORD prepare_native(Lease* lease) {
    __try {
        lease->report.stage = 160;
        using GetApp = SharedMessage* (__fastcall*)(SharedMessage*);
        using GetService = SharedMessage* (__fastcall*)(void*, SharedMessage*);
        reinterpret_cast<GetApp>(core_address(core_base, 0x47220))(&lease->app);
        if (!emoticon_account_matches(lease->app, lease->config.self_wxid)) return ERROR_NOT_READY;
        reinterpret_cast<GetService>(core_address(core_base, 0x36c740))(lease->app.object, &lease->kernel);
        if (!lease->kernel.object || !lease->kernel.control) return ERROR_NOT_READY;
        // 默认 Inspect 不触发 SNS 服务的可能惰性创建，只构造本地模型。
        if (lease->execute || WXBOT_MOMENTS_ENABLE_EVENT_PROBE) {
            reinterpret_cast<GetService>(address(kGetSns))(lease->kernel.object, &lease->service);
            if (!lease->service.object || !lease->service.control) return ERROR_NOT_READY;
        }
        using Allocate = void* (__fastcall*)(size_t);
        auto block = static_cast<unsigned char*>(reinterpret_cast<Allocate>(
            core_address(core_base, 0x74ab89c))(0x2b8));
        if (!block) return ERROR_NOT_ENOUGH_MEMORY;
        lease->raw_model_allocation = block;
        *reinterpret_cast<uintptr_t*>(block) = address(kModelControlTable);
        *reinterpret_cast<uint64_t*>(block + 8) = 0x100000001ull;
        using Construct = void* (__fastcall*)(void*, uint32_t);
        reinterpret_cast<Construct>(address(kModelCtor))(block + 0x10, lease->config.image_count ? 8 : 9);
        lease->model_constructed = true;
        lease->model = {block + 0x10, block};
        lease->raw_model_allocation = nullptr;
        auto model = block + 0x10;
        auto text = reinterpret_cast<SmallString*>(model + 0x60);
        if (text->length || text->capacity != 15) return ERROR_INVALID_DATA;
        // 88ad0 原生字符串 ctor，确认支持 >=4096 的对齐分配及释放元数据。
        using ConstructString = SmallString* (__fastcall*)(SmallString*, const char*);
        reinterpret_cast<ConstructString>(core_address(core_base, 0x88ad0))(text, lease->config.text);
        *reinterpret_cast<uint32_t*>(model + 0x30) = lease->config.image_count ? 1 : 2;
        *reinterpret_cast<uint32_t*>(model + 0x248) = lease->config.visibility;
        if (!lease->config.image_count) *reinterpret_cast<uint32_t*>(model + 0x84) = 2;
        lease->report.stage = 161;
        for (uint32_t index = 0; index < lease->config.image_count; ++index) {
            const DWORD staged = stage_media_input(lease, index);
            if (staged) return staged;
            // 2236900 reads std::wstring; borrow the owned InputTemp copy.
            WideView path{};
            const wchar_t* source = lease->staged_images[index];
            path.length = wcslen(source);
            path.capacity = path.length < 8 ? 7 : path.length;
            if (path.length < 8) std::memcpy(path.data, source, (path.length + 1) * sizeof(wchar_t));
            else std::memcpy(path.data, &source, sizeof(source));
            using MediaFactory = void (__fastcall*)(SharedMessage*, const WideView*);
            reinterpret_cast<MediaFactory>(address(kImageFactory))(&lease->media[index], &path);
            const auto media = static_cast<const unsigned char*>(lease->media[index].object);
            if (!media || !lease->media[index].control || *reinterpret_cast<const uint32_t*>(media) != 2 ||
                !(*reinterpret_cast<const float*>(media + 0x88) > 0 &&
                  *reinterpret_cast<const float*>(media + 0x88) < 65536) ||
                !(*reinterpret_cast<const float*>(media + 0x8c) > 0 &&
                  *reinterpret_cast<const float*>(media + 0x8c) < 65536)) return ERROR_INVALID_DATA;
            ++lease->media_ready;
            if (!lease->execute && !inspect_local_media_input(media, source, lease)) return ERROR_INVALID_DATA;
        }
        if (lease->config.image_count) {
            using AssignVector = void (__fastcall*)(void*, const SharedMessage*, size_t);
            reinterpret_cast<AssignVector>(address(kAssignMediaVector))(
                model + 0x118, lease->media, lease->config.image_count);
        }
        if (!check_model(lease) || !emoticon_account_matches(lease->app, lease->config.self_wxid))
            return ERROR_INVALID_DATA;
        lease->report.stage = 162;
        return ERROR_SUCCESS;
    } __except (record_exception(GetExceptionInformation(), &lease->report)) { return ERROR_GEN_FAILURE; }
}

static bool release_native(Lease* lease, bool remove_staged) {
    // 构造/核对失败不等于清理失败。单独记录本轮清理异常，避免失败探针
    // 已成功释放所有引用，却因先前的 exception 字段永久卡住 live_tasks。
    TestResult cleanup{};
    for (auto& media : lease->media) guarded_release_shared(&media, &cleanup);
    guarded_release_shared(&lease->model, &cleanup);
    guarded_release_shared(&lease->scheduler, &cleanup);
    guarded_release_shared(&lease->service, &cleanup);
    guarded_release_shared(&lease->kernel, &cleanup);
    guarded_release_shared(&lease->app, &cleanup);
    if (lease->raw_model_allocation) {
        __try {
            // ctor 未完成时不运行完整对象析构；原生 C++ frame 负责已构造成员展开。
            using Free = void (__fastcall*)(void*);
            reinterpret_cast<Free>(address(0x754bd18))(lease->raw_model_allocation);
            lease->raw_model_allocation = nullptr;
        } __except (record_exception(GetExceptionInformation(), &cleanup)) { }
    }
    if (cleanup.exception) {
        if (!lease->report.exception) lease->report = cleanup;
        return false;
    }
    // Unknown/timeout is not proof that an asynchronous uploader stopped.
    // Leave its native cache input intact until Weixin's normal cache cleanup.
    if (remove_staged)
        for (uint32_t index = 0; index < 9; ++index)
            if (lease->staged_created[index]) {
                DeleteFileW(lease->staged_images[index]);
                lease->staged_created[index] = false;
            }
    return true;
}

static void __fastcall event_entry(void* receiver, SharedMessage* event);

static DWORD connect_native(Lease* lease, const SharedMessage* owner) {
    __try {
        if (!emoticon_account_matches(lease->app, lease->config.self_wxid)) return ERROR_NOT_READY;
        using App = void* (__fastcall*)();
        using GetScheduler = SharedMessage* (__fastcall*)(void*, SharedMessage*);
        void* app = reinterpret_cast<App>(core_address(core_base, 0x45d10))();
        if (!app) return ERROR_NOT_READY;
        reinterpret_cast<GetScheduler>(core_address(core_base, 0x47310))(app, &lease->scheduler);
        if (!lease->scheduler.object || !lease->scheduler.control) return ERROR_NOT_READY;
        void* executor = *reinterpret_cast<void**>(static_cast<unsigned char*>(lease->scheduler.object) + 0x10);
        if (!executor) return ERROR_NOT_READY;
        Slot slot{lease, reinterpret_cast<void*>(address(kMoveEventAdapter)),
                  reinterpret_cast<void*>(&event_entry), 0, 0,
                  reinterpret_cast<void*>(address(kMethodEquals))};
        using Connect = void (__fastcall*)(void*, const Slot*, void*, uint32_t, void*, const SharedMessage*);
        lease->report.stage = 163;
        reinterpret_cast<Connect>(address(kConnect))(
            static_cast<unsigned char*>(lease->service.object) + kFinishedEvent,
            &slot, nullptr, 0, executor, owner);
        _InterlockedExchange(&lease->subscribed, 1);
        return ERROR_SUCCESS;
    } __except (record_exception(GetExceptionInformation(), &lease->report)) {
        // Connect 可能已经把回调交给原生系统，不能假定失败即未注册。
        _InterlockedExchange(&lease->quarantine, 1);
        return ERROR_GEN_FAILURE;
    }
}

struct NativeEventGuard {
    void* mutex;
    bool owns;
};
static_assert(sizeof(NativeEventGuard) == 16, "Unexpected native event guard ABI");
static_assert(offsetof(NativeEventGuard, owns) == 8, "Unexpected native event guard state");

static bool drain_cancelled_connections(Lease* lease) {
    // Disconnect only marks nodes. Without another SNS event, their strong owner
    // references remain alive indefinitely. Use the same cleanup path as Emit's
    // scope guard, under its separate recursive emission mutex (+50).
    NativeEventGuard guard{};
    bool success = false;
    __try {
        __try {
            auto source = static_cast<unsigned char*>(lease->service.object) + kFinishedEvent;
            auto backend = *reinterpret_cast<unsigned char**>(source + 8);
            if (!backend) __leave;
            guard.mutex = backend + 0x50;
            using Lock = int (__fastcall*)(void*);
            using Prune = void (__fastcall*)(NativeEventGuard*, void*);
            if (reinterpret_cast<Lock>(address(0x754cd48))(guard.mutex) != 0) __leave;
            guard.owns = true;
            if (*reinterpret_cast<const unsigned char*>(backend + 0xb4)) __leave;
            // A callback may be running inside Emit on this same thread. In
            // that case Emit's own guard prunes after all callbacks return.
            if (*reinterpret_cast<const uint32_t*>(backend + 0xb0) == 0)
                reinterpret_cast<Prune>(address(kPruneCancelled))(&guard, backend);
            success = true;
        } __finally {
            if (guard.owns) {
                using Unlock = int (__fastcall*)(void*);
                reinterpret_cast<Unlock>(address(0x754cd70))(guard.mutex);
                guard.owns = false;
            }
        }
    } __except (record_exception(GetExceptionInformation(), &lease->report)) {
        success = false;
    }
    return success;
}

static bool disconnect_native(Lease* lease) {
    if (!InterlockedCompareExchange(&lease->subscribed, 0, 0)) return true;
    __try {
        if (!lease->service.object) return false;
        using Disconnect = void (__fastcall*)(void*, void*);
        reinterpret_cast<Disconnect>(address(kDisconnect))(
            static_cast<unsigned char*>(lease->service.object) + kFinishedEvent, lease);
        _InterlockedExchange(&lease->subscribed, 0);
        return drain_cancelled_connections(lease);
    } __except (record_exception(GetExceptionInformation(), &lease->report)) { return false; }
}

static DWORD submit_native(Lease* lease) {
    __try {
        if (InterlockedCompareExchange(&lease->quarantine, 0, 0) ||
            !check_model(lease) || !emoticon_account_matches(lease->app, lease->config.self_wxid))
            return ERROR_NOT_READY;
        using Submit = bool (__fastcall*)(void*, SharedMessage*);
        lease->report.stage = 164;
        // 306ade0@306af93..306afba 析构输入 shared_ptr，原生55f45b0也显式保留副本。
        // 传递一份额外强引用；本地 lease.model 的引用留待完成/超时清理。
        SharedMessage transferred = lease->model;
        _InterlockedIncrement(reinterpret_cast<volatile LONG*>(
            static_cast<unsigned char*>(transferred.control) + 8));
        _InterlockedExchange(&lease->submitted, 1); // 此后失败/超时也不自动重试。
        const bool accepted = reinterpret_cast<Submit>(address(kSubmit))(lease->service.object, &transferred);
        if (accepted) _InterlockedExchange(&lease->accepted, 1);
        if (!emoticon_account_matches(lease->app, lease->config.self_wxid)) return ERROR_NOT_READY;
        return accepted ? ERROR_SUCCESS : ERROR_GEN_FAILURE;
    } __except (record_exception(GetExceptionInformation(), &lease->report)) { return ERROR_GEN_FAILURE; }
}

static DWORD snapshot_native(Lease* lease, const SharedMessage* event, Snapshot* snapshot) {
    __try {
        if (!event || !event->object || !event->control) return ERROR_INVALID_DATA;
        const auto item = static_cast<const unsigned char*>(event->object);
        char client_id[256]{};
        if (!read_string(reinterpret_cast<const SmallString*>(item + 8), client_id, sizeof(client_id)))
            return ERROR_INVALID_DATA;
        if (std::strcmp(client_id, lease->client_id)) return ERROR_SUCCESS;
        snapshot->matching = true;
        snapshot->account_matches = emoticon_account_matches(lease->app, lease->config.self_wxid);
        snapshot->ret_code = *reinterpret_cast<const int32_t*>(item);
        snapshot->ret_known = true;
        const auto model = reinterpret_cast<const SharedMessage*>(item + 0x28);
        if (snapshot->ret_code == 0 && model->object && model->control) {
            // 同时核对 event 回传模型的 client_id，不能只信任外层事件标签。
            char returned_id[256]{};
            const auto data = static_cast<const unsigned char*>(model->object);
            if (!read_string(reinterpret_cast<const SmallString*>(data + 0x40), returned_id,
                             sizeof(returned_id)) || std::strcmp(returned_id, lease->client_id))
                return ERROR_INVALID_DATA;
            const auto sns = reinterpret_cast<const SharedMessage*>(data + 0x280);
            if (sns->object && sns->control)
                snapshot->sns_id = *static_cast<const uint64_t*>(sns->object);
        }
        return ERROR_SUCCESS;
    } __except (record_exception(GetExceptionInformation(), &lease->report)) { return ERROR_INVALID_DATA; }
}

// 只从已调度原生任务或带同一 executor 的原生完成回调调用。
static void finish_on_task(const std::shared_ptr<Lease>& lease, DWORD status, const char* outcome,
                           bool inspected, const Snapshot* snapshot = nullptr) {
    if (_InterlockedCompareExchange(&lease->finalized, 1, 0) != 0) return;
    if (lease->done) SetEvent(lease->done);
    const bool account_ok = snapshot && snapshot->account_matches;
    const bool ret_known = snapshot && snapshot->ret_known;
    const int32_t ret_code = ret_known ? snapshot->ret_code : 0;
    const uint64_t sns_id = snapshot ? snapshot->sns_id : 0;
    bool safe = !InterlockedCompareExchange(&lease->quarantine, 0, 0) && disconnect_native(lease.get());
    if (safe) safe = release_native(lease.get(), inspected || ret_known ||
        !InterlockedCompareExchange(&lease->submitted, 0, 0));
    if (!safe) {
        _InterlockedExchange(&lease->quarantine, 1);
        status = ERROR_GEN_FAILURE;
        outcome = "cleanup_unconfirmed";
        // 保留 pending 和 live_tasks；宁可拒绝卸载，不释放可能仍被原生回调使用的代码。
    }
    lease->report.stage = safe ? 166 : 169;
    write_result(*lease, status, outcome, inspected, ret_known, ret_code, sns_id, account_ok);
    if (safe) release_pending(lease.get());
}

static void __fastcall event_entry(void* receiver, SharedMessage* event) {
    _InterlockedIncrement(&active_callbacks);
    try {
        auto raw = static_cast<Lease*>(receiver);
        auto lease = raw->shared_from_this(); // 原生事件 node 已持有 ownerShared 的强引用。
        if (!InterlockedCompareExchange(&lease->finalized, 0, 0)) {
            Snapshot snapshot{};
            const DWORD status = snapshot_native(lease.get(), event, &snapshot);
            if (snapshot.matching) {
                _InterlockedExchange(&lease->accepted, 1);
                if (GetTickCount64() >= lease->deadline) {
                    _InterlockedExchange(&lease->deadline_observed, 1);
                    finish_on_task(lease, WAIT_TIMEOUT, "timeout", false, &snapshot);
                } else if (status != ERROR_SUCCESS) {
                    finish_on_task(lease, status, "invalid_completion", false, &snapshot);
                } else if (!snapshot.account_matches) {
                    finish_on_task(lease, ERROR_NOT_READY, "account_changed", false, &snapshot);
                } else if (snapshot.ret_code != 0) {
                    finish_on_task(lease, ERROR_GEN_FAILURE, "failed", false, &snapshot);
                } else if (!snapshot.sns_id) {
                    finish_on_task(lease, ERROR_INVALID_DATA, "missing_sns_id", false, &snapshot);
                } else {
                    finish_on_task(lease, ERROR_SUCCESS, "completed", false, &snapshot);
                }
            }
        }
        // kMoveEventAdapter 转移了这份引用；与原生 e949f0 一样由被调用方释放。
        if (event) guarded_release_shared(event, &lease->report);
    } catch (...) {
        TestResult error{};
        if (event) guarded_release_shared(event, &error);
        // 不让 C++ 异常跨入微信；event owner/pending 继续阻止卸载，timeout 接管。
    }
    _InterlockedDecrement(&active_callbacks);
}

static DWORD WINAPI deadline_thread(void* parameter) {
    _InterlockedIncrement(&active_callbacks);
    auto holder = static_cast<std::shared_ptr<Lease>*>(parameter);
    auto lease = *holder;
    delete holder;
    try {
        const DWORD wait = WaitForSingleObject(lease->done, lease->config.timeout_ms);
        if (wait != WAIT_OBJECT_0 && !InterlockedCompareExchange(&lease->finalized, 0, 0)) {
            _InterlockedExchange(&lease->deadline_observed, 1);
            std::function<void()> task = [lease]() {
                _InterlockedIncrement(&active_callbacks);
                finish_on_task(lease, WAIT_TIMEOUT, "timeout", false);
                _InterlockedDecrement(&active_callbacks);
            };
            const DWORD status = schedule_task(lease->config.base, &task);
            if (status && !InterlockedCompareExchange(&lease->finalized, 0, 0)) {
                // 无法在原生线程取消时，绝不从 Windows worker 直接操作事件源。
                _InterlockedExchange(&lease->quarantine, 1);
                write_result(*lease, status, "timeout_cleanup_pending", false);
                // pending 继续保留租约；禁止卸载和自动重发。
            }
        }
    } catch (...) {
        _InterlockedExchange(&lease->quarantine, 1);
        write_result(*lease, ERROR_GEN_FAILURE, "timeout_cleanup_pending", false);
    }
    lease.reset();
    _InterlockedDecrement(&active_callbacks);
    return 0;
}

static bool start_deadline(const std::shared_ptr<Lease>& lease) {
    auto holder = new (std::nothrow) std::shared_ptr<Lease>(lease);
    if (!holder) return false;
    HANDLE thread = CreateThread(nullptr, 0, deadline_thread, holder, 0, nullptr);
    if (!thread) { delete holder; return false; }
    CloseHandle(thread);
    return true;
}

static void on_task(const std::shared_ptr<Lease>& lease) {
    _InterlockedIncrement(&active_callbacks);
    // 调度器失败可能发生在复制 callable 后；-1 表示调用方已撤销尚未开始的任务。
    if (_InterlockedCompareExchange(&lease->started, 1, 0) != 0) {
        _InterlockedDecrement(&active_callbacks);
        return;
    }
    lease->text_bytes = std::strlen(lease->config.text);
    const DWORD prepared = prepare_native(lease.get());
    if (prepared || !lease->execute) {
        Snapshot snapshot{};
        snapshot.account_matches = prepared == ERROR_SUCCESS;
        DWORD inspected = prepared;
        if (!prepared && !lease->execute && WXBOT_MOMENTS_ENABLE_EVENT_PROBE) {
            SharedMessage owner{};
            std::memcpy(&owner, &lease, sizeof(owner));
            // 可选研究探针：只注册后立即取消；绝不调用 Submit 或伪造服务端完成事件。
            inspected = connect_native(lease.get(), &owner);
        }
        finish_on_task(lease, inspected, inspected ? "prepare_failed" : "inspect_ready",
                       inspected == ERROR_SUCCESS, &snapshot);
        _InterlockedDecrement(&active_callbacks);
        return;
    }
    // 必须先订阅完成事件，再提交；收到非零 SNS ID 才确认发布。
    lease->done = CreateEventW(nullptr, TRUE, FALSE, nullptr);
    if (!lease->done) {
        finish_on_task(lease, GetLastError(), "prepare_failed", false);
        _InterlockedDecrement(&active_callbacks);
        return;
    }
    lease->deadline = GetTickCount64() + lease->config.timeout_ms;
    SharedMessage owner{};
    std::memcpy(&owner, &lease, sizeof(owner)); // 仅输入视图；Connect 自己原子 retain。
    const DWORD connected = connect_native(lease.get(), &owner);
    if (connected) {
        finish_on_task(lease, connected, "subscribe_failed", false);
    } else if (!start_deadline(lease)) {
        finish_on_task(lease, ERROR_NOT_ENOUGH_MEMORY, "deadline_unavailable", false);
    } else {
        const DWORD submitted = submit_native(lease.get());
        if (submitted) {
            finish_on_task(lease, submitted,
                submitted == ERROR_NOT_READY ? "account_changed" : "submit_unconfirmed", false);
        } else if (!InterlockedCompareExchange(&lease->finalized, 0, 0)) {
            lease->report.stage = 165;
            write_result(*lease, ERROR_SUCCESS, "accepted", false, false, 0, 0, true);
        }
    }
    _InterlockedDecrement(&active_callbacks);
}

static DWORD copy_config(void* parameter, MomentsConfig* config) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    __try { *config = *static_cast<const MomentsConfig*>(parameter); }
    __except (EXCEPTION_EXECUTE_HANDLER) { return ERROR_INVALID_PARAMETER; }
    return ERROR_SUCCESS;
}

static DWORD dispatch(void* parameter, bool execute) {
    if (InterlockedCompareExchange(&live_tasks, 0, 0) ||
        InterlockedCompareExchange(&live_completions, 0, 0)) return ERROR_BUSY;
    auto config = std::unique_ptr<MomentsConfig>(new (std::nothrow) MomentsConfig{});
    if (!config) return ERROR_NOT_ENOUGH_MEMORY;
    DWORD status = copy_config(parameter, config.get());
    if (status) return status;
    status = validate_config(*config);
    if (status) return status;
    const HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core) || InterlockedCompareExchange(&core_profile_id, 0, 0) != 2)
        return ERROR_NOT_SUPPORTED;
    core_base = reinterpret_cast<uintptr_t>(core);
    if (!anchors_match()) return ERROR_REVISION_MISMATCH;
    try {
        auto lease = std::make_shared<Lease>(*config, execute);
        lease->text_bytes = std::strlen(config->text);
        if (execute && !WXBOT_MOMENTS_ENABLE_CANDIDATE_SUBMIT) {
            write_result(*lease, ERROR_NOT_SUPPORTED, "native_abi_unverified", false);
            return ERROR_NOT_SUPPORTED;
        }
        std::function<void()> task = [lease]() { on_task(lease); };
        retain_pending(lease);
        status = schedule_task(config->base, &task);
        if (status && _InterlockedCompareExchange(&lease->started, -1, 0) == 0) {
            // 原生 scheduler 异常可能发生在复制 callable 后，不能假设无人引用。
            _InterlockedExchange(&lease->quarantine, 1);
            write_result(*lease, status, "schedule_unconfirmed", false);
        }
        return status;
    } catch (const std::bad_alloc&) { return ERROR_NOT_ENOUGH_MEMORY; }
    catch (...) { return ERROR_GEN_FAILURE; }
}

static DWORD exported(void* parameter, bool execute) {
    _InterlockedIncrement(&active_callbacks);
    bool admitted = false;
    __try {
        if (_InterlockedCompareExchange(&admission, 1, 0) != 0) return ERROR_BUSY;
        admitted = true;
        return dispatch(parameter, execute);
    } __finally {
        if (admitted) _InterlockedExchange(&admission, 0);
        _InterlockedDecrement(&active_callbacks);
    }
}
} // namespace wxbot_moments_candidate

extern "C" __declspec(dllexport) DWORD WINAPI WxInspectMoments(void* parameter) {
    return wxbot_moments_candidate::exported(parameter, false);
}
extern "C" __declspec(dllexport) DWORD WINAPI WxPublishMoments(void* parameter) {
    return wxbot_moments_candidate::exported(parameter, true);
}

// 供离线 PE 检查器读取的纯常量；不得访问账号、core 或执行原生调用。
extern "C" __declspec(dllexport) DWORD WINAPI WxMomentsCapabilities() {
    return 1u | (WXBOT_MOMENTS_ENABLE_CANDIDATE_SUBMIT ? 2u : 0u) |
        (WXBOT_MOMENTS_ENABLE_EVENT_PROBE ? 4u : 0u);
}
