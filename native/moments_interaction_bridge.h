// WeChat 4.1.15.56: owned post lookup and manual like/comment tasks.
// Public calls copy parameters; task tickets protect callable destruction.
#pragma once
#include <atomic>
#include <climits>
#include <functional>
#include <memory>
#include <new>
#include "moments_task_lifetime.h"

#ifndef WXBOT_ENABLE_SNS_INTERACTION
#define WXBOT_ENABLE_SNS_INTERACTION 1
#endif
#include "moments_interaction_call.h"

namespace wxbot_sns_interaction {
constexpr uintptr_t kLookup = 0x5737070;
constexpr uintptr_t kFutureControl = 0x8f52418;
constexpr uintptr_t kResultVtable = 0x9a31818;
constexpr uintptr_t kResultType = 0xb9ba4b0;
constexpr uintptr_t kSnsVtable = 0x95e00d8;
constexpr uintptr_t kSnsControl = 0x9195c08;
constexpr uintptr_t kTryLock = 0x754cd50;
constexpr uintptr_t kUnlock = 0x754cd70;

enum State : LONG { NEW = 0, STARTING, PREPARING, WAITING, CHECKING,
                    FOUND, NOT_FOUND, FAILED, CANCELED, QUARANTINED,
                    SUBMITTING, COMPLETED, UNKNOWN_RESULT, ALREADY_LIKED };
enum Snapshot : DWORD { SNAP_PENDING = 0, SNAP_BUSY, SNAP_FOUND, SNAP_MISSING,
                        SNAP_REJECTED, SNAP_INVALID, SNAP_UNCERTAIN };

struct Config {
    TestConfig base;             // pid must be the process holding Weixin.dll
    char account[128];
    uint64_t sns_id;             // caller-supplied: never write to logs
    uint32_t timeout_ms;         // 1000..30000; no native blocking await
    uint32_t flags;              // bit0: optional non-owning TLS diagnostic
    uint32_t action;             // 0 lookup only, 1 like, 2 plain comment
    uint32_t reserved;
    char content[1024];
    uint64_t operation_id;       // unique IPC ownership token, not a WeChat ID
};
struct PollResult {
    uint32_t size;               // sizeof(PollResult)
    uint32_t state;              // State enum; no raw pointer, content or ID
    uint32_t tls_start_bits;     // non-owning snapshot; 0 means unknown
    uint32_t tls_check_bits;     // latest check callback, may be another thread
    uint32_t submitted_tasks;
    uint32_t retired_tasks;
    uint32_t cleanup_complete;
    uint32_t reserved;
    uint32_t attempted;
    uint32_t native_known;
    uint32_t native_value;
    uint32_t was_liked;
    uint32_t native_status;
    uint32_t exception;
    uint64_t operation_id;
};
using TryLock = int (__fastcall*)(void*);
using Unlock = int (__fastcall*)(void*);
struct LockOps { TryLock try_lock; Unlock unlock; };

// Observational only: never acquires a coroutine strong ref or calls 49f2f0.
// "known" means reads succeeded, not that these pointers remain valid or that
// a future operation is permitted. A freed/reused weak control may produce an
// inaccurate instantaneous value; this diagnostic never authorizes a dispatch.
enum TlsBits : LONG { TLS_KNOWN = 1, TLS_SLOT = 2, TLS_WEAK = 4,
                      TLS_OBJECT = 8, TLS_CONTROL = 16, TLS_STRONG = 32 };
static LONG tls_snapshot_slot(void* slot) {
    if (!slot) return TLS_KNOWN;
    __try {
        LONG bits = TLS_KNOWN | TLS_SLOT;
        const auto weak = *reinterpret_cast<unsigned char**>(static_cast<unsigned char*>(slot) + 0x1f0);
        if (!weak) return bits;
        bits |= TLS_WEAK;
        const auto object = *reinterpret_cast<void**>(weak);
        const auto control = *reinterpret_cast<unsigned char**>(weak + 8);
        if (object) bits |= TLS_OBJECT;
        if (!control) return bits;
        bits |= TLS_CONTROL;
        // Observational aligned 32-bit load only; no RMW and no refcount update.
        if (__iso_volatile_load32(reinterpret_cast<const volatile int*>(control + 8)) > 0)
            bits |= TLS_STRONG;
        return bits;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return 0; }
}
static LONG tls_snapshot_native() {
    __try {
        // RVA derived from the RIP-relative TLS-index load at +0x49f2f6.
        const auto index = *reinterpret_cast<uint32_t*>(core_sns_address(core_base, 0xbb94b20));
        if (index >= 4096) return 0;
        const auto slots = reinterpret_cast<void**>(__readgsqword(0x58));
        if (!slots) return 0;
        return tls_snapshot_slot(slots[index]);
    } __except (EXCEPTION_EXECUTE_HANDLER) { return 0; }
}

// Release from the native task only. An SEH may occur after decrementing the
// count; on failure it is unsafe to retry and the lease must remain quarantined.
static bool release_once(SharedMessage* message) {
    if (!message || (!!message->object != !!message->control)) return false;
    TestResult error{};
    guarded_release_shared(message, &error);
    return error.exception == 0;
}

// No C++ objects requiring unwinding are local to this SEH-protected function.
// A nonzero out pair remains owned by the caller even if unlock fails; the
// caller must quarantine and must NOT blindly repeat a partially failed release.
static Snapshot inspect_future(const SharedMessage* future, const LockOps* ops,
                               uint64_t expected, SharedMessage* out) {
    Snapshot result = SNAP_INVALID;
    bool locked = false;
    bool unlock_uncertain = false;
    if (!out) return SNAP_INVALID;
    out->object = nullptr; out->control = nullptr;
    __try {
        __try {
            if (!future || !future->object || !future->control || !ops ||
                !ops->try_lock || !ops->unlock || !expected) __leave;
            const auto control = static_cast<unsigned char*>(future->control);
            const auto object = static_cast<unsigned char*>(future->object);
            if (object != control + 0x10 ||
                *reinterpret_cast<uintptr_t*>(control) != core_sns_address(core_base, kFutureControl) ||
                *reinterpret_cast<uint32_t*>(object + 0x10) != 0x102) __leave;
            const int rc = ops->try_lock(object + 0x10);
            if (rc == 3) { result = SNAP_BUSY; __leave; }
            if (rc != 0) { result = SNAP_UNCERTAIN; __leave; }
            locked = true;
            const auto state = *reinterpret_cast<uint32_t*>(object + 0xc4);
            if (state == 0) { result = SNAP_PENDING; __leave; }
            if (state == 2) { result = SNAP_REJECTED; __leave; }
            if (state != 1) __leave;
            const auto wrapper = *reinterpret_cast<unsigned char**>(object + 0xb8);
            if (!wrapper || *reinterpret_cast<uintptr_t*>(wrapper) != core_sns_address(core_base, kResultVtable) ||
                *reinterpret_cast<uintptr_t*>(wrapper + 0x18) != core_sns_address(core_base, kResultType) ||
                *reinterpret_cast<void**>(wrapper + 0x20) != wrapper + 8) __leave;
            const auto item = *reinterpret_cast<unsigned char**>(wrapper + 8);
            const auto owner = *reinterpret_cast<unsigned char**>(wrapper + 0x10);
            if (!item && !owner) { result = SNAP_MISSING; __leave; }
            if (!item || !owner || *reinterpret_cast<uint64_t*>(item) != expected) __leave;
            auto count = reinterpret_cast<volatile LONG*>(owner + 8);
            LONG old = _InterlockedCompareExchange(count, 0, 0);
            while (old > 0 && old < LONG_MAX) {
                const LONG observed = _InterlockedCompareExchange(count, old + 1, old);
                if (observed == old) break;
                old = observed;
            }
            if (old <= 0 || old >= LONG_MAX) __leave;
            out->object = item; out->control = owner; // one strong ref, owned
            result = SNAP_FOUND;
        } __finally {
            if (locked && ops->unlock(static_cast<unsigned char*>(future->object) + 0x10) != 0)
                unlock_uncertain = true;
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        // Includes a fault in native try-lock or unlock. Ownership/lock state
        // may be unknown even if no output pair was recorded.
        return SNAP_UNCERTAIN;
    }
    return unlock_uncertain ? SNAP_UNCERTAIN : result;
}

struct Lease {
    Config config{};
    SharedMessage app{}, kernel{}, service{}, future{}, item{};
    volatile LONG state = NEW, cancel = 0, running = 0, released = 0;
    volatile LONG tls_start_bits = 0, tls_check_bits = 0;
    volatile LONG attempted = 0, native_known = 0, native_value = 0, was_liked = 0;
    volatile LONG native_status = 0, exception = 0;
    volatile LONG dispatch_gate = 0; // 0 cancellable, 1 dispatch committed, 2 canceled
    void* retained_context = nullptr;
    ULONGLONG deadline = 0;
    std::vector<std::shared_ptr<sns_task_lifetime::Ticket>> tickets;
    // A SEH after starting native lookup or an ambiguous scheduler result means
    // we must retain everything rather than assume the native job was not queued.
    bool lookup_started = false, lookup_returned = false;
    explicit Lease(const Config& value) : config(value) { _InterlockedIncrement(&live_tasks); }
    ~Lease() { _InterlockedDecrement(&live_tasks); }
};
static SRWLOCK pending_lock = SRWLOCK_INIT;
static SRWLOCK admission_lock = SRWLOCK_INIT;
struct Admission {
    Admission() { AcquireSRWLockExclusive(&admission_lock); }
    ~Admission() { ReleaseSRWLockExclusive(&admission_lock); }
};
struct ExportLease {
    ExportLease() { _InterlockedIncrement(&active_callbacks); }
    ~ExportLease() { _InterlockedDecrement(&active_callbacks); }
};
static std::shared_ptr<Lease> pending;

static void set_state(Lease* lease, State state) {
    if (state == QUARANTINED) {
        _InterlockedExchange(&lease->state, QUARANTINED);
        return;
    }
    // A single atomic state makes quarantine irreversible even when another
    // thread was preparing to publish a normal state at the same time.
    LONG observed = _InterlockedCompareExchange(&lease->state, 0, 0);
    while (observed != QUARANTINED) {
        const LONG previous = _InterlockedCompareExchange(&lease->state, state, observed);
        if (previous == observed) return;
        observed = previous;
    }
}
static State get_state(Lease* lease) {
    return static_cast<State>(_InterlockedCompareExchange(&lease->state, 0, 0));
}
static std::shared_ptr<Lease> get_pending() {
    AcquireSRWLockShared(&pending_lock);
    auto value = pending;
    ReleaseSRWLockShared(&pending_lock);
    return value;
}

// Each release is attempted at most once. Any failure pins the lease and keeps
// live_tasks nonzero; no caller may retry a partial reference decrement.
static bool release_native(Lease* lease) {
    if (_InterlockedCompareExchange(&lease->released, 1, 0) != 0) return false;
    if (!release_once(&lease->item)) return false;
    if (!release_once(&lease->future)) return false;
    if (!release_once(&lease->service)) return false;
    if (!release_once(&lease->kernel)) return false;
    if (!release_once(&lease->app)) return false;
    return true;
}

static bool account_ok(Lease* lease) {
    bool matches = false;
    __try { matches = emoticon_account_matches(lease->app, lease->config.account); }
    __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
    return matches;
}

// Native task only. No chrono waits, UI actions, interaction operations, or
// unchecked await. Address and owner assumptions are version-specific.
static DWORD begin_lookup(Lease* lease) {
    DWORD status = ERROR_GEN_FAILURE;
    __try {
        using GetApp = SharedMessage* (__fastcall*)(SharedMessage*);
        using GetService = SharedMessage* (__fastcall*)(void*, SharedMessage*);
        using Lookup = SharedMessage* (__fastcall*)(void*, SharedMessage*, uint64_t);
        reinterpret_cast<GetApp>(core_address(core_base, 0x47220))(&lease->app);
        if (!lease->app.object || !lease->app.control || !account_ok(lease)) return ERROR_NOT_READY;
        reinterpret_cast<GetService>(core_address(core_base, 0x36c740))(lease->app.object, &lease->kernel);
        if (!lease->kernel.object || !lease->kernel.control) return ERROR_NOT_READY;
        reinterpret_cast<GetService>(core_sns_address(core_base, 0x108aee0))(lease->kernel.object, &lease->service);
        if (!lease->service.object || !lease->service.control ||
            *reinterpret_cast<uintptr_t*>(lease->service.object) != core_sns_address(core_base, kSnsVtable) ||
            *reinterpret_cast<uintptr_t*>(lease->service.control) != core_sns_address(core_base, kSnsControl))
            return ERROR_INVALID_DATA;
        const auto priv = *reinterpret_cast<unsigned char**>(static_cast<unsigned char*>(lease->service.object) + 0x428);
        if (!priv) return ERROR_NOT_READY;
        void* manager = *reinterpret_cast<void**>(priv);
        if (!manager || !account_ok(lease)) return ERROR_NOT_READY;
        // Treat a throw or fault after this line as ambiguous, never release.
        lease->lookup_started = true;
        reinterpret_cast<Lookup>(core_sns_address(core_base, kLookup))(manager, &lease->future, lease->config.sns_id);
        lease->lookup_returned = true;
        if (!lease->future.object || !lease->future.control) return ERROR_INVALID_DATA;
        const auto ctl = static_cast<unsigned char*>(lease->future.control);
        if (lease->future.object != ctl + 0x10 || *reinterpret_cast<uintptr_t*>(ctl) != core_sns_address(core_base, kFutureControl))
            return ERROR_INVALID_DATA;
        status = ERROR_SUCCESS;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return ERROR_GEN_FAILURE; }
    return status;
}

static void finish(Lease* lease, State result) {
    if (get_state(lease) == QUARANTINED) return;
    // A returned lookup owns its native worker; a failed return has unknown
    // ownership and is kept alive. All bridge callbacks have separate tickets.
    if (lease->lookup_started && !lease->lookup_returned) { set_state(lease, QUARANTINED); return; }
    if (!release_native(lease)) { set_state(lease, QUARANTINED); return; }
    set_state(lease, result);
}

static bool read_liked(Lease* lease) {
    __try {
        auto value = *(static_cast<unsigned char*>(lease->item.object) + 0xc2);
        if (value > 1) return false;
        _InterlockedExchange(&lease->was_liked, value);
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

static void found(Lease* lease) {
    if (!read_liked(lease)) { finish(lease, FAILED); return; }
    if (!lease->config.action) { finish(lease, FOUND); return; }
    if (lease->config.action == 1 && lease->was_liked) { finish(lease, ALREADY_LIKED); return; }
    if (!account_ok(lease) || lease->cancel || GetTickCount64() >= lease->deadline) {
        finish(lease, CANCELED); return;
    }
    const LONG gate = _InterlockedCompareExchange(&lease->dispatch_gate, 1, 0);
    if (gate == 2) { finish(lease, CANCELED); return; }
    if (gate != 0) { set_state(lease, QUARANTINED); return; }
    if (_InterlockedCompareExchange(&lease->attempted, 1, 0) != 0) {
        set_state(lease, QUARANTINED); return;
    }
    set_state(lease, SUBMITTING);
    const auto result = sns_interaction_call::run(&lease->service, &lease->item,
        lease->config.action, lease->config.content, lease->config.sns_id);
    _InterlockedExchange(&lease->attempted, result.attempted ? 1 : 0);
    _InterlockedExchange(&lease->native_known, result.native_known ? 1 : 0);
    _InterlockedExchange(&lease->native_value, result.native_value ? 1 : 0);
    _InterlockedExchange(&lease->native_status, static_cast<LONG>(result.status));
    _InterlockedExchange(&lease->exception, static_cast<LONG>(result.exception));
    lease->retained_context = result.retained_context;
    if (result.quarantine) { set_state(lease, QUARANTINED); return; }
    finish(lease, !result.attempted ? FAILED :
        result.native_known && result.native_value && account_ok(lease) ? COMPLETED : UNKNOWN_RESULT);
}

// Execution is allowed before the submitting thread returns: Weixin is allowed
// to run a scheduled callable immediately. Admission protects the whole submit
// scope from Close, and every returned native task remains in the ticket ledger.
static void on_start(const std::shared_ptr<Lease>& lease) {
    _InterlockedIncrement(&active_callbacks); _InterlockedIncrement(&lease->running);
    try {
    if (get_state(lease.get()) != QUARANTINED &&
             _InterlockedCompareExchange(&lease->state, PREPARING, STARTING) == STARTING) {
        if (lease->config.flags & 1) _InterlockedExchange(&lease->tls_start_bits, tls_snapshot_native());
        if (_InterlockedCompareExchange(&lease->cancel, 0, 0)) finish(lease.get(), CANCELED);
        else {
            const DWORD status = begin_lookup(lease.get());
            if (get_state(lease.get()) == QUARANTINED) { /* keep all refs */ }
            else if (status != ERROR_SUCCESS) finish(lease.get(), FAILED);
            else if (_InterlockedCompareExchange(&lease->cancel, 0, 0)) finish(lease.get(), CANCELED);
            else set_state(lease.get(), WAITING);
        }
    }
    } catch (...) { set_state(lease.get(), QUARANTINED); }
    _InterlockedDecrement(&lease->running); _InterlockedDecrement(&active_callbacks);
}

static void on_check(const std::shared_ptr<Lease>& lease) {
    _InterlockedIncrement(&active_callbacks); _InterlockedIncrement(&lease->running);
    try {
    if (get_state(lease.get()) == CHECKING) {
        if (lease->config.flags & 1) _InterlockedExchange(&lease->tls_check_bits, tls_snapshot_native());
        if (_InterlockedCompareExchange(&lease->cancel, 0, 0) || GetTickCount64() >= lease->deadline ||
            !account_ok(lease.get())) {
            // Lookup received only a copied uint64 ID and a future out pair.
            // Its remaining worker is native Weixin code and owns its promise;
            // releasing our future does not cancel or unload that worker.
            finish(lease.get(), CANCELED);
        } else {
            LockOps ops{reinterpret_cast<TryLock>(core_sns_address(core_base, kTryLock)),
                        reinterpret_cast<Unlock>(core_sns_address(core_base, kUnlock))};
            const Snapshot result = inspect_future(&lease->future, &ops, lease->config.sns_id, &lease->item);
            if (result == SNAP_PENDING || result == SNAP_BUSY) set_state(lease.get(), WAITING);
            else if (result == SNAP_UNCERTAIN) set_state(lease.get(), QUARANTINED);
            else if (!account_ok(lease.get()) || _InterlockedCompareExchange(&lease->cancel, 0, 0))
                finish(lease.get(), CANCELED);
            else if (result == SNAP_FOUND) found(lease.get());
            else finish(lease.get(), result == SNAP_MISSING ? NOT_FOUND : FAILED);
        }
    }
    } catch (...) { set_state(lease.get(), QUARANTINED); }
    _InterlockedDecrement(&lease->running); _InterlockedDecrement(&active_callbacks);
}

static bool validate(const Config* cfg) {
    if (!cfg || cfg->base.size != sizeof(Config) || cfg->base.pid != GetCurrentProcessId() ||
        cfg->base.manager || cfg->base.text[0] || cfg->base.log_path[0] || (cfg->flags & ~1u) ||
        !cfg->sns_id || !cfg->operation_id || cfg->timeout_ms < 1000 || cfg->timeout_ms > 30000 || cfg->action > 2 || cfg->reserved)
        return false;
    const size_t bytes = strnlen_s(cfg->content, sizeof(cfg->content));
    if (cfg->action == 2) {
        if (!bytes || bytes >= sizeof(cfg->content) ||
            !MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, cfg->content, static_cast<int>(bytes), nullptr, 0)) return false;
    } else if (bytes) return false;
    const size_t n = strnlen_s(cfg->account, sizeof(cfg->account));
    return n && n < sizeof(cfg->account) && valid_target(cfg->account) && !std::strchr(cfg->account, '@');
}

static bool anchors_match() {
    struct Anchor { uintptr_t rva; unsigned char bytes[16]; };
    static constexpr Anchor anchors[] = {
        {kLookup, {0x55,0x41,0x56,0x56,0x57,0x53,0x48,0x81,0xec,0x80,0,0,0,0x48,0x8d,0xac}},
        {kTryLock, {0x48,0x83,0xec,0x38,0x48,0x83,0x64,0x24,0x20,0,0x48,0x8d,0x54,0x24,0x20,0x83}},
        {kUnlock, {0x48,0x83,0xec,0x28,0x83,0x69,0x4c,0x01,0x75,0x0e,0x83,0x49,0x48,0xff,0x48,0x83}},
        {0x49f2f0, {0x56,0x57,0x48,0x83,0xec,0x28,0x8b,0x05,0x24,0x58,0x6f,0x0b,0x65,0x48,0x8b,0x14}},
    };
    __try {
        for (const auto& anchor : anchors)
            if (std::memcmp(reinterpret_cast<const void*>(core_sns_address(core_base, anchor.rva)), anchor.bytes, 16)) return false;
        return true;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

// Keep SEH away from functions with std::shared_ptr / std::function locals.
static bool copy_config(void* parameter, Config* cfg) {
    __try { if (!parameter) return false; *cfg = *static_cast<Config*>(parameter); return true; }
    __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}
static bool copy_poll(void* parameter, PollResult* value) {
    __try { if (!parameter) return false; *value = *static_cast<PollResult*>(parameter); return true; }
    __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}
static bool write_poll(void* parameter, const PollResult* value) {
    __try { *static_cast<PollResult*>(parameter) = *value; return true; }
    __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

static DWORD start_impl(void* parameter) {
    Admission admission;
    Config cfg{};
    if (!copy_config(parameter, &cfg)) return ERROR_INVALID_PARAMETER;
    if (!validate(&cfg)) return ERROR_INVALID_PARAMETER;
    const auto core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core) || _InterlockedCompareExchange(&core_profile_id, 0, 0) != 2)
        return ERROR_NOT_SUPPORTED;
    core_base = reinterpret_cast<uintptr_t>(core);
    if (!wxbot_moments_candidate::anchors_match() || !anchors_match()) return ERROR_REVISION_MISMATCH;
    if (_InterlockedCompareExchange(&live_tasks, 0, 0) || _InterlockedCompareExchange(&live_completions, 0, 0))
        return ERROR_BUSY;
    auto lease = std::make_shared<Lease>(cfg);
    auto ticket = std::make_shared<sns_task_lifetime::Ticket>();
    lease->tickets.push_back(ticket);
    lease->deadline = GetTickCount64() + cfg.timeout_ms;
    set_state(lease.get(), STARTING);
    AcquireSRWLockExclusive(&pending_lock);
    if (pending) { ReleaseSRWLockExclusive(&pending_lock); return ERROR_BUSY; }
    pending = lease; ReleaseSRWLockExclusive(&pending_lock);
    DWORD status = ERROR_GEN_FAILURE;
    try {
        auto marker = ticket->marker;
        std::function<void()> task = [lease, marker] { (void)marker; on_start(lease); };
        status = sns_task_lifetime::schedule(ticket.get(), &task);
    } catch (...) {
        set_state(lease.get(), QUARANTINED);
        ticket->uncertain = true;
        return status;
    }
    if (status) set_state(lease.get(), ticket->uncertain ? QUARANTINED : FAILED);
    return status;
}

static DWORD poll_impl(void* parameter) {
    Admission admission;
    PollResult output{};
    if (!copy_poll(parameter, &output)) return ERROR_INVALID_PARAMETER;
    if (output.size != sizeof(PollResult)) return ERROR_INVALID_PARAMETER;
    auto lease = get_pending();
    if (!lease) return ERROR_NOT_FOUND;
    if (output.operation_id != lease->config.operation_id) return ERROR_INVALID_HANDLE;
    if (get_state(lease.get()) == WAITING) {
        // Allocation occurs before state change. On allocation failure the
        // existing WAITING lease remains pollable and can still be canceled.
        auto ticket = std::make_shared<sns_task_lifetime::Ticket>();
        lease->tickets.push_back(ticket);
        if (_InterlockedCompareExchange(&lease->state, CHECKING, WAITING) != WAITING) {
            lease->tickets.pop_back();
            return ERROR_BUSY;
        }
        DWORD status = ERROR_GEN_FAILURE;
        try {
            auto marker = ticket->marker;
            std::function<void()> task = [lease, marker] { (void)marker; on_check(lease); };
            status = sns_task_lifetime::schedule(ticket.get(), &task);
        } catch (...) {
            set_state(lease.get(), QUARANTINED);
            ticket->uncertain = true;
            return status;
        }
        if (status) set_state(lease.get(), QUARANTINED);
    }
    output.submitted_tasks = static_cast<uint32_t>(lease->tickets.size());
    output.retired_tasks = 0;
    for (const auto& ticket : lease->tickets) {
        const DWORD status = sns_task_lifetime::retire(ticket.get());
        if (status == ERROR_SUCCESS) ++output.retired_tasks;
        else if (status != ERROR_BUSY) set_state(lease.get(), QUARANTINED);
    }
    output.state = static_cast<uint32_t>(get_state(lease.get()));
    output.tls_start_bits = static_cast<uint32_t>(_InterlockedCompareExchange(&lease->tls_start_bits, 0, 0));
    output.tls_check_bits = static_cast<uint32_t>(_InterlockedCompareExchange(&lease->tls_check_bits, 0, 0));
    output.cleanup_complete = output.retired_tasks == output.submitted_tasks &&
        _InterlockedCompareExchange(&lease->running, 0, 0) == 0 &&
        _InterlockedCompareExchange(&lease->released, 0, 0) == 1 && output.state != QUARANTINED;
    output.reserved = 0;
    output.attempted = _InterlockedCompareExchange(&lease->attempted, 0, 0);
    output.native_known = _InterlockedCompareExchange(&lease->native_known, 0, 0);
    output.native_value = _InterlockedCompareExchange(&lease->native_value, 0, 0);
    output.was_liked = _InterlockedCompareExchange(&lease->was_liked, 0, 0);
    output.native_status = _InterlockedCompareExchange(&lease->native_status, 0, 0);
    output.exception = _InterlockedCompareExchange(&lease->exception, 0, 0);
    return write_poll(parameter, &output) ? ERROR_SUCCESS : ERROR_INVALID_PARAMETER;
}

static DWORD close_impl(void* parameter) {
    Admission admission;
    PollResult request{};
    if (!copy_poll(parameter, &request) || request.size != sizeof(PollResult) || !request.operation_id)
        return ERROR_INVALID_PARAMETER;
    auto lease = get_pending();
    if (!lease) return ERROR_SUCCESS;
    if (request.operation_id != lease->config.operation_id) return ERROR_INVALID_HANDLE;
    const State state = get_state(lease.get());
    if (state != FOUND && state != NOT_FOUND && state != FAILED && state != CANCELED &&
        state != COMPLETED && state != UNKNOWN_RESULT && state != ALREADY_LIKED) return ERROR_BUSY;
    if (_InterlockedCompareExchange(&lease->running, 0, 0)) return ERROR_BUSY;
    for (const auto& ticket : lease->tickets) {
        const DWORD status = sns_task_lifetime::retire(ticket.get());
        if (status != ERROR_SUCCESS) return status;
    }
    // Before dispatch, no native refs were acquired by the lease. Otherwise
    // cleanup must have completed inside its last Weixin callback.
    if (!lease->released && (lease->app.control || lease->kernel.control || lease->service.control ||
                            lease->future.control || lease->item.control)) return ERROR_BUSY;
    AcquireSRWLockExclusive(&pending_lock);
    pending.reset();
    ReleaseSRWLockExclusive(&pending_lock);
    lease.reset();
    return ERROR_SUCCESS;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxMomentsInteractionStart(void* parameter) {
    if (!WXBOT_ENABLE_SNS_INTERACTION) return ERROR_NOT_SUPPORTED;
    ExportLease exported_call;
    try { return start_impl(parameter); }
    catch (...) { return ERROR_GEN_FAILURE; }
}
extern "C" __declspec(dllexport) DWORD WINAPI WxMomentsInteractionPoll(void* parameter) {
    if (!WXBOT_ENABLE_SNS_INTERACTION) return ERROR_NOT_SUPPORTED;
    ExportLease exported_call;
    try { return poll_impl(parameter); }
    catch (...) { return ERROR_GEN_FAILURE; }
}
extern "C" __declspec(dllexport) DWORD WINAPI WxMomentsInteractionCancel(void* parameter) {
    if (!WXBOT_ENABLE_SNS_INTERACTION) return ERROR_NOT_SUPPORTED;
    ExportLease exported_call;
    Admission admission;
    PollResult request{};
    if (!copy_poll(parameter, &request) || request.size != sizeof(PollResult) || !request.operation_id)
        return ERROR_INVALID_PARAMETER;
    auto lease = get_pending();
    if (!lease) return ERROR_NOT_FOUND;
    if (request.operation_id != lease->config.operation_id) return ERROR_INVALID_HANDLE;
    _InterlockedExchange(&lease->cancel, 1);
    // Atomically compete with the final dispatch boundary. A committed call is
    // not cancelable; caller must poll and must not assume it was prevented.
    return _InterlockedCompareExchange(&lease->dispatch_gate, 2, 0) == 1 ? ERROR_BUSY : ERROR_SUCCESS;
}
extern "C" __declspec(dllexport) DWORD WINAPI WxMomentsInteractionClose(void* parameter) {
    if (!WXBOT_ENABLE_SNS_INTERACTION) return ERROR_NOT_SUPPORTED;
    ExportLease exported_call;
    try { return close_impl(parameter); } catch (...) { return ERROR_GEN_FAILURE; }
}
extern "C" __declspec(dllexport) DWORD WINAPI WxMomentsInteractionCapabilities(void*) {
    return WXBOT_ENABLE_SNS_INTERACTION ? 1u : 0u;
}
} // namespace wxbot_sns_interaction
