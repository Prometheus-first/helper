#pragma once
// Experimental CPU-frame bridge. The VoIP profile is independent of Weixin.dll.
// No pixel data, addresses, account identifiers or file paths are reported.
struct VideoFrameMetadata {
    int32_t width, height;
    uint32_t flags, reserved;
    uintptr_t auxiliary, planes[3], extra;
    int32_t strides[3];
    uint32_t tail;
};
static_assert(sizeof(VideoFrameMetadata) == 72, "Video metadata ABI");
static_assert(offsetof(VideoFrameMetadata, planes) == 24, "Video plane ABI");
static_assert(offsetof(VideoFrameMetadata, strides) == 56, "Video stride ABI");
struct VideoProbeControl {
    uint32_t size, abi, operation, armed;
    uint64_t callbacks, invalid_metadata, replaced_frames, first_tick, last_tick;
    int32_t input_width, input_height;
    uint32_t input_format;
    int32_t output_width, output_height;
    uint32_t output_format;
    int32_t input_strides[3], output_strides[3];
};
static_assert(sizeof(VideoProbeControl) == 104, "Video probe ABI");
using VideoPreEncode = int (__fastcall*)(void*, VideoFrameMetadata*);
static VideoPreEncode original_video_preencode = nullptr;
static HMODULE video_module = nullptr;
static bool video_hook_created = false;
static bool video_probe_enabled = false;
static SRWLOCK video_stats_lock = SRWLOCK_INIT;
static VideoProbeControl video_stats{};
// Common VideoEnc2Send entry. The optional preprocessing path is bypassed
// by the active 4.2 camera pipeline, as confirmed by an observer-only call.
static constexpr uintptr_t video_preencode_rva = 0x6dd3f0;
static constexpr uintptr_t video_encoder_rva = 0x6f3050;
using VideoEncode = uintptr_t (__fastcall*)(void*, VideoFrameMetadata*, void*);
static VideoEncode original_video_encode = nullptr;
static bool video_encoder_hook_created = false;
static DWORD video_tls = TLS_OUT_OF_INDEXES;
static volatile LONG video_quality_failed = 0;
struct VideoQuality {
    uint32_t size, abi, width, height, format, bitrate_bps, fps_cap, native_status;
    uint64_t encoder_calls;
    uint32_t fallback, reserved;
};
static_assert(sizeof(VideoQuality) == 48, "Video quality ABI");
static VideoQuality video_quality{};

static bool video_core_supported() {
    const LONG profile = InterlockedCompareExchange(&core_profile_id, 0, 0);
    // Both exact 4.2 core builds load the same verified VoipEngine image.
    // The account RVAs are resolved separately through core_address().
    return profile == 3 || profile == 4;
}

static bool video_fingerprint(HMODULE module) {
    uint32_t timestamp = 0, image_size = 0;
    if (!core_image_identity(module, &timestamp, &image_size) ||
            timestamp != 0x6ae05a4a || image_size != 0x161f000) return false;
    wchar_t path[32768]{};
    if (!GetModuleFileNameW(module, path, static_cast<DWORD>(std::size(path)))) return false;
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, nullptr,
                              OPEN_EXISTING, FILE_FLAG_SEQUENTIAL_SCAN, nullptr);
    if (file == INVALID_HANDLE_VALUE) return false;
    LARGE_INTEGER bytes{};
    bool valid = GetFileSizeEx(file, &bytes) && bytes.QuadPart == 20966960;
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    static constexpr unsigned char expected[32] = {
        0x4b,0x50,0x80,0x79,0xea,0xba,0xf0,0x44,0x68,0xa0,0xc0,0x84,0x44,0x1d,0x51,0xf3,
        0xa7,0xf7,0x97,0x12,0x67,0x87,0x56,0xaf,0x66,0xdb,0xde,0x90,0xfb,0x75,0xd0,0x07};
    if (valid && BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) >= 0) {
        if (BCryptCreateHash(algorithm, &hash, nullptr, 0, nullptr, 0, 0) >= 0) {
            unsigned char buffer[65536], digest[32];
            DWORD count = 0;
            while (valid) {
                valid = ReadFile(file, buffer, sizeof(buffer), &count, nullptr) != FALSE;
                if (!valid || !count) break;
                valid = BCryptHashData(hash, buffer, count, 0) >= 0;
            }
            valid = valid && BCryptFinishHash(hash, digest, sizeof(digest), 0) >= 0 &&
                std::equal(expected, expected + 32, digest);
            BCryptDestroyHash(hash);
        } else valid = false;
        BCryptCloseAlgorithmProvider(algorithm, 0);
    } else valid = false;
    CloseHandle(file);
    return valid;
}

