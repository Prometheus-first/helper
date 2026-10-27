// Private, version-locked scheduler ownership. No hooks or background thread.
#pragma once
#include <vector>

namespace sns_task_lifetime {
constexpr uintptr_t kControlTable = 0x914e2e8;
constexpr uintptr_t kTaskTable = 0x914e378;
struct Marker {};
struct Ticket {
    SharedMessage scheduler{}, task{};
    unsigned char* weak = nullptr;
    std::shared_ptr<Marker> marker = std::make_shared<Marker>();
    bool entered = false, returned = false, settled = false, uncertain = false;
    bool retired = false;
};

static bool retain_task_weak(Ticket* ticket) {
    __try {
        auto ctl = static_cast<unsigned char*>(ticket->task.control);
        if (!ctl || ticket->task.object != ctl + 0x10 || ticket->weak ||
            *reinterpret_cast<uintptr_t*>(ctl) != core_sns_address(core_base, kControlTable) ||
            *reinterpret_cast<uintptr_t*>(ctl + 0x10) != core_sns_address(core_base, kTaskTable) ||
            _InterlockedCompareExchange(reinterpret_cast<volatile LONG*>(ctl + 8), 0, 0) <= 0)
            return false;
        auto weak = reinterpret_cast<volatile LONG*>(ctl + 12);
        LONG n = _InterlockedCompareExchange(weak, 0, 0);
        while (n > 0 && n < LONG_MAX) {
            LONG old = _InterlockedCompareExchange(weak, n + 1, n);
            if (old == n) { ticket->weak = ctl; return true; }
            n = old;
        }
        return false;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}

static DWORD schedule(Ticket* ticket, std::function<void()>* fn) {
    static_assert(sizeof(std::function<void()>) == 64, "std::function ABI");
    struct Location { const char* function; const char* file; uint32_t line, pad; void* caller; };
    Location loc{"sns.inspect", "moments_task_lifetime.h", 1, 0, nullptr};
    TestResult error{};
    DWORD status = ERROR_NOT_READY;
    __try {
        using App = void* (__fastcall*)();
        using Scheduler = SharedMessage* (__fastcall*)(void*, SharedMessage*);
        using Schedule = SharedMessage* (__fastcall*)(void*, SharedMessage*, Location*, std::function<void()>*, uint32_t);
        void* app = reinterpret_cast<App>(core_address(core_base, 0x45d10))();
        if (app) {
            reinterpret_cast<Scheduler>(core_address(core_base, 0x47310))(app, &ticket->scheduler);
            if (ticket->scheduler.object && ticket->scheduler.control) {
                ticket->entered = true;
                reinterpret_cast<Schedule>(core_address(core_base, 0x2fb010))(
                    ticket->scheduler.object, &ticket->task, &loc, fn, 1);
                ticket->returned = true;
                if (retain_task_weak(ticket)) status = ERROR_SUCCESS;
                else { ticket->uncertain = true; return ERROR_INVALID_DATA; }
            }
        }
        guarded_release_shared(&ticket->task, &error);
        if (!error.exception) guarded_release_shared(&ticket->scheduler, &error);
        if (error.exception) { ticket->uncertain = true; return ERROR_GEN_FAILURE; }
        ticket->settled = true;
        return status;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        ticket->uncertain = true;
        return ERROR_GEN_FAILURE;
    }
}

// Called only under the exported-operation admission lock, after submission's
// local std::function has been destroyed. Our weak keeps the control allocated.
static DWORD retire(Ticket* ticket) {
    if (ticket->uncertain || !ticket->settled) return ERROR_INVALID_DATA;
    if (ticket->retired) return ERROR_SUCCESS;
    if (ticket->marker.use_count() != 1) return ERROR_BUSY;
    if (!ticket->entered) { ticket->retired = true; return ERROR_SUCCESS; }
    __try {
        auto ctl = ticket->weak;
        if (!ctl || *reinterpret_cast<uintptr_t*>(ctl) != core_sns_address(core_base, kControlTable))
            return ERROR_INVALID_DATA;
        LONG strong = _InterlockedCompareExchange(reinterpret_cast<volatile LONG*>(ctl + 8), 0, 0);
        LONG weak = _InterlockedCompareExchange(reinterpret_cast<volatile LONG*>(ctl + 12), 0, 0);
        if (strong < 0 || weak < 1) return ERROR_INVALID_DATA;
        if (strong || weak != 1) return ERROR_BUSY;
        // The object's destructor has returned and its implicit weak is gone.
        // There is no other weak owner that could be copying a reference now.
        if (_InterlockedCompareExchange(reinterpret_cast<volatile LONG*>(ctl + 12), 0, 1) != 1)
            return ERROR_BUSY;
        ticket->weak = nullptr; // release at most once, including an SEH
        using Delete = void (__fastcall*)(void*);
        auto table = *reinterpret_cast<Delete**>(ctl);
        table[1](ctl);
        ticket->retired = true;
        return ERROR_SUCCESS;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        ticket->uncertain = true;
        return ERROR_GEN_FAILURE;
    }
}
} // namespace sns_task_lifetime
