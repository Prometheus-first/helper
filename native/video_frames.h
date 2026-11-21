#pragma once
// Three bounded I420 slots. Interlocked ownership publishes complete frames;
// neither the producer nor the consumer can overwrite a READING slot.
static constexpr uint32_t video_map_magic = 0x31564257; // WBV1
struct VideoMapHeader {
    uint32_t magic, abi, bytes, width, height, format, slot_bytes, slot_count;
    uint32_t owner_pid, reserved;
    uint64_t generation[2];
    volatile LONG64 heartbeat;
    volatile LONG closed;
    unsigned char padding[60];
};
struct VideoMapSlot {
    volatile LONG state; // FREE=0, WRITING=1, READY=2, READING=3
    uint32_t payload_bytes;
    uint64_t sequence, published_tick;
    unsigned char padding[40];
};
struct VideoSourceControl {
    uint32_t size, abi, operation, owner_pid;
    wchar_t mapping_name[96];
    uint64_t generation[2];
    char account[128];
    uint32_t quality, reserved;
};
struct VideoPublishControl {
    uint32_t size, abi;
    void* mapping;
    uint64_t mapping_bytes;
    const void* pixels;
    uint64_t pixel_bytes, sequence;
};
static_assert(sizeof(VideoMapHeader) == 128 && sizeof(VideoMapSlot) == 64, "Video map ABI");
static_assert(sizeof(VideoSourceControl) == 360 && sizeof(VideoPublishControl) == 48, "Video control ABI");
static SRWLOCK video_source_lock = SRWLOCK_INIT;
static HANDLE video_mapping_handle = nullptr;
static VideoMapHeader* video_mapping_view = nullptr;
static VideoMapHeader video_mapping_identity{};
static uintptr_t video_account_root = 0, video_account_object = 0;
static char video_bound_account[128]{};
static uint32_t video_quality_hint = 0;

