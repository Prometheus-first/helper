// Version-locked small-program commands through the client's native IPC.
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#include <bcrypt.h>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <iterator>
#include <algorithm>
#include "core_version.h"
#include "vendor/minhook-1.3.4/include/MinHook.h"

struct CommandConfig {
    uint32_t size, pid, stage, result, response_size, request_size;
    uint32_t responses_seen, largest_response;
    char call_id[65], request[16384], response[1048576];
};
static_assert(sizeof(CommandConfig) == 1065060, "CommandConfig ABI");
static_assert(offsetof(CommandConfig, stage) == 8 && offsetof(CommandConfig, call_id) == 32, "CommandConfig header ABI");
static_assert(offsetof(CommandConfig, request) == 97 && offsetof(CommandConfig, response) == 16481, "CommandConfig buffers ABI");

namespace {
SRWLOCK lifecycle_lock = SRWLOCK_INIT;
SRWLOCK response_lock = SRWLOCK_INIT;
volatile LONG native_calls = 0, hook_calls = 0, observing = 0;
CommandConfig* command_output = nullptr;
char selected_call_id[65]{}, request_snapshot[16384]{};
uint32_t selected_call_length = 0, selected_request_size = 0;
using Dispatcher = uintptr_t(__fastcall*)(uint32_t, const char*, const char*, uint32_t);
Dispatcher original_dispatch = nullptr;
void* dispatch_address = nullptr;
bool mh_initialized = false, hook_created = false, hook_enabled = false;

const unsigned char CORE_HASH[32] = {
    0x2b,0x36,0x77,0xc3,0xf9,0x32,0xda,0xaa,0x4e,0xf8,0x4c,0x8b,0x2a,0xcd,0x0a,0xee,
    0x12,0xcb,0xe5,0xa7,0x79,0xff,0x9e,0x74,0x0c,0x37,0x07,0x61,0xe5,0xc2,0x91,0xce
};
const unsigned char HOST_HASH[32] = {
    0x33,0x1f,0x44,0xc8,0x11,0xb5,0xf2,0x48,0x9a,0xdd,0x4e,0xbc,0xec,0x82,0xa7,0x9b,
    0x90,0xff,0xbe,0xca,0x99,0x1a,0xaa,0xab,0x4c,0x16,0xde,0xbe,0x1d,0xf5,0xf4,0x26
};

bool verified(HMODULE module, LONGLONG expected_size, const unsigned char* expected_hash) {
    wchar_t path[32768]{};
    if (!module) return false;
    const DWORD path_length = GetModuleFileNameW(module, path, static_cast<DWORD>(std::size(path)));
    if (!path_length || path_length >= std::size(path)) return false;
    HANDLE file = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_DELETE,
                              nullptr, OPEN_EXISTING, 0, nullptr);
    SecureZeroMemory(path, sizeof(path));
    if (file == INVALID_HANDLE_VALUE) return false;
    LARGE_INTEGER size{};
    bool valid = GetFileSizeEx(file, &size) && size.QuadPart == expected_size;
    BCRYPT_ALG_HANDLE algorithm = nullptr;
    BCRYPT_HASH_HANDLE hash = nullptr;
    if (valid && BCryptOpenAlgorithmProvider(&algorithm, BCRYPT_SHA256_ALGORITHM, nullptr, 0) >= 0) {
        if (BCryptCreateHash(algorithm, &hash, nullptr, 0, nullptr, 0, 0) >= 0) {
            unsigned char bytes[65536]{}, digest[32]{};
            LONGLONG total = 0;
            DWORD count = 0;
            while (valid) {
                valid = ReadFile(file, bytes, sizeof(bytes), &count, nullptr) != FALSE;
                if (!valid || !count) break;
                total += count;
                valid = total <= expected_size && BCryptHashData(hash, bytes, count, 0) >= 0;
            }
            valid = valid && total == expected_size && BCryptFinishHash(hash, digest, sizeof(digest), 0) >= 0 &&
                    std::memcmp(expected_hash, digest, sizeof(digest)) == 0;
            SecureZeroMemory(bytes, sizeof(bytes));
            SecureZeroMemory(digest, sizeof(digest));
            BCryptDestroyHash(hash);
        } else valid = false;
        BCryptCloseAlgorithmProvider(algorithm, 0);
    } else valid = false;
    CloseHandle(file);
    return valid;
}

