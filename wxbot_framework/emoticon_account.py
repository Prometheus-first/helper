"""已核对微信版本当前账号的表情缓存密钥快照，仅在内存中使用。"""
from __future__ import annotations

import struct

from .account import APP_GLOBAL_RVA, _pointer, read_current_account, validate_account
from .native_profiles import select_memory_profile

# 旧版本二进制中的固定混淆掩码（保留兼容）；不是任何用户的密钥。
_CACHE_MASK = bytes.fromhex("c9629058e9481a7d100824e418f6f17b2ceb0ce2d7e96e9dc00d3e8249539b96")


def read_emoticon_cache_key(read_memory, core_base, *, expected_account):
    """在已验证 DLL 指纹的通道中读取当前账号缓存 key，不记录或返回其他资料。

    调用方必须在每次导出文件之前继续核对登录会话；快照不赋予持续访问权。
    """
    validate_account(expected_account)

    def read(address, length):
        value = read_memory(_pointer(address), length)
        if len(value) != length:
            raise ValueError("缓存字段读取不完整")
        return value

    def ptr(address):
        return struct.unpack("<Q", read(address, 8))[0]

    try:
        if read_current_account(read_memory, core_base) != expected_account:
            raise ValueError("账号不匹配")
        profile = select_memory_profile(read_memory, core_base)
        if profile is None:
            raise ValueError("版本布局未确认")
        app = _pointer(ptr(core_base + profile.rva(APP_GLOBAL_RVA)))
        context = _pointer(ptr(app + 0x68))
        address = context + 0x2D8
        header = read(address, 32)
        length, capacity = struct.unpack_from("<QQ", header, 16)
        if (not 1 <= length <= 4096 or capacity < length or
                not (capacity == 15 or 16 <= capacity <= 1024 * 1024)):
            raise ValueError("缓存字段布局不匹配")
        buffer = address if capacity == 15 else _pointer(struct.unpack_from("<Q", header)[0])
        body = read(buffer, length)
        if (read(address, 32) != header or read(buffer, length) != body or
                ptr(core_base + profile.rva(APP_GLOBAL_RVA)) != app or ptr(app + 0x68) != context or
                read_current_account(read_memory, core_base) != expected_account):
            raise ValueError("缓存快照失效")
        return bytes(value ^ profile.emoticon_cache_mask[index & 31]
                     for index, value in enumerate(body[:16])).ljust(16, b"\0")
    except (OSError, ValueError, struct.error):
        raise RuntimeError("当前账号的表情缓存密钥暂不可用或登录会话已变化") from None
