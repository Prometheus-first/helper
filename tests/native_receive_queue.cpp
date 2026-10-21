// Offline native queue checks. Run in this test process; never opens WeChat.
// Build from native/.build with the same x64 MSVC and minhook.lib as the bridge:
// cl /std:c++17 /EHsc /MD /O2 /utf-8 ../../tests/native_receive_queue.cpp /link bcrypt.lib minhook.lib
#include "../native/text_bridge.cpp"
#include <cassert>
#include <vector>
#include <string>
#include <cstdlib>

// A failed offline check must report to the runner, never open a Windows dialog.
#undef assert
#define assert(expression) do { if (!(expression)) { \
    std::fprintf(stderr, "Check failed at line %d: %s\n", __LINE__, #expression); \
    std::exit(1); } } while (0)

int main() {
    const char* sender = "wxid_fixture_sender";
    const char* receiver = "wxid_fixture_receiver";
    std::string large(24000, 'x');
    enqueue_received(sender, receiver, "", nullptr, 49, large.c_str());
    std::vector<unsigned char> legacy(21792 + 128, 0xa5);
    assert(WxPollMediaMessageV3(legacy.data()) == 0);
    assert(*reinterpret_cast<uint32_t*>(legacy.data()) == 21792);
    assert((*reinterpret_cast<uint32_t*>(legacy.data() + 20) & 1) == 1);
    for (size_t i = 5408; i < legacy.size(); ++i)
        assert(legacy[i] == (i < 21792 ? 0 : 0xa5));

    enqueue_received(sender, receiver, "", nullptr, 49, large.c_str());
    auto current = std::make_unique<ReceivedMessage>();
    assert(WxPollMediaMessageV4(current.get()) == 0);
    assert(current->size == 267552 && current->present == 1 && current->type == 49);
    assert(std::string(current->image_xml) == large);
    assert(WxPollMediaMessageV4(current.get()) == 0 && current->present == 0);

    // Exercise the actual copy helper at and above the V4 byte boundary.
    unsigned char raw[0x78]{};
    struct Wrapper { void* reserved; SmallString* string; } wrapper{};
    SmallString value{};
    std::string bounded(262143, 'a');
    const char* bytes = bounded.c_str();
    std::memcpy(value.data, &bytes, sizeof(bytes));
    value.length = value.capacity = bounded.size();
    wrapper.string = &value;
    const auto wrapper_pointer = &wrapper;
    std::memcpy(raw + 0x20, &wrapper_pointer, sizeof(wrapper_pointer));
    copy_app_message(raw, sender, receiver, "");
    assert(WxPollMediaMessageV4(current.get()) == 0);
    assert(current->present && current->reserved == 0);
    assert(std::strlen(current->image_xml) == bounded.size());
    value.length = value.capacity = 262144;
    copy_app_message(raw, sender, receiver, "");
    assert(WxPollMediaMessageV4(current.get()) == 0);
    assert(current->present && (current->reserved & 1) && !current->image_xml[0]);

    for (int i = 0; i < 65; ++i) enqueue_received(sender, receiver, "fixture");
    assert(WxPollMediaMessageV4(current.get()) == 0 && current->dropped == 1);
    for (int i = 0; i < 64; ++i) WxPollMediaMessageV4(current.get());
    assert(current->present == 0);
    std::puts("Native receive queue: V3 guard, V4 long payload, limit and bounded eviction passed.");
    return 0;
}