bool contains_call_id(const char* data, uint32_t size) {
    // This is only an admission filter. The Python transport validates the JSON correlation exactly.
    const uint32_t limit = size < 65536u ? size : 65536u;
    if (!selected_call_length || selected_call_length > limit) return false;
    for (uint32_t offset = 0; offset <= limit - selected_call_length; ++offset) {
        if (std::memcmp(data + offset, selected_call_id, selected_call_length) == 0) return true;
    }
    return false;
}

void observe_response(const char* name, const char* data, uint32_t size) {
    __try {
        static const char event_name[] = "xweb_return_cdp_result";
        if (!InterlockedCompareExchange(&observing, 0, 0) || !name || !data || !size ||
            strnlen_s(name, sizeof(event_name)) != sizeof(event_name) - 1 ||
            std::memcmp(name, event_name, sizeof(event_name)) != 0) return;
        AcquireSRWLockExclusive(&response_lock);
        __try {
            auto out = command_output;
            if (!InterlockedCompareExchange(&observing, 0, 0) || !out) return;
            if (out->responses_seen != UINT32_MAX) ++out->responses_seen;
            if (size > out->largest_response) out->largest_response = size;
            if (!contains_call_id(data, size)) return;
            // One matching receipt owns this slot; later callbacks must never overwrite it.
            InterlockedExchange(&observing, 0);
            if (size >= sizeof(out->response)) {
                out->result = ERROR_INSUFFICIENT_BUFFER;
                out->response_size = size;
                MemoryBarrier();
                InterlockedExchange(reinterpret_cast<volatile LONG*>(&out->stage), 3);
                return;
            }
            std::memcpy(out->response, data, size);
            out->response[size] = 0;
            out->response_size = size;
            MemoryBarrier();
            InterlockedExchange(reinterpret_cast<volatile LONG*>(&out->stage), 2);
        } __finally {
            ReleaseSRWLockExclusive(&response_lock);
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        InterlockedExchange(&observing, 0);
    }
}

uintptr_t __fastcall dispatch_hook(uint32_t type, const char* name, const char* data, uint32_t size) {
    InterlockedIncrement(&hook_calls);
    uintptr_t result = 0;
    __try {
        observe_response(name, data, size);
        // Observation must not replace or suppress the client's own dispatcher.
        result = original_dispatch(type, name, data, size);
    } __finally {
        InterlockedDecrement(&hook_calls);
    }
    return result;
}

bool has_slot_or_hooks() {
    return command_output || selected_call_length || selected_request_size || dispatch_address || original_dispatch ||
           mh_initialized || hook_created || hook_enabled || InterlockedCompareExchange(&observing, 0, 0) ||
           InterlockedCompareExchange(&hook_calls, 0, 0);
}

DWORD command_impl(CommandConfig* config) {
    if (has_slot_or_hooks()) return ERROR_BUSY;
    if (!config || config->size != sizeof(CommandConfig) || config->pid != GetCurrentProcessId()) return ERROR_INVALID_PARAMETER;
    const uint32_t request_size = config->request_size;
    const size_t call_length = strnlen_s(config->call_id, sizeof(config->call_id));
    if (!request_size || request_size >= sizeof(config->request) ||
        strnlen_s(config->request, sizeof(config->request)) != request_size ||
        !call_length || call_length >= sizeof(config->call_id)) return ERROR_INVALID_PARAMETER;
    for (size_t index = 0; index < call_length; ++index) {
        const auto value = static_cast<unsigned char>(config->call_id[index]);
        if (value < 0x21 || value > 0x7e) return ERROR_INVALID_PARAMETER;
    }
    HMODULE host = GetModuleHandleW(L"wmpf_host_export_x64.dll");
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!verified(host, 6189608, HOST_HASH) || !verified(core, 205932072, CORE_HASH)) return ERROR_REVISION_MISMATCH;
    const auto host_base = reinterpret_cast<uintptr_t>(host);
    const auto core_base = reinterpret_cast<uintptr_t>(core);
    const auto dos = reinterpret_cast<const IMAGE_DOS_HEADER*>(core_base);
    const auto nt = reinterpret_cast<const IMAGE_NT_HEADERS64*>(core_base + dos->e_lfanew);
    if (dos->e_magic != IMAGE_DOS_SIGNATURE || nt->Signature != IMAGE_NT_SIGNATURE ||
        !core_select_build(205932072ull, CORE_HASH, nt->FileHeader.TimeDateStamp, nt->OptionalHeader.SizeOfImage) ||
        InterlockedCompareExchange(&core_profile_id, 0, 0) != 4) return ERROR_REVISION_MISMATCH;
    const auto dispatcher = core_address(core_base, 0x94f780);
    if (!dispatcher) return ERROR_REVISION_MISMATCH;
    static const unsigned char dispatcher_prefix[] = {0x55,0x41,0x57,0x41,0x56,0x41,0x55,0x41,0x54,0x56,0x57,0x53};
    if (std::memcmp(reinterpret_cast<const void*>(dispatcher), dispatcher_prefix, sizeof(dispatcher_prefix)))
        return ERROR_REVISION_MISMATCH;
    const auto manager = *reinterpret_cast<uintptr_t*>(host_base + 0x5d3708);
    if (!manager || !*reinterpret_cast<uintptr_t*>(manager + 0x18)) return ERROR_NOT_READY;
    const auto table = *reinterpret_cast<uintptr_t*>(manager);
    if (!table || *reinterpret_cast<uintptr_t*>(table + 32) != host_base + 0x1c10e0) return ERROR_REVISION_MISMATCH;

    // Retain the input and output until Stop confirms that no callback can access them.
    command_output = config;
    selected_call_length = static_cast<uint32_t>(call_length);
    selected_request_size = request_size;
    std::memcpy(selected_call_id, config->call_id, call_length + 1);
    std::memcpy(request_snapshot, config->request, request_size + 1);
    if (strnlen_s(selected_call_id, sizeof(selected_call_id)) != call_length ||
        strnlen_s(request_snapshot, sizeof(request_snapshot)) != request_size) return ERROR_INVALID_PARAMETER;
    config->result = 0;
    config->response_size = config->responses_seen = config->largest_response = 0;
    config->response[0] = 0;
    InterlockedExchange(reinterpret_cast<volatile LONG*>(&config->stage), 1);
    const auto initialized = MH_Initialize();
    if (initialized != MH_OK) {
        if (initialized == MH_ERROR_ALREADY_INITIALIZED) mh_initialized = true;
        return ERROR_BUSY;
    }
    mh_initialized = true;
    dispatch_address = reinterpret_cast<void*>(dispatcher);
    const auto created = MH_CreateHook(dispatch_address, reinterpret_cast<void*>(&dispatch_hook),
                                       reinterpret_cast<void**>(&original_dispatch));
    if (created != MH_OK) {
        if (created == MH_ERROR_ALREADY_CREATED) hook_created = true;
        return ERROR_INVALID_FUNCTION;
    }
    hook_created = true;
    if (!original_dispatch) return ERROR_INVALID_FUNCTION;
    // Mark a possibly enabled hook before calling the library, so an exceptional return remains guarded.
    hook_enabled = true;
    if (MH_EnableHook(dispatch_address) != MH_OK) return ERROR_INVALID_FUNCTION;
    InterlockedExchange(&observing, 1);
    using Send = int(__fastcall*)(void*, const char*, uint32_t, const char*, uint32_t);
    const auto sent = reinterpret_cast<Send>(host_base + 0x1c10e0)(reinterpret_cast<void*>(manager),
                        "wechat_call_cdp_command", 0xffffffffu, request_snapshot, selected_request_size);
    AcquireSRWLockExclusive(&response_lock);
    __try {
        // A synchronous oversized callback may already have set the more specific error.
        if (InterlockedCompareExchange(reinterpret_cast<volatile LONG*>(&config->stage), 0, 0) != 3)
            config->result = static_cast<uint32_t>(sent);
    } __finally {
        ReleaseSRWLockExclusive(&response_lock);
    }
    return ERROR_SUCCESS;
}

