#pragma once
#include <cstddef>
#include <cstdint>
#include <cstring>

// Only the native revoke result gets new local identity. Original records and
// unrelated updates must retain their IDs and their full payload byte for byte.
static bool local_recall_matches_record(const unsigned char* record, size_t bytes,
                                       uintptr_t expected_type, uint64_t message_id) {
    if (!record || bytes < 0x150 || !message_id) return false;
    uintptr_t type = 0;
    uint64_t id = 0;
    std::memcpy(&type, record, sizeof(type));
    std::memcpy(&id, record + 0x148, sizeof(id));
    return type == expected_type && id == message_id;
}

static bool local_recall_detach_notice(unsigned char* record, size_t bytes,
                                      uintptr_t expected_type, uint64_t message_id) {
    if (!record || bytes < 0x150 ||
            !local_recall_matches_record(record, bytes, expected_type, message_id))
        return false;
    uint32_t kind = 0;
    std::memcpy(&kind, record + 0xc, sizeof(kind));
    if (kind != 10000) return false;
    // RawMessage's database row ID is uint32 at 0x144. The uint64 at
    // 0x150 belongs to the message's ordering/key; it must remain native.
    const uint32_t fresh_local_id = 0;
    const uint64_t fresh_server_id = 0;
    std::memcpy(record + 0x144, &fresh_local_id, sizeof(fresh_local_id));
    std::memcpy(record + 0x148, &fresh_server_id, sizeof(fresh_server_id));
    return true;
}

static bool local_recall_notice_written(const unsigned char* record, size_t bytes,
                                       uintptr_t expected_type, uint32_t original_local_id) {
    if (!record || bytes < 0x148) return false;
    uintptr_t type = 0;
    uint32_t kind = 0;
    uint32_t local_id = 0;
    std::memcpy(&type, record, sizeof(type));
    std::memcpy(&kind, record + 0xc, sizeof(kind));
    std::memcpy(&local_id, record + 0x144, sizeof(local_id));
    return type == expected_type && kind == 10000 && local_id &&
        local_id != original_local_id;
}
