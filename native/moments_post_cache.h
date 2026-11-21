#pragma once
#include <array>
#include <climits>

// Only references copied from a live, successful read lease may enter here.
// The cache owns native strong references; it never stores borrowed pointers.
namespace moments_posts {
constexpr size_t kLimit = 50;
constexpr ULONGLONG kLifetime = 300000;
static SRWLOCK lock = SRWLOCK_INIT;
static volatile LONG blocked = 0;
static volatile LONG64 queries = 0, hits = 0, misses = 0, comments = 0;

static void quarantine() {
    if (_InterlockedCompareExchange(&blocked, 1, 0) == 0) _InterlockedIncrement(&live_tasks);
}
static bool retain(SharedMessage* out, const SharedMessage& source) {
    __try {
        if (!source.object || !source.control) return false;
        auto count = reinterpret_cast<volatile LONG*>(static_cast<unsigned char*>(source.control) + 8);
        LONG old = _InterlockedCompareExchange(count, 0, 0);
        for (unsigned attempt = 0; attempt < 128 && old > 0 && old < LONG_MAX; ++attempt) {
            LONG seen = _InterlockedCompareExchange(count, old+1, old);
            if (seen == old) { *out = source; return true; }
            old = seen;
        }
    } __except (EXCEPTION_EXECUTE_HANDLER) { quarantine(); }
    return false;
}
struct Post { uint64_t id = 0; SharedMessage owner{}; };
struct Snapshot {
    char account[128]{};
    ULONGLONG expires = 0;
    SharedMessage app{}, service{};
    std::array<Post, kLimit> posts{};
    size_t count = 0;
    ~Snapshot() {
        TestResult cleanup{};
        // After an uncertain release, never attempt any further decrements.
        for (size_t i = 0; i < count && !blocked; ++i) {
            guarded_release_shared(&posts[i].owner, &cleanup);
            if (cleanup.exception) quarantine();
        }
        if (!blocked) guarded_release_shared(&service, &cleanup);
        if (cleanup.exception) quarantine();
        if (!blocked) guarded_release_shared(&app, &cleanup);
        if (cleanup.exception) quarantine();
        SecureZeroMemory(account, sizeof(account));
        SecureZeroMemory(posts.data(), sizeof(posts));
    }
    const SharedMessage* find(uint64_t id) const {
        for (size_t i = 0; i < count; ++i) if (posts[i].id == id) return &posts[i].owner;
        return nullptr;
    }
};
static std::shared_ptr<Snapshot> current;

// Swap/destroy outside the cache lock: native finalizers must not run under it.
static DWORD clear() {
    std::shared_ptr<Snapshot> old;
    AcquireSRWLockExclusive(&lock);
    old.swap(current);
    ReleaseSRWLockExclusive(&lock);
    old.reset();
    return blocked ? ERROR_BUSY : ERROR_SUCCESS;
}
static void expire() {
    std::shared_ptr<Snapshot> old;
    AcquireSRWLockExclusive(&lock);
    if (current && GetTickCount64() >= current->expires) old.swap(current);
    ReleaseSRWLockExclusive(&lock);
    old.reset();
}
static bool describe_post(const SharedMessage& pair, uint64_t* id) {
    __try {
        if (!pair.object || !pair.control) return false;
        *id = *static_cast<const uint64_t*>(pair.object);
        return *id != 0;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return false; }
}
static DWORD publish(const char* account, const SharedMessage& app, const SharedMessage& service,
                     const SharedMessage* begin, size_t count) {
    if (blocked) return ERROR_BUSY;
    try {
        auto next = std::make_shared<Snapshot>();
        std::memcpy(next->account, account, sizeof(next->account));
        if (!retain(&next->app, app) || !retain(&next->service, service)) return ERROR_INVALID_DATA;
        for (size_t i = 0; i < (std::min)(count, kLimit); ++i) {
            uint64_t id = 0;
            if (!describe_post(begin[i], &id)) return ERROR_INVALID_DATA;
            auto& post = next->posts[next->count];
            if (!retain(&post.owner, begin[i])) return ERROR_INVALID_DATA;
            post.id = id;
            ++next->count;
        }
        next->expires = GetTickCount64()+kLifetime;
        AcquireSRWLockExclusive(&lock);
        current.swap(next);
        ReleaseSRWLockExclusive(&lock);
        next.reset();
        return blocked ? ERROR_BUSY : ERROR_SUCCESS;
    } catch (...) { return ERROR_NOT_ENOUGH_MEMORY; }
}
static std::shared_ptr<Snapshot> acquire(const char* account, uint64_t id) {
    expire();
    std::shared_ptr<Snapshot> result;
    AcquireSRWLockShared(&lock);
    if (!blocked && current && !std::strcmp(current->account, account) && current->find(id)) result = current;
    ReleaseSRWLockShared(&lock);
    _InterlockedIncrement64(result ? &hits : &misses);
    return result;
}

// Diagnostics contain counts only, never post identifiers, accounts or pointers.
struct Stats { uint32_t size, cached_posts; uint64_t queries, hits, misses, comments; };
static_assert(sizeof(Stats) == 40);
static DWORD stats(void* parameter) {
    if (!parameter) return ERROR_INVALID_PARAMETER;
    __try {
        if (static_cast<Stats*>(parameter)->size != sizeof(Stats)) return ERROR_INVALID_PARAMETER;
    } __except (EXCEPTION_EXECUTE_HANDLER) { return ERROR_INVALID_PARAMETER; }
    expire();
    Stats result{};
    result.size = sizeof(result);
    AcquireSRWLockShared(&lock);
    if (current) result.cached_posts = static_cast<uint32_t>(current->count);
    ReleaseSRWLockShared(&lock);
    result.queries = _InterlockedCompareExchange64(&queries, 0, 0);
    result.hits = _InterlockedCompareExchange64(&hits, 0, 0);
    result.misses = _InterlockedCompareExchange64(&misses, 0, 0);
    result.comments = _InterlockedCompareExchange64(&comments, 0, 0);
    __try { *static_cast<Stats*>(parameter) = result; return ERROR_SUCCESS; }
    __except (EXCEPTION_EXECUTE_HANDLER) { return ERROR_INVALID_PARAMETER; }
}
}
extern "C" __declspec(dllexport) DWORD WINAPI WxMomentsPostCacheStats(void* parameter) {
    _InterlockedIncrement(&active_callbacks);
    const DWORD code = moments_posts::stats(parameter);
    _InterlockedDecrement(&active_callbacks);
    return code;
}
