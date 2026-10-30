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

    enqueue_received(sender, receiver, "original", nullptr, 1, nullptr, 0, 18446744073709551600ULL);
    std::vector<unsigned char> v4(267552 + 128, 0xa5);
    assert(WxPollMediaMessageV4(v4.data()) == 0);
    for (size_t i = 267552; i < v4.size(); ++i) assert(v4[i] == 0xa5);
    enqueue_received(sender, receiver, "original", nullptr, 1, nullptr, 0, 18446744073709551600ULL);
    assert(WxPollMediaMessageV5(current.get()) == 0);
    assert(current->size == 267560 && current->native_message_id == 18446744073709551600ULL);

    std::string notice = "<sysmsg type=\"revokemsg\"><revokemsg><session>123@chatroom</session>"
                         "<newmsgid>1234567890123456789</newmsgid></revokemsg></sysmsg>";
    SmallString system{};
    const char* notice_bytes = notice.c_str();
    std::memcpy(system.data, &notice_bytes, sizeof(notice_bytes));
    system.length = system.capacity = notice.size();
    copy_recall_source(&system);
    assert(WxPollMediaMessageV5(current.get()) == 0 && current->present);
    assert(current->type == 10002 && current->reserved == 2);
    assert(std::string(current->image_xml) == notice);
    system.length = system.capacity = 16384;
    copy_recall_source(&system);
    assert(WxPollMediaMessageV5(current.get()) == 0 && !current->present);

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

    // Feed a complete synthetic AddMsg batch through the actual text hook copy.
    // Long text uses V7's payload field while V6 keeps its prior behavior.
    {
        unsigned char text_raw[0x78]{};
        const char* room = "123456789@chatroom";
        SmallString from = voice_borrow(room, std::strlen(room));
        SmallString to = voice_borrow(receiver, std::strlen(receiver));
        Wrapper from_wrapper{nullptr, &from}, to_wrapper{nullptr, &to}, body_wrapper{};
        auto from_ptr = &from_wrapper, to_ptr = &to_wrapper, body_ptr = &body_wrapper;
        std::memcpy(text_raw + 8, &from_ptr, sizeof(from_ptr));
        std::memcpy(text_raw + 0x18, &to_ptr, sizeof(to_ptr));
        std::memcpy(text_raw + 0x20, &body_ptr, sizeof(body_ptr));
        *reinterpret_cast<uint32_t*>(text_raw + 0x14) = 1;
        *reinterpret_cast<uint64_t*>(text_raw + 0x50) = 123456789012345ULL;
        const uintptr_t batch[] = {reinterpret_cast<uintptr_t>(text_raw),
                                   reinterpret_cast<uintptr_t>(text_raw + sizeof(text_raw))};
        for (size_t length : {size_t(1023), size_t(1024), size_t(4000), size_t(262143)}) {
            std::string text = "wxid_fixture_sender:\n" + std::string(length - 21, 'a');
            assert(text.size() == length);
            SmallString content = voice_borrow(text.c_str(), text.size());
            body_wrapper.string = &content;
            sample_all_text(batch, sizeof(text_raw));
            assert(WxPollMediaMessageV7(current.get()) == 0 && current->present);
            assert(current->native_message_id == 123456789012345ULL);
            assert(std::strcmp(current->sender, room) == 0 && std::strcmp(current->receiver, receiver) == 0);
            if (length < 1024) {
                assert(current->reserved == 0 && std::string(current->text) == text);
            } else {
                assert((current->reserved & RECEIVE_LONG_TEXT) && std::string(current->image_xml) == text);
                assert(!current->text[0]);
                sample_all_text(batch, sizeof(text_raw));
                assert(WxPollMediaMessageV6(current.get()) == 0 && !current->present);
            }
        }
        std::string oversized(262144, 'x');
        SmallString too_large = voice_borrow(oversized.c_str(), oversized.size());
        body_wrapper.string = &too_large;
        sample_all_text(batch, sizeof(text_raw));
        assert(WxPollMediaMessageV7(current.get()) == 0 && current->present);
        assert(current->reserved & RECEIVE_TEXT_UNAVAILABLE);
        std::string nul_text(1024, 'x'); nul_text[500] = '\0';
        SmallString with_nul = voice_borrow(nul_text.c_str(), nul_text.size());
        body_wrapper.string = &with_nul;
        sample_all_text(batch, sizeof(text_raw));
        assert(WxPollMediaMessageV7(current.get()) == 0 && (current->reserved & RECEIVE_TEXT_UNAVAILABLE));
    }

    // V6 carries binary voice after the XML's NUL; legacy V5 writes its exact
    // original boundary. This is a structural fixture, not playable audio.
    std::string voice("\x02#!SILK_V3", 10);
    voice.append("\x03\0a\0b", 5);
    std::string voice_xml("<msg><voicemsg voicelength=\"20\" length=\"15\" endflag=\"1\"/></msg>");
    SmallString voice_value = voice_borrow(voice.c_str(), voice.size());
    SmallString xml_value = voice_borrow(voice_xml.c_str(), voice_xml.size());
    struct BinaryWrapper { void* reserved; SmallString* value; uint32_t length, padding; } binary{};
    binary.value = &voice_value;
    binary.length = static_cast<uint32_t>(voice.size());
    wrapper.string = &xml_value;
    auto binary_pointer = &binary;
    std::memcpy(raw + 0x30, &binary_pointer, sizeof(binary_pointer));
    _InterlockedExchange(&core_profile_id, 2);
    copy_voice_message(raw, sender, receiver, "", 123456789);
    assert(WxPollMediaMessageV6(current.get()) == 0);
    assert(current->size == 267568 && current->native_message_id == 123456789 && current->type == 34);
    assert(current->voice_offset == voice_xml.size() + 1 && current->voice_size == voice.size());
    assert(std::strcmp(current->image_xml, voice_xml.c_str()) == 0);
    assert(std::memcmp(current->image_xml + current->voice_offset, voice.data(), voice.size()) == 0);
    voice_value.data[12] = 'z';
    assert(current->image_xml[current->voice_offset + 12] == 'a');
    voice_value = voice_borrow(voice.c_str(), voice.size());
    copy_voice_message(raw, sender, receiver, "", 7);
    std::vector<unsigned char> v5(267560 + 128, 0xa5);
    assert(WxPollMediaMessageV5(v5.data()) == 0);
    assert(*reinterpret_cast<uint32_t*>(v5.data()) == 267560);
    for (size_t i = 267560; i < v5.size(); ++i) assert(v5[i] == 0xa5);
    for (size_t i = 0; i < voice.size(); ++i)
        assert(v5[offsetof(ReceivedMessage, image_xml) + voice_xml.size() + 1 + i] == 0);
    binary.length += 1;
    copy_voice_message(raw, sender, receiver, "", 8);
    assert(WxPollMediaMessageV6(current.get()) == 0 && !current->voice_size);
    binary.length -= 1;
    _InterlockedExchange(&core_profile_id, 1);
    copy_voice_message(raw, sender, receiver, "", 9);
    assert(WxPollMediaMessageV6(current.get()) == 0 && !current->voice_size);
    _InterlockedExchange(&core_profile_id, 0);

    for (int i = 0; i < 65; ++i) enqueue_received(sender, receiver, "fixture");
    assert(WxPollMediaMessageV4(current.get()) == 0 && current->dropped == 1);
    for (int i = 0; i < 64; ++i) WxPollMediaMessageV4(current.get());
    assert(current->present == 0);
    std::puts("Native queue: V3/V4/V5 boundaries, V6 voice binary ownership, profile and length guards passed.");
    return 0;
}
