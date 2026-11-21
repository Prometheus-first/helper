// Production ownership/cache code with synthetic native shared-control blocks.
// No WeChat process, network request, post content or real account is involved.
#include "../native/text_bridge.cpp"
#include <cassert>
#include <thread>

using Callback = void (__fastcall*)(void*);
struct Control {
    Callback* table;
    volatile LONG strong = 1, weak = 1;
    LONG destroyed = 0, deleted = 0;
};
static void __fastcall destroy(void* p) { ++static_cast<Control*>(p)->destroyed; }
static void __fastcall remove(void* p) { ++static_cast<Control*>(p)->deleted; }
static void __fastcall fail_destroy(void*) { RaiseException(0xe0421234, 0, 0, nullptr); }
static Callback table[]{destroy, remove}, failing_table[]{fail_destroy, remove};
static constexpr char account[128] = "wxid_offline_cache_owner";

static void test_bounded_owned_posts_survive_original_read_and_cache_replacement() {
    uint64_t app_value = 1, service_value = 2;
    Control app_control{table}, service_control{table};
    SharedMessage app{&app_value, &app_control}, service{&service_value, &service_control};
    std::array<uint64_t, 60> ids{};
    std::array<Control, 60> controls{};
    std::array<SharedMessage, 60> source{};
    for (size_t i = 0; i < source.size(); ++i) {
        ids[i] = 1000+i; controls[i].table = table;
        source[i] = {&ids[i], &controls[i]};
    }
    assert(moments_posts::publish(account, app, service, source.data(), source.size()) == 0);
    for (size_t i = 0; i < source.size(); ++i) assert(controls[i].strong == (i < 50 ? 2 : 1));
    auto in_comment = moments_posts::acquire(account, ids[0]);
    assert(in_comment && in_comment->count == 50);
    assert(!moments_posts::acquire("wxid_offline_different", ids[0]));
    assert(!moments_posts::acquire(account, ids[59]));
    // The successful read's complete vector and context can now be released.
    release_shared(app); release_shared(service);
    for (auto& pair : source) release_shared(pair);
    for (size_t i = 0; i < 50; ++i) assert(controls[i].strong == 1 && controls[i].destroyed == 0);
    uint64_t next_id = 2000;
    Control next_control{table};
    SharedMessage next_post{&next_id, &next_control};
    assert(moments_posts::publish(account, in_comment->app, in_comment->service, &next_post, 1) == 0);
    assert(!moments_posts::acquire(account, ids[0]));
    // A replacement/read/clear cannot destroy a post borrowed by an active comment.
    assert(moments_posts::clear() == 0);
    assert(controls[0].strong == 1 && in_comment->find(ids[0]));
    in_comment.reset();
    for (size_t i = 0; i < 60; ++i) assert(controls[i].destroyed == 1 && controls[i].deleted == 1);
    assert(app_control.destroyed == 1 && service_control.destroyed == 1);
    release_shared(next_post);
    assert(next_control.destroyed == 1 && next_control.deleted == 1);
    assert(live_tasks == 0); // Idle retained posts never block unrelated native sends.
}

static void test_expiry_and_failed_retain() {
    uint64_t id = 3000, context = 1;
    Control app_control{table}, service_control{table}, post_control{table};
    SharedMessage app{&context, &app_control}, service{&context, &service_control}, post{&id, &post_control};
    assert(moments_posts::publish(account, app, service, &post, 1) == 0);
    auto owned = moments_posts::acquire(account, id);
    owned->expires = 0;
    assert(!moments_posts::acquire(account, id));
    assert(post_control.strong == 2); // The active comment still owns this expired snapshot.
    owned.reset();
    assert(post_control.strong == 1);
    post_control.strong = 0;
    assert(moments_posts::publish(account, app, service, &post, 1) == ERROR_INVALID_DATA);
    assert(post_control.strong == 0); // A zero count must never be resurrected.
    assert(app_control.strong == 1 && service_control.strong == 1);
    release_shared(app); release_shared(service);
}

static void test_concurrent_expiry_and_acquisition() {
    uint64_t id = 4000, context = 1;
    Control app_control{table}, service_control{table}, post_control{table};
    SharedMessage app{&context, &app_control}, service{&context, &service_control}, post{&id, &post_control};
    std::thread reader([&] {
        for (int i = 0; i < 1000; ++i) {
            auto held = moments_posts::acquire(account, id);
            if (held) assert(held->find(id) && post_control.strong > 1);
        }
    });
    for (int i = 0; i < 100; ++i) {
        assert(moments_posts::publish(account, app, service, &post, 1) == 0);
        assert(moments_posts::clear() == 0);
    }
    reader.join();
    assert(moments_posts::clear() == 0);
    assert(app_control.strong == 1 && service_control.strong == 1 && post_control.strong == 1);
    release_shared(post); release_shared(service); release_shared(app);
}

static void test_uncertain_release_quarantines_without_a_second_decrement() {
    uint64_t id = 5000, context = 1;
    Control app_control{table}, service_control{table}, post_control{failing_table};
    SharedMessage app{&context, &app_control}, service{&context, &service_control}, post{&id, &post_control};
    assert(moments_posts::publish(account, app, service, &post, 1) == 0);
    release_shared(post); release_shared(service); release_shared(app);
    assert(moments_posts::clear() == ERROR_BUSY);
    assert(live_tasks == 1 && post_control.strong == 0);
    assert(app_control.strong == 1 && service_control.strong == 1);
    assert(moments_posts::clear() == ERROR_BUSY && post_control.strong == 0);
    assert(!moments_posts::acquire(account, id));
}

int main() {
    test_bounded_owned_posts_survive_original_read_and_cache_replacement();
    test_expiry_and_failed_retain();
    test_concurrent_expiry_and_acquisition();
    assert(moments_posts::queries == 0); // Ownership/lookup has no homepage-request path.
    test_uncertain_release_quarantines_without_a_second_decrement();
    std::puts("Moments post cache ownership: passed");
}
