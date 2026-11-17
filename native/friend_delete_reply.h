#pragma once

#include <cstddef>
#include <cstdint>
#include <string_view>

// The 4.2.0.12 oplog returns one code per command, in request order.
namespace friend_delete_reply {
constexpr int32_t retry_code = -0x1b1;

inline std::string_view read_string_field(std::string_view encoded) {
    if (encoded.size() < 2 || static_cast<unsigned char>(encoded[0]) != 0x0a)
        return {};
    size_t length = static_cast<unsigned char>(encoded[1]);
    size_t header = 2;
    if (length >= 0x80) {
        if (encoded.size() < 3 || encoded[2] != 1)
            return {};
        ++header;
    }
    if (!length || encoded.size() - header != length)
        return {};
    return encoded.substr(header);
}

inline bool matches_contact(std::string_view cmd_data, std::string_view target) {
    if (target.empty() || target.size() > 127)
        return false;
    // DelContact.UserName is an SKBuiltinString, which wraps the actual ID.
    return read_string_field(read_string_field(cmd_data)) == target;
}

inline bool terminal_code(int32_t transport_error, int32_t transport_detail,
                          int32_t batch_result, const int32_t* codes, size_t code_count,
                          size_t command_count, size_t matched_index, int32_t& code) {
    if (transport_error || transport_detail || batch_result || !codes ||
        code_count != command_count || matched_index >= code_count ||
        codes[matched_index] == retry_code)
        return false;
    code = codes[matched_index];
    return true;
}
}  // namespace friend_delete_reply