static bool video_account_matches(uintptr_t expected_root, uintptr_t expected_object, const char* account,
                                  uintptr_t* root_output = nullptr, uintptr_t* object_output = nullptr) {
    __try {
        const auto root = *reinterpret_cast<const uintptr_t*>(core_address(core_base, 0xb912690));
        if (!root || (expected_root && root != expected_root) ||
                *reinterpret_cast<const uintptr_t*>(root) != core_address(core_base, 0x8d804e8)) return false;
        const auto object = *reinterpret_cast<const uintptr_t*>(root + 0x68);
        if (!object || (expected_object && object != expected_object) ||
                *reinterpret_cast<const uintptr_t*>(object) != core_address(core_base, 0x8dfe2f8)) return false;
        const auto identity = reinterpret_cast<const SmallString*>(object + 0x48);
        const size_t length = strnlen_s(account, 128);
        if (!length || length >= 128 || identity->length != length || identity->capacity < length ||
                identity->capacity > 1024 * 1024) return false;
        const char* bytes = identity->capacity > 15 ?
            *reinterpret_cast<const char* const*>(identity->data) : identity->data;
        const bool valid = bytes && !std::memcmp(bytes, account, length) &&
            *reinterpret_cast<const uintptr_t*>(core_address(core_base, 0xb912690)) == root &&
            *reinterpret_cast<const uintptr_t*>(root + 0x68) == object;
        if (valid && root_output) *root_output = root;
        if (valid && object_output) *object_output = object;
        return valid;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

static bool video_map_layout(const VideoMapHeader& header, uint64_t bytes) {
    if (header.magic != video_map_magic || header.abi != 1 || header.format != 1 ||
            header.width < 16 || header.height < 16 || header.width > 1920 || header.height > 1920 ||
            (header.width & 1) || (header.height & 1) || header.slot_count != 3 ||
            !header.owner_pid || (!header.generation[0] && !header.generation[1])) return false;
    const uint64_t payload = uint64_t(header.width) * header.height * 3 / 2;
    if (payload > 3110400) return false;
    const uint64_t stride = 64 + ((payload + 63) & ~uint64_t(63));
    return header.slot_bytes == stride && bytes == 128 + 3 * stride && header.bytes == bytes;
}

static void video_source_close() {
    AcquireSRWLockExclusive(&video_source_lock);
    if (video_mapping_view) UnmapViewOfFile(video_mapping_view);
    if (video_mapping_handle) CloseHandle(video_mapping_handle);
    video_mapping_view = nullptr; video_mapping_handle = nullptr;
    video_mapping_identity = {};
    video_account_root = video_account_object = 0;
    video_quality_hint = 0;
    InterlockedExchange(&video_quality_failed, 0);
    SecureZeroMemory(video_bound_account, sizeof(video_bound_account));
    ReleaseSRWLockExclusive(&video_source_lock);
}

// Local publisher export: the Python producer calls this in its own process.
// It does not execute a remote thread or send frames through the message queue.
extern "C" __declspec(dllexport) DWORD WINAPI WxPublishVideoFrame(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    __try {
        auto control = static_cast<VideoPublishControl*>(parameter);
        if (control->size != sizeof(*control) || control->abi != 1 || !control->mapping ||
                !control->pixels || !control->sequence) return ERROR_INVALID_PARAMETER;
        auto map = static_cast<VideoMapHeader*>(control->mapping);
        VideoMapHeader header = *map;
        if (!video_map_layout(header, control->mapping_bytes) || header.owner_pid != GetCurrentProcessId() ||
                InterlockedCompareExchange(&map->closed, 0, 0)) return ERROR_INVALID_DATA;
        const uint64_t payload = uint64_t(header.width) * header.height * 3 / 2;
        if (control->pixel_bytes != payload) return ERROR_INVALID_PARAMETER;
        for (unsigned i = 0; i < 3; ++i) {
            auto slot = reinterpret_cast<VideoMapSlot*>(reinterpret_cast<unsigned char*>(map) +
                                                       128 + i * header.slot_bytes);
            LONG observed = InterlockedCompareExchange(&slot->state, 1, 0);
            if (observed != 0 && InterlockedCompareExchange(&slot->state, 1, 2) != 2) continue;
            __try {
                std::memcpy(reinterpret_cast<unsigned char*>(slot) + 64, control->pixels, size_t(payload));
                slot->payload_bytes = static_cast<uint32_t>(payload);
                slot->sequence = control->sequence;
                slot->published_tick = GetTickCount64();
                InterlockedExchange64(&map->heartbeat, static_cast<LONG64>(slot->published_tick));
                InterlockedExchange(&slot->state, 2);
                return 0;
            } __finally {
                // A failed read of the caller's pixels cannot publish a partial frame.
                InterlockedCompareExchange(&slot->state, 0, 1);
            }
        }
        return ERROR_BUSY;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return ERROR_INVALID_DATA; }
}

static DWORD video_source_control(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    auto control = static_cast<VideoSourceControl*>(parameter);
    if (control->size != sizeof(*control) || control->abi != 1 ||
            (control->operation != 1 && control->operation != 2)) return ERROR_INVALID_PARAMETER;
    if (control->operation == 2) { video_source_close(); return 0; }
    if (!video_probe_enabled || !video_core_supported())
        return ERROR_NOT_SUPPORTED;
    uintptr_t root = 0, object = 0;
    if (control->quality > 1 || control->reserved) return ERROR_INVALID_PARAMETER;
    if (!video_account_matches(0, 0, control->account, &root, &object)) return ERROR_NOT_READY;
    const wchar_t prefix[] = L"Local\\wxbot-video-";
    if (std::wcsncmp(control->mapping_name, prefix, std::size(prefix) - 1) ||
            control->mapping_name[95] != 0 || !control->owner_pid) return ERROR_INVALID_PARAMETER;
    const HANDLE mapping = OpenFileMappingW(FILE_MAP_READ | FILE_MAP_WRITE, FALSE, control->mapping_name);
    if (!mapping) return GetLastError();
    auto view = static_cast<VideoMapHeader*>(MapViewOfFile(mapping, FILE_MAP_READ | FILE_MAP_WRITE, 0, 0, 128));
    VideoMapHeader header{};
    bool valid = false;
    if (view) {
        header = *view;
        valid = video_map_layout(header, header.bytes) && !header.closed && header.owner_pid == control->owner_pid &&
            header.generation[0] == control->generation[0] && header.generation[1] == control->generation[1];
        UnmapViewOfFile(view);
    }
    if (!valid) { CloseHandle(mapping); return ERROR_INVALID_DATA; }
    view = static_cast<VideoMapHeader*>(MapViewOfFile(mapping, FILE_MAP_READ | FILE_MAP_WRITE, 0, 0, header.bytes));
    if (!view) { CloseHandle(mapping); return ERROR_INVALID_DATA; }
    video_source_close();
    AcquireSRWLockExclusive(&video_source_lock);
    video_mapping_handle = mapping; video_mapping_view = view; video_mapping_identity = header;
    video_account_root = root; video_account_object = object;
    std::memcpy(video_bound_account, control->account, sizeof(video_bound_account));
    video_quality_hint = control->quality;
    ReleaseSRWLockExclusive(&video_source_lock);
    return 0;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxVideoSource(void* parameter) {
    _InterlockedIncrement(&active_callbacks);
    DWORD code = ERROR_INVALID_DATA;
    __try { code = video_source_control(parameter); }
    __finally { _InterlockedDecrement(&active_callbacks); }
    return code;
}

// On success the source lock and one slot stay owned through the original
// synchronous CPU-frame call. The caller must release both in its finally.
static VideoMapSlot* video_acquire_frame(const VideoFrameMetadata& original, VideoFrameMetadata* replacement) {
    if ((original.flags & 31) != 1 || !TryAcquireSRWLockShared(&video_source_lock)) return nullptr;
    auto view = video_mapping_view;
    const auto& header = video_mapping_identity;
    const uint64_t now = GetTickCount64();
    const uint64_t heartbeat = view ? uint64_t(InterlockedCompareExchange64(&view->heartbeat, 0, 0)) : 0;
    if (!view || !video_account_matches(video_account_root, video_account_object, video_bound_account) ||
            InterlockedCompareExchange(&view->closed, 0, 0) || !heartbeat ||
            now < heartbeat || now - heartbeat > 500 || original.width != int(header.width) ||
            original.height != int(header.height)) {
        ReleaseSRWLockShared(&video_source_lock); return nullptr;
    }
    VideoMapSlot* selected = nullptr;
    const uint32_t payload = header.width * header.height * 3 / 2;
    for (unsigned i = 0; i < 3; ++i) {
        auto slot = reinterpret_cast<VideoMapSlot*>(reinterpret_cast<unsigned char*>(view) +
                                                   128 + i * header.slot_bytes);
        if (InterlockedCompareExchange(&slot->state, 3, 2) != 2) continue;
        if (slot->payload_bytes != payload || !slot->sequence || slot->published_tick > now ||
                now - slot->published_tick > 500 || (selected && slot->sequence <= selected->sequence)) {
            InterlockedExchange(&slot->state, 0); continue;
        }
        if (selected) InterlockedExchange(&selected->state, 0);
        selected = slot;
    }
    if (!selected) { ReleaseSRWLockShared(&video_source_lock); return nullptr; }
    *replacement = original; // Preserve all native orientation, timing and routing fields.
    replacement->planes[0] = reinterpret_cast<uintptr_t>(selected) + 64;
    replacement->planes[1] = replacement->planes[0] + header.width * header.height;
    replacement->planes[2] = replacement->planes[1] + header.width * header.height / 4;
    replacement->strides[0] = int(header.width);
    replacement->strides[1] = replacement->strides[2] = int(header.width / 2);
    return selected;
}
static void video_release_frame(VideoMapSlot* slot) {
    if (!slot) return;
    // Keep the latest complete frame available until its 500 ms deadline.
    InterlockedExchange(&slot->state, 2);
    ReleaseSRWLockShared(&video_source_lock);
}
