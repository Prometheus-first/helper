#pragma once

namespace fresh_moments {
enum State : LONG { RUNNING = 1, READY = 2, FAILED = 3, QUARANTINED = 4 };
struct Config {
    uint32_t size, pid;
    char account[128];
    uint64_t operation_id;
};
struct Poll {
    uint32_t size, state, attempted, error_type;
    int32_t native_code;
    uint32_t exception;
    uint64_t operation_id, begin, count;
};
struct NativeResult {
    uint32_t error_type;
    int32_t native_code;
    SmallString message;
    SharedMessage payload;
};
static_assert(sizeof(Config) == 144 && sizeof(Poll) == 48 && sizeof(NativeResult) == 56);
struct Lease {
    Config config;
    SharedMessage app{}, kernel{}, service{};
    NativeResult result{};
    TestResult report{};
    volatile LONG state = RUNNING, cancel = 0;
    volatile LONG attempted = 0;
    uintptr_t begin = 0;
    size_t count = 0;
    explicit Lease(const Config& value) : config(value) {
        result.message.capacity = 15;
        _InterlockedIncrement(&live_tasks);
    }
    ~Lease() {
        SecureZeroMemory(&config, sizeof(config));
        _InterlockedDecrement(&live_tasks);
    }
};
static SRWLOCK pending_lock = SRWLOCK_INIT;
static std::shared_ptr<Lease> pending;

static bool release(Lease* lease) {
    guarded_release_shared(&lease->result.payload, &lease->report);
    image_free_string(&lease->result.message, &lease->report);
    guarded_release_shared(&lease->service, &lease->report);
    guarded_release_shared(&lease->kernel, &lease->report);
    guarded_release_shared(&lease->app, &lease->report);
    return !lease->report.exception;
}

static void run(Lease* lease) {
    bool returned = false;
    LONG state = FAILED;
    __try {
        using GetApp = SharedMessage* (__fastcall*)(SharedMessage*);
        using GetService = SharedMessage* (__fastcall*)(void*, SharedMessage*);
        reinterpret_cast<GetApp>(core_address(core_base, 0x47220))(&lease->app);
        if (lease->cancel || !emoticon_account_matches(lease->app, lease->config.account)) __leave;
        reinterpret_cast<GetService>(core_address(core_base, 0x36c740))(
            lease->app.object, &lease->kernel);
        if (!lease->kernel.object || !lease->kernel.control) __leave;
        reinterpret_cast<GetService>(core_moments_address(core_base, 0x108aee0))(
            lease->kernel.object, &lease->service);
        if (!lease->service.object || !lease->service.control || lease->cancel ||
                !emoticon_account_matches(lease->app, lease->config.account)) __leave;
        using Query = NativeResult* (__fastcall*)(void*, NativeResult*, uint64_t, uint64_t, bool);
        InterlockedExchange(&lease->attempted, 1);
        // Verified 4.2.0.12 native first-page callers use (0, 0, false).
        auto output = reinterpret_cast<Query>(core_moments_read_address(core_base))(
            lease->service.object, &lease->result, 0, 0, false);
        returned = true;
        if (output != &lease->result || lease->cancel || lease->result.error_type ||
                lease->result.native_code || !lease->result.payload.object ||
                !lease->result.payload.control ||
                !emoticon_account_matches(lease->app, lease->config.account)) __leave;
        const auto vector = static_cast<const uintptr_t*>(lease->result.payload.object);
        const uintptr_t begin = vector[0], end = vector[1], capacity = vector[2];
        if (begin || end || capacity) {
            if (begin < 0x10000 || begin > end || end > capacity ||
                    capacity >= 0x800000000000ull || (end - begin) % 16 ||
                    (capacity - begin) % 16 || (capacity - begin) / 16 > 10000) __leave;
        }
        lease->begin = begin;
        lease->count = (end - begin) / 16;
        state = READY;
    } __except (record_exception(GetExceptionInformation(), &lease->report)) {
        state = FAILED;
    }
    if (lease->attempted && !returned) state = QUARANTINED;
    // The result's shared references remain owned until Close, including while
    // Python decodes the immutable response vector. No report file contains posts.
    InterlockedExchange(&lease->state, state);
}

static bool copy_config(void* parameter, Config* config) {
    __try { *config = *static_cast<const Config*>(parameter); return true; }
    __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}
static bool copy_poll(void* parameter, Poll* poll, bool write) {
    __try {
        if (write) *static_cast<Poll*>(parameter) = *poll;
        else *poll = *static_cast<const Poll*>(parameter);
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

struct Admission {
    Admission() { _InterlockedIncrement(&active_callbacks); AcquireSRWLockExclusive(&pending_lock); }
    ~Admission() { ReleaseSRWLockExclusive(&pending_lock); _InterlockedDecrement(&active_callbacks); }
};
static DWORD start(void* parameter) {
    Config config{};
    if (!parameter || !copy_config(parameter, &config) || config.size != sizeof(config) ||
            config.pid != GetCurrentProcessId() || !config.operation_id ||
            !friend_valid_account(config.account)) return ERROR_INVALID_PARAMETER;
    Admission admission;
    if (pending) {
        return pending->config.operation_id == config.operation_id &&
            !std::strcmp(pending->config.account, config.account) ? ERROR_SUCCESS : ERROR_BUSY;
    }
    if (live_tasks || text_reply_pending()) return ERROR_BUSY;
    auto core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core) || core_profile_id != 4) return ERROR_NOT_SUPPORTED;
    core_base = reinterpret_cast<uintptr_t>(core);
    try {
        pending = std::make_shared<Lease>(config);
        auto lease = pending;
        std::function<void()> task = [lease]() { run(lease.get()); };
        TestConfig schedule_config{};
        schedule_config.size = sizeof(schedule_config);
        schedule_config.pid = config.pid;
        const DWORD code = schedule_task(schedule_config, &task);
        if (code) InterlockedExchange(&lease->cancel, 1);
        return code;
    } catch (...) {
        if (pending) InterlockedExchange(&pending->cancel, 1);
        return ERROR_GEN_FAILURE;
    }
}
static DWORD poll(void* parameter, bool close) {
    Poll result{};
    if (!parameter || !copy_poll(parameter, &result, false) ||
            result.size != sizeof(result) || !result.operation_id) return ERROR_INVALID_PARAMETER;
    Admission admission;
    if (!pending || pending->config.operation_id != result.operation_id) return ERROR_NOT_FOUND;
    if (close) {
        InterlockedExchange(&pending->cancel, 1);
        if (pending->state == QUARANTINED || pending.use_count() != 1 ||
                (pending->state == RUNNING && pending->attempted))
            return ERROR_BUSY;
        if (!release(pending.get())) {
            InterlockedExchange(&pending->state, QUARANTINED);
            return ERROR_INVALID_DATA;
        }
        pending.reset();
        return ERROR_SUCCESS;
    }
    result.state = InterlockedCompareExchange(&pending->state, 0, 0);
    result.attempted = InterlockedCompareExchange(&pending->attempted, 0, 0);
    if (result.state != RUNNING) {
        result.error_type = pending->result.error_type;
        result.native_code = pending->result.native_code;
        result.exception = pending->report.exception;
    }
    if (result.state == READY && !pending->cancel &&
            emoticon_account_matches(pending->app, pending->config.account)) {
        result.begin = pending->begin;
        result.count = pending->count;
    } else if (result.state == READY) result.state = FAILED;
    return copy_poll(parameter, &result, true) ? ERROR_SUCCESS : ERROR_INVALID_PARAMETER;
}
} // namespace fresh_moments

extern "C" __declspec(dllexport) DWORD WINAPI WxStartMomentsRead(void* parameter) {
    return fresh_moments::start(parameter);
}
extern "C" __declspec(dllexport) DWORD WINAPI WxPollMomentsRead(void* parameter) {
    return fresh_moments::poll(parameter, false);
}
extern "C" __declspec(dllexport) DWORD WINAPI WxCloseMomentsRead(void* parameter) {
    return fresh_moments::poll(parameter, true);
}