DWORD stop_impl() {
    InterlockedExchange(&observing, 0);
    if (mh_initialized) {
        // Disable first, then drain callbacks before freeing their trampoline or output pointer.
        const auto disabled = MH_DisableHook(MH_ALL_HOOKS);
        if (disabled != MH_OK && disabled != MH_ERROR_DISABLED) return ERROR_BUSY;
        hook_enabled = false;
    } else if (hook_created || hook_enabled) return ERROR_BUSY;
    const auto deadline = GetTickCount64() + 2000;
    while (InterlockedCompareExchange(&hook_calls, 0, 0) && GetTickCount64() < deadline) Sleep(1);
    if (InterlockedCompareExchange(&hook_calls, 0, 0)) return ERROR_BUSY;
    if (hook_created) {
        const auto removed = MH_RemoveHook(dispatch_address);
        if (removed != MH_OK && removed != MH_ERROR_NOT_CREATED) return ERROR_BUSY;
        hook_created = false;
    }
    if (mh_initialized) {
        const auto uninitialized = MH_Uninitialize();
        if (uninitialized != MH_OK && uninitialized != MH_ERROR_NOT_INITIALIZED) return ERROR_BUSY;
        mh_initialized = false;
    }
    hook_enabled = false;
    original_dispatch = nullptr;
    dispatch_address = nullptr;
    command_output = nullptr;
    selected_call_length = selected_request_size = 0;
    SecureZeroMemory(selected_call_id, sizeof(selected_call_id));
    SecureZeroMemory(request_snapshot, sizeof(request_snapshot));
    return ERROR_SUCCESS;
}

