#pragma once
// Exact 4.2.0.12: query an owned homepage vector, retain the matching post,
// then use the native plain-comment path. No fabricated post or async lookup ABI.
namespace fresh_comment {
using Config = wxbot_sns_interaction::Config;
using Poll = wxbot_sns_interaction::PollResult;
using namespace wxbot_sns_interaction;
struct Lease {
    Config config;
    fresh_moments::Lease page;
    volatile LONG state = STARTING, cancel = 0, gate = 0;
    Poll result{};
    explicit Lease(const Config& cfg) : config(cfg), page(page_config(cfg)) {}
    static fresh_moments::Config page_config(const Config& cfg) {
        fresh_moments::Config value{};
        value.size = sizeof(value); value.pid = cfg.base.pid;
        value.operation_id = cfg.operation_id;
        std::memcpy(value.account, cfg.account, sizeof(value.account));
        return value;
    }
    ~Lease() { SecureZeroMemory(&config, sizeof(config)); }
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
        fresh_moments::run(&lease->page);
        if (lease->page.state == fresh_moments::QUARANTINED) { state = QUARANTINED; __leave; }
        if (lease->page.state != fresh_moments::READY) __leave;
        const auto entries = reinterpret_cast<const SharedMessage*>(lease->page.begin);
        const SharedMessage* item = nullptr;
        for (size_t i = 0; i < lease->page.count; ++i) {
            if (entries[i].object && entries[i].control &&
                    *static_cast<const uint64_t*>(entries[i].object) == lease->config.sns_id) {
                item = &entries[i]; break;
            }
        }
        if (!item) { state = NOT_FOUND; __leave; }
        if (lease->cancel || !emoticon_account_matches(lease->page.app, lease->config.account)) {
            state = CANCELED; __leave;
        }
        if (_InterlockedCompareExchange(&lease->gate, 1, 0) != 0) { state = CANCELED; __leave; }
        const auto sent = sns_interaction_call::run(&lease->page.service, item, 2,
                                                   lease->config.content, lease->config.sns_id);
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
        if (!fresh_moments::release(&lease->page)) state = QUARANTINED;
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
    try {
        pending = std::make_shared<Lease>(cfg);
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