static bool video_read_metadata(const VideoFrameMetadata* frame, VideoFrameMetadata* output) {
    __try {
        if (!frame) return false;
        std::memcpy(output, frame, sizeof(*output));
        return output->width > 0 && output->width <= 8192 &&
            output->height > 0 && output->height <= 8192;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

#include "video_frames.h"
#include "video_capture.h"

static void video_read_quality(void* encoder, VideoQuality* quality) {
    __try {
        const auto bytes = static_cast<const unsigned char*>(encoder);
        const auto codec = *reinterpret_cast<const unsigned char* const*>(bytes + 0xe0);
        if (!codec) return;
        const auto bitrate = *reinterpret_cast<const uint32_t*>(codec + 0x20);
        const auto fps = *reinterpret_cast<const uint16_t*>(codec + 0x10);
        if (bitrate <= 100000000) quality->bitrate_bps = bitrate;
        if (fps <= 120) quality->fps_cap = fps;
    } __except (EXCEPTION_EXECUTE_HANDLER) { }
}

struct VideoBitrateScope {
    volatile LONG* field;
    LONG saved;
};
static VideoBitrateScope video_bitrate_scope(void* encoder) {
    VideoBitrateScope scope{};
    if (video_tls == TLS_OUT_OF_INDEXES || !TlsGetValue(video_tls) ||
            InterlockedCompareExchange(&video_quality_failed, 0, 0)) return scope;
    __try {
        const auto backend = *reinterpret_cast<unsigned char**>(static_cast<unsigned char*>(encoder) + 0xd8);
        if (!backend || !video_module) return scope;
        const auto table = *reinterpret_cast<void***>(backend);
        const auto setter = reinterpret_cast<unsigned char*>(video_module) + 0x748e30;
        static constexpr unsigned char prefix[] = {0x89,0x91,0xc8,0,0,0,0xc3};
        if (!table || table[7] != setter || std::memcmp(setter, prefix, sizeof(prefix))) return scope;
        // Verified SetBitrate only writes this backend's target field. The
        // encoder copies it into its per-frame configuration. Restore it in
        // finally so native frames and later calls keep their QoS settings.
        auto field = reinterpret_cast<volatile LONG*>(backend + 0xc8);
        const LONG saved = InterlockedCompareExchange(field, 0, 0);
        if (saved >= 64000 && saved < 1200000 &&
                InterlockedCompareExchange(field, 1200000, saved) == saved) scope = {field, saved};
    } __except (EXCEPTION_EXECUTE_HANDLER) { }
    return scope;
}

static uintptr_t __fastcall video_encode(void* encoder, VideoFrameMetadata* frame, void* metadata) {
    _InterlockedIncrement(&active_callbacks);
    uintptr_t result = 0;
    VideoFrameMetadata observed{};
    VideoQuality quality{};
    VideoBitrateScope bitrate{};
    __try {
        const bool valid = video_read_metadata(frame, &observed);
        video_read_quality(encoder, &quality);
        bitrate = video_bitrate_scope(encoder);
        if (bitrate.field) quality.bitrate_bps = 1200000;
        result = original_video_encode(encoder, frame, metadata);
        if (int32_t(result) < 0 && video_tls != TLS_OUT_OF_INDEXES && TlsGetValue(video_tls))
            InterlockedExchange(&video_quality_failed, 1);
        if (valid && TryAcquireSRWLockExclusive(&video_stats_lock)) {
            quality.width = observed.width; quality.height = observed.height;
            quality.format = observed.flags & 31; quality.native_status = uint32_t(result);
            quality.encoder_calls = video_quality.encoder_calls + 1;
            video_quality = quality;
            ReleaseSRWLockExclusive(&video_stats_lock);
        }
    } __finally {
        if (bitrate.field) InterlockedCompareExchange(bitrate.field, bitrate.saved, 1200000);
        _InterlockedDecrement(&active_callbacks);
    }
    return result;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxVideoQuality(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    auto output = static_cast<VideoQuality*>(parameter);
    if (output->size != sizeof(*output) || output->abi != 1) return ERROR_INVALID_PARAMETER;
    _InterlockedIncrement(&active_callbacks);
    __try {
        AcquireSRWLockShared(&video_stats_lock);
        *output = video_quality;
        ReleaseSRWLockShared(&video_stats_lock);
        output->size = sizeof(*output); output->abi = 1;
        output->fallback = uint32_t(InterlockedCompareExchange(&video_quality_failed, 0, 0));
    } __finally { _InterlockedDecrement(&active_callbacks); }
    return 0;
}

static int __fastcall video_preencode(void* engine, VideoFrameMetadata* input) {
    _InterlockedIncrement(&active_callbacks);
    int result = 0;
    VideoFrameMetadata before{}, replacement{};
    VideoMapSlot* slot = nullptr;
    __try {
        const bool valid = video_read_metadata(input, &before);
        if (valid) {
            video_capture_frame(before);
            slot = video_acquire_frame(before, &replacement);
        }
        // This exact profile's CPU path reads the explicit size-format override
        // at engine+0x224 before invoking its normal QoS scaler and encoder.
        // A source-only hint uses the verified 640/480 table entry (8).
        auto size_override = reinterpret_cast<volatile LONG*>(static_cast<unsigned char*>(engine) + 0x224);
        LONG saved_size = 0;
        bool size_changed = false;
        void* saved_tls = video_tls == TLS_OUT_OF_INDEXES ? nullptr : TlsGetValue(video_tls);
        if (slot && video_quality_hint == 1 && video_tls != TLS_OUT_OF_INDEXES &&
                !InterlockedCompareExchange(&video_quality_failed, 0, 0)) {
            saved_size = InterlockedCompareExchange(size_override, 0, 0);
            if (saved_size >= 0 && saved_size <= 12) {
                if (TlsSetValue(video_tls, reinterpret_cast<void*>(1))) {
                    InterlockedExchange(size_override, 8);
                    size_changed = true;
                }
            }
        }
        __try {
            // No decode, file I/O, allocation or blocking in the frame callback.
            result = original_video_preencode(engine, slot ? &replacement : input);
        } __finally {
            if (size_changed) InterlockedCompareExchange(size_override, saved_size, 8);
            if (video_tls != TLS_OUT_OF_INDEXES) TlsSetValue(video_tls, saved_tls);
            video_release_frame(slot);
        }
        if (TryAcquireSRWLockExclusive(&video_stats_lock)) {
            ++video_stats.callbacks;
            if (slot) {
                ++video_stats.replaced_frames;
                video_stats.output_width = replacement.width;
                video_stats.output_height = replacement.height;
                video_stats.output_format = replacement.flags & 31;
                std::copy(replacement.strides, replacement.strides + 3, video_stats.output_strides);
            }
            if (!video_stats.first_tick) video_stats.first_tick = GetTickCount64();
            video_stats.last_tick = GetTickCount64();
            if (!valid) ++video_stats.invalid_metadata;
            else {
                video_stats.input_width = before.width;
                video_stats.input_height = before.height;
                video_stats.input_format = before.flags & 31;
                std::copy(before.strides, before.strides + 3, video_stats.input_strides);
            }
            ReleaseSRWLockExclusive(&video_stats_lock);
        }
    } __finally { _InterlockedDecrement(&active_callbacks); }
    return result;
}

static DWORD video_probe_disable() {
    video_capture_close();
    video_source_close();
    if (video_encoder_hook_created) {
        const auto status = MH_DisableHook(reinterpret_cast<unsigned char*>(video_module) + video_encoder_rva);
        if (status != MH_OK && status != MH_ERROR_DISABLED) return 600 + status;
    }
    if (!video_hook_created) return 0;
    const auto status = MH_DisableHook(reinterpret_cast<unsigned char*>(video_module) + video_preencode_rva);
    if (status == MH_OK || status == MH_ERROR_DISABLED) video_probe_enabled = false;
    return status == MH_OK || status == MH_ERROR_DISABLED ? 0 : 600 + status;
}
static DWORD video_probe_remove() {
    if (video_encoder_hook_created) {
        const auto status = MH_RemoveHook(reinterpret_cast<unsigned char*>(video_module) + video_encoder_rva);
        if (status != MH_OK) return 600 + status;
        video_encoder_hook_created = false;
    }
    if (!video_hook_created) return 0;
    const auto status = MH_RemoveHook(reinterpret_cast<unsigned char*>(video_module) + video_preencode_rva);
    if (status != MH_OK) return 600 + status;
    video_hook_created = false;
    if (video_tls != TLS_OUT_OF_INDEXES) { TlsFree(video_tls); video_tls = TLS_OUT_OF_INDEXES; }
    FreeLibrary(video_module);
    video_module = nullptr;
    return 0;
}

static DWORD video_probe_control(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    auto control = static_cast<VideoProbeControl*>(parameter);
    if (control->size != sizeof(*control) || control->abi != 1 ||
            control->operation < 1 || control->operation > 3) return ERROR_INVALID_PARAMETER;
    if (control->operation == 1 && !video_hook_created) {
        if (!video_core_supported()) return ERROR_NOT_SUPPORTED;
        HMODULE module = nullptr;
        if (!GetModuleHandleExW(0, L"VoipEngine.dll", &module)) return ERROR_MOD_NOT_FOUND;
        if (!video_fingerprint(module)) { FreeLibrary(module); return ERROR_REVISION_MISMATCH; }
        auto target = reinterpret_cast<unsigned char*>(module) + video_preencode_rva;
        // Also reject an unexpected live entry or an independently patched prologue.
        static constexpr unsigned char prefix[] = {0x48,0x89,0x5c,0x24,0x18,0x55,0x56,0x57};
        if (std::memcmp(target, prefix, sizeof(prefix))) { FreeLibrary(module); return ERROR_REVISION_MISMATCH; }
        auto status = MH_Initialize();
        if (status != MH_OK && status != MH_ERROR_ALREADY_INITIALIZED) { FreeLibrary(module); return 600 + status; }
        status = MH_CreateHook(target, reinterpret_cast<void*>(&video_preencode),
                               reinterpret_cast<void**>(&original_video_preencode));
        if (status != MH_OK) { FreeLibrary(module); return 600 + status; }
        video_module = module;
        video_hook_created = true;
        video_tls = TlsAlloc();
        if (video_tls == TLS_OUT_OF_INDEXES) return ERROR_NOT_ENOUGH_MEMORY;
        AcquireSRWLockExclusive(&video_stats_lock);
        video_stats = {};
        video_quality = {};
        ReleaseSRWLockExclusive(&video_stats_lock);
        const auto encoder_target = reinterpret_cast<unsigned char*>(module) + video_encoder_rva;
        static constexpr unsigned char encoder_prefix[] = {0x48,0x89,0x5c,0x24,0x10,0x48,0x89,0x6c};
        if (std::memcmp(encoder_target, encoder_prefix, sizeof(encoder_prefix))) return ERROR_REVISION_MISMATCH;
        status = MH_CreateHook(encoder_target, reinterpret_cast<void*>(&video_encode),
                               reinterpret_cast<void**>(&original_video_encode));
        if (status != MH_OK) return 600 + status;
        video_encoder_hook_created = true;
    }
    if (control->operation == 1) {
        if (!video_encoder_hook_created) return ERROR_NOT_READY;
        const auto encoder_status = MH_EnableHook(reinterpret_cast<unsigned char*>(video_module) + video_encoder_rva);
        if (encoder_status != MH_OK && encoder_status != MH_ERROR_ENABLED) return 600 + encoder_status;
        const auto status = MH_EnableHook(reinterpret_cast<unsigned char*>(video_module) + video_preencode_rva);
        if (status != MH_OK && status != MH_ERROR_ENABLED) return 600 + status;
        video_probe_enabled = true;
    } else if (control->operation == 3) {
        const DWORD code = video_probe_disable();
        if (code) return code;
    }
    AcquireSRWLockShared(&video_stats_lock);
    auto result = video_stats;
    ReleaseSRWLockShared(&video_stats_lock);
    result.size = sizeof(result); result.abi = 1; result.operation = control->operation;
    result.armed = video_probe_enabled;
    *control = result;
    return 0;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxVideoProbe(void* parameter) {
    _InterlockedIncrement(&active_callbacks);
    DWORD code = ERROR_INVALID_DATA;
    __try { code = video_probe_control(parameter); }
    __finally { _InterlockedDecrement(&active_callbacks); }
    return code;
}