DWORD command_guarded(CommandConfig* config) {
    __try { return command_impl(config); }
    __except (EXCEPTION_EXECUTE_HANDLER) { return ERROR_INVALID_DATA; }
}

DWORD stop_guarded() {
    __try { return stop_impl(); }
    __except (EXCEPTION_EXECUTE_HANDLER) { return ERROR_BUSY; }
}

DWORD invoke(bool stop, CommandConfig* config) {
    InterlockedIncrement(&native_calls);
    DWORD result = ERROR_BUSY;
    __try {
        if (TryAcquireSRWLockExclusive(&lifecycle_lock)) {
            __try { result = stop ? stop_guarded() : command_guarded(config); }
            __finally { ReleaseSRWLockExclusive(&lifecycle_lock); }
        }
    } __finally {
        InterlockedDecrement(&native_calls);
    }
    return result;
}
}

extern "C" __declspec(dllexport) DWORD WINAPI MpCommand(CommandConfig* config) { return invoke(false, config); }
extern "C" __declspec(dllexport) DWORD WINAPI MpStopCommand(void*) { return invoke(true, nullptr); }
extern "C" __declspec(dllexport) DWORD WINAPI MpCanUnload(void*) {
    if (!TryAcquireSRWLockExclusive(&lifecycle_lock)) return ERROR_BUSY;
    DWORD result = ERROR_BUSY;
    __try {
        if (!InterlockedCompareExchange(&native_calls, 0, 0) && !has_slot_or_hooks()) result = ERROR_SUCCESS;
    } __finally {
        ReleaseSRWLockExclusive(&lifecycle_lock);
    }
    return result;
}

BOOL WINAPI DllMain(HINSTANCE module, DWORD reason, LPVOID) {
    if (reason == DLL_PROCESS_ATTACH) DisableThreadLibraryCalls(module);
    return TRUE;
}
