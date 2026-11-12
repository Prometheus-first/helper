#include "../native/local_recall_policy.h"
#include <array>
#include <cassert>
#include <cstdio>

int main() {
    constexpr uintptr_t type = 0x12345678;
    constexpr uint64_t id = 0x123456789abcdef0;
    constexpr uint32_t local_id = 0x11223344;
    constexpr uint64_t order_key = 0xaabbccdd99887766;
    std::array<unsigned char, 0x2e8> original{};
    original.fill(0x5a);
    std::memcpy(original.data(), &type, sizeof(type));
    std::memcpy(original.data() + 0x148, &id, sizeof(id));
    std::memcpy(original.data() + 0x144, &local_id, sizeof(local_id));
    std::memcpy(original.data() + 0x150, &order_key, sizeof(order_key));
    uint32_t kind = 10000;
    std::memcpy(original.data() + 0xc, &kind, sizeof(kind));

    auto notice = original;
    assert(local_recall_detach_notice(notice.data(), notice.size(), type, id));
    auto expected = original;
    std::memset(expected.data() + 0x144, 0, 12);
    assert(notice == expected); // Sender, timestamp, body and metadata remain native.
    assert(local_recall_matches_record(original.data(), original.size(), type, id));
    assert(!local_recall_matches_record(original.data(), 0x148, type, id));
    assert(!local_recall_matches_record(original.data(), original.size(), type, id + 1));

    for (int scenario = 0; scenario != 5; ++scenario) {
        auto invalid = original;
        size_t size = invalid.size();
        uint64_t expected_id = id;
        if (scenario == 0) expected_id = 0;
        if (scenario == 1) size = 0x14f;
        if (scenario == 2) invalid[0] ^= 1;
        if (scenario == 3) expected_id += 1;
        if (scenario == 4) {
            kind = 1;
            std::memcpy(invalid.data() + 0xc, &kind, sizeof(kind));
        }
        auto before = invalid;
        assert(!local_recall_detach_notice(invalid.data(), size, type, expected_id));
        assert(invalid == before);
    }
    assert(!local_recall_detach_notice(nullptr, original.size(), type, id));
    // Native save wrappers return a plain RawMessage, with no presence byte.
    assert(!local_recall_notice_written(original.data(), original.size(), type, local_id));
    const uint32_t new_local_id = local_id + 1;
    std::memcpy(notice.data() + 0x144, &new_local_id, sizeof(new_local_id));
    assert(local_recall_notice_written(notice.data(), notice.size(), type, local_id));
    assert(local_recall_notice_written(notice.data(), 0x148, type, local_id));
    assert(!local_recall_notice_written(notice.data(), 0x147, type, local_id));
    assert(!local_recall_notice_written(nullptr, notice.size(), type, local_id));
    assert(!local_recall_notice_written(notice.data(), notice.size(), type ^ 1, local_id));
    const uint32_t zero_id = 0;
    std::memcpy(notice.data() + 0x144, &zero_id, sizeof(zero_id));
    assert(!local_recall_notice_written(notice.data(), notice.size(), type, local_id));
    std::puts("Local recall notice policy: passed");
}
