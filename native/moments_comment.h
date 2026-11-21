#pragma once
// Exact 4.2.0.12: reuse the owned post from the successful discovery read.
// Missing/expired snapshots fail before dispatch; this component never queries SNS.
namespace fresh_comment {
using Config = wxbot_sns_interaction::Config;
using Poll = wxbot_sns_interaction::PollResult;
using namespace wxbot_sns_interaction;
struct Lease {
    Config config;
    std::shared_ptr<moments_posts::Snapshot> snapshot;
    SharedMessage app{}, kernel{}, service{};
    TestResult cleanup{};
    volatile LONG state = STARTING, cancel = 0, gate = 0;
    Poll result{};
    Lease(const Config& cfg, std::shared_ptr<moments_posts::Snapshot> source)
        : config(cfg), snapshot(std::move(source)) { _InterlockedIncrement(&live_tasks); }
    ~Lease() {
        snapshot.reset();
        SecureZeroMemory(&config, sizeof(config));
        _InterlockedDecrement(&live_tasks);
    }
};
static SRWLOCK lock = SRWLOCK_INIT;
static std::shared_ptr<Lease> pending;
struct Admission {
    Admission() { _InterlockedIncrement(&active_callbacks); AcquireSRWLockExclusive(&lock); }
    ~Admission() { ReleaseSRWLockExclusive(&lock); _InterlockedDecrement(&active_callbacks); }
};
static void run(Lease* lease) {
    LONG state = FAILED;
    __try {
        if (lease->cancel) { state = CANCELED; __leave; }
        if (GetTickCount64() >= lease->snapshot->expires) { state = NOT_FOUND; __leave; }
        const auto item = lease->snapshot->find(lease->config.sns_id);
        if (!item) { state = NOT_FOUND; __leave; }
        // These local getters validate the live login/service; none requests a page.
        using GetApp = SharedMessage* (__fastcall*)(SharedMessage*);
        using GetService = SharedMessage* (__fastcall*)(void*, SharedMessage*);
        reinterpret_cast<GetApp>(core_address(core_base, 0x47220))(&lease->app);
        if (!lease->app.object || lease->app.object != lease->snapshot->app.object ||
                lease->app.control != lease->snapshot->app.control ||
                !emoticon_account_matches(lease->app, lease->config.account)) __leave;
        reinterpret_cast<GetService>(core_address(core_base, 0x36c740))(lease->app.object, &lease->kernel);
        if (!lease->kernel.object || !lease->kernel.control) __leave;
        reinterpret_cast<GetService>(core_moments_address(core_base, 0x108aee0))(
            lease->kernel.object, &lease->service);
        if (lease->service.object != lease->snapshot->service.object ||
                lease->service.control != lease->snapshot->service.control) __leave;
        if (lease->cancel || !emoticon_account_matches(lease->app, lease->config.account)) {
            state = CANCELED; __leave;
        }
        if (_InterlockedCompareExchange(&lease->gate, 1, 0) != 0) { state = CANCELED; __leave; }
        const auto sent = sns_interaction_call::run(&lease->service, item, 2,
                                                   lease->config.content, lease->config.sns_id);
        if (sent.attempted) _InterlockedIncrement64(&moments_posts::comments);
        lease->result.attempted = sent.attempted;
        lease->result.native_known = sent.native_known;
        lease->result.native_value = sent.native_value;
        lease->result.native_status = sent.status;
        lease->result.exception = sent.exception;
        if (sent.quarantine) { state = QUARANTINED; __leave; }
        state = sent.native_known && sent.native_value && !sent.status ? COMPLETED :
                sent.attempted ? UNKNOWN_RESULT : FAILED;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        lease->result.exception = GetExceptionCode();
        state = QUARANTINED;
    }
    // No foreign thread releases the native service/vector while its call runs.
    if (state != QUARANTINED) {
        guarded_release_shared(&lease->service, &lease->cleanup);
        if (!lease->cleanup.exception) guarded_release_shared(&lease->kernel, &lease->cleanup);
        if (!lease->cleanup.exception) guarded_release_shared(&lease->app, &lease->cleanup);
        if (lease->cleanup.exception) state = QUARANTINED;
        else lease->result.cleanup_complete = 1;
    }
    _InterlockedExchange(&lease->state, state);
}
static DWORD start(void* parameter) {
    Config cfg{};
    if (!wxbot_sns_interaction::copy_config(parameter, &cfg) ||
            !wxbot_sns_interaction::validate(&cfg) || cfg.action != 2) return ERROR_INVALID_PARAMETER;
    Admission admission;
    if (pending || live_tasks || text_reply_pending()) return ERROR_BUSY;
    const auto core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core) || core_profile_id != 4) return ERROR_NOT_SUPPORTED;
    core_base = reinterpret_cast<uintptr_t>(core);
    if (!sns_interaction_call::fingerprint_anchors(2)) return ERROR_REVISION_MISMATCH;
    auto snapshot = moments_posts::acquire(cfg.account, cfg.sns_id);
    if (!snapshot) return ERROR_NOT_FOUND;
    try {
        pending = std::make_shared<Lease>(cfg, std::move(snapshot));
        auto lease = pending;
        std::function<void()> task = [lease]() { run(lease.get()); };
        const DWORD code = schedule_task(cfg.base, &task);
        if (code) _InterlockedExchange(&lease->cancel, 1);
        return code;
    } catch (...) {
        if (pending) _InterlockedExchange(&pending->cancel, 1);
        return ERROR_GEN_FAILURE;
    }
}
static DWORD poll(void* parameter, bool close, bool cancel) {
    Poll request{};
    if (!wxbot_sns_interaction::copy_poll(parameter, &request) || request.size != sizeof(request) ||
            !request.operation_id) return ERROR_INVALID_PARAMETER;
    Admission admission;
    if (!pending || pending->config.operation_id != request.operation_id) return ERROR_NOT_FOUND;
    if (cancel) {
        _InterlockedExchange(&pending->cancel, 1);
        return _InterlockedCompareExchange(&pending->gate, 2, 0) == 1 ? ERROR_BUSY : ERROR_SUCCESS;
    }
    const LONG state = _InterlockedCompareExchange(&pending->state, 0, 0);
    if (close) {
        if (state == STARTING || state == QUARANTINED || pending.use_count() != 1)
            return ERROR_BUSY;
        if (!pending->result.cleanup_complete) return ERROR_BUSY;
        pending.reset();
        return ERROR_SUCCESS;
    }
    // The worker publishes its result with the final interlocked state update.
    Poll result{};
    if (state != STARTING) result = pending->result;
    result.size = sizeof(result); result.state = state;
    result.operation_id = request.operation_id;
    return wxbot_sns_interaction::write_poll(parameter, &result) ? ERROR_SUCCESS : ERROR_INVALID_PARAMETER;
}
}
extern "C" __declspec(dllexport) DWORD WINAPI WxMomentsCommentStart(void* p) { return fresh_comment::start(p); }
extern "C" __declspec(dllexport) DWORD WINAPI WxMomentsCommentPoll(void* p) { return fresh_comment::poll(p, false, false); }
extern "C" __declspec(dllexport) DWORD WINAPI WxMomentsCommentCancel(void* p) { return fresh_comment::poll(p, false, true); }
extern "C" __declspec(dllexport) DWORD WINAPI WxMomentsCommentClose(void* p) { return fresh_comment::poll(p, true, false); }
