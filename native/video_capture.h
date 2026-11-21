#pragma once
// Copy the existing WeChat camera input before replacement. No second camera.
static SRWLOCK video_capture_lock = SRWLOCK_INIT;
static HANDLE video_capture_handle = nullptr;
static VideoMapHeader* video_capture_view = nullptr;
static VideoMapHeader video_capture_identity{};
static uintptr_t video_capture_root = 0, video_capture_object = 0;
static char video_capture_account[128]{};
static volatile LONG64 video_capture_sequence = 0;

static void video_capture_close() {
    AcquireSRWLockExclusive(&video_capture_lock);
    if (video_capture_view) {
        InterlockedExchange(&video_capture_view->closed, 1);
        UnmapViewOfFile(video_capture_view);
    }
    if (video_capture_handle) CloseHandle(video_capture_handle);
    video_capture_handle = nullptr; video_capture_view = nullptr;
    video_capture_identity = {};
    video_capture_root = video_capture_object = 0;
    SecureZeroMemory(video_capture_account, sizeof(video_capture_account));
    ReleaseSRWLockExclusive(&video_capture_lock);
}

static DWORD video_capture_control(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    auto control = static_cast<VideoSourceControl*>(parameter);
    if (control->size != sizeof(*control) || control->abi != 1 ||
            (control->operation != 1 && control->operation != 2)) return ERROR_INVALID_PARAMETER;
    if (control->operation == 2) { video_capture_close(); return 0; }
    if (!video_probe_enabled || !video_core_supported())
        return ERROR_NOT_SUPPORTED;
    uintptr_t root = 0, object = 0;
    if (control->quality || control->reserved) return ERROR_INVALID_PARAMETER;
    if (!video_account_matches(0, 0, control->account, &root, &object)) return ERROR_NOT_READY;
    const wchar_t prefix[] = L"Local\\wxbot-video-";
    if (std::wcsncmp(control->mapping_name, prefix, std::size(prefix) - 1) ||
            control->mapping_name[95] || !control->owner_pid) return ERROR_INVALID_PARAMETER;
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
    video_capture_close();
    AcquireSRWLockExclusive(&video_capture_lock);
    video_capture_handle = mapping; video_capture_view = view; video_capture_identity = header;
    video_capture_root = root; video_capture_object = object;
    std::memcpy(video_capture_account, control->account, sizeof(video_capture_account));
    InterlockedExchange64(&video_capture_sequence, 0);
    ReleaseSRWLockExclusive(&video_capture_lock);
    return 0;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxVideoCapture(void* parameter) {
    _InterlockedIncrement(&active_callbacks);
    DWORD code = ERROR_INVALID_DATA;
    __try { code = video_capture_control(parameter); }
    __finally { _InterlockedDecrement(&active_callbacks); }
    return code;
}

static void video_capture_frame(const VideoFrameMetadata& frame) {
    if (!TryAcquireSRWLockShared(&video_capture_lock)) return;
    VideoMapSlot* owned = nullptr;
    __try {
        const auto view = video_capture_view;
        const auto& header = video_capture_identity;
        if (!view || InterlockedCompareExchange(&view->closed, 0, 0)) __leave;
        if (!video_account_matches(video_capture_root, video_capture_object, video_capture_account)) {
            InterlockedExchange(&view->closed, 1); __leave;
        }
        if ((frame.flags & 31) != 1 || frame.width != int(header.width) || frame.height != int(header.height)) __leave;
        // Validate all planes before publishing any bytes.
        if (!frame.planes[0] || !frame.planes[1] || !frame.planes[2] ||
                frame.strides[0] < frame.width || frame.strides[0] > 8192 ||
                frame.strides[1] < frame.width / 2 || frame.strides[1] > 8192 ||
                frame.strides[2] < frame.width / 2 || frame.strides[2] > 8192) __leave;
        for (unsigned i = 0; i < 3; ++i) {
            auto slot = reinterpret_cast<VideoMapSlot*>(reinterpret_cast<unsigned char*>(view) + 128 + i * header.slot_bytes);
            if (InterlockedCompareExchange(&slot->state, 1, 0) == 0 ||
                    InterlockedCompareExchange(&slot->state, 1, 2) == 2) { owned = slot; break; }
        }
        if (!owned) __leave;
        __try {
            auto output = reinterpret_cast<unsigned char*>(owned) + 64;
            for (unsigned p = 0; p < 3; ++p) {
                const int rows = p ? frame.height / 2 : frame.height;
                const int columns = p ? frame.width / 2 : frame.width;
                const auto input = reinterpret_cast<const unsigned char*>(frame.planes[p]);
                for (int row = 0; row < rows; ++row) {
                    std::memcpy(output, input + size_t(row) * frame.strides[p], columns);
                    output += columns;
                }
            }
            owned->payload_bytes = header.width * header.height * 3 / 2;
            owned->sequence = uint64_t(InterlockedIncrement64(&video_capture_sequence));
            owned->published_tick = GetTickCount64();
            InterlockedExchange64(&view->heartbeat, static_cast<LONG64>(owned->published_tick));
            InterlockedExchange(&owned->state, 2);
        } __except (EXCEPTION_EXECUTE_HANDLER) { }
    } __finally {
        if (owned) InterlockedCompareExchange(&owned->state, 0, 1);
        ReleaseSRWLockShared(&video_capture_lock);
    }
}

struct VideoReadControl {
    uint32_t size, abi;
    void* mapping;
    uint64_t mapping_bytes;
    void* pixels;
    uint64_t pixel_bytes, sequence, captured_tick;
};
static_assert(sizeof(VideoReadControl) == 56, "Video capture reader ABI");

// Local worker export. Copy while holding slot ownership, never racing producer.
extern "C" __declspec(dllexport) DWORD WINAPI WxReadVideoFrame(void* parameter) {
    VideoMapSlot* selected = nullptr;
    DWORD result = ERROR_NO_MORE_ITEMS;
    __try {
        if (!parameter) return ERROR_INVALID_PARAMETER;
        auto control = static_cast<VideoReadControl*>(parameter);
        if (control->size != sizeof(*control) || control->abi != 1 || !control->mapping || !control->pixels)
            return ERROR_INVALID_PARAMETER;
        auto map = static_cast<VideoMapHeader*>(control->mapping);
        VideoMapHeader header = *map;
        if (!video_map_layout(header, control->mapping_bytes) || InterlockedCompareExchange(&map->closed, 0, 0))
            return ERROR_INVALID_DATA;
        const uint32_t payload = header.width * header.height * 3 / 2;
        if (control->pixel_bytes != payload) return ERROR_INVALID_PARAMETER;
        const auto now = GetTickCount64();
        for (unsigned i = 0; i < 3; ++i) {
            auto slot = reinterpret_cast<VideoMapSlot*>(reinterpret_cast<unsigned char*>(map) + 128 + i * header.slot_bytes);
            if (InterlockedCompareExchange(&slot->state, 3, 2) != 2) continue;
            if (slot->payload_bytes != payload || slot->sequence <= control->sequence ||
                    slot->published_tick > now || now - slot->published_tick > 500 ||
                    (selected && slot->sequence <= selected->sequence)) {
                InterlockedExchange(&slot->state, 0); continue;
            }
            if (selected) InterlockedExchange(&selected->state, 0);
            selected = slot;
        }
        if (selected) {
            std::memcpy(control->pixels, reinterpret_cast<unsigned char*>(selected) + 64, payload);
            control->sequence = selected->sequence;
            control->captured_tick = selected->published_tick;
            result = 0;
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) { result = ERROR_INVALID_DATA; }
    if (selected) InterlockedExchange(&selected->state, 0);
    return result;
}
