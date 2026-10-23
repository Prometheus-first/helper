"""已核对微信版本联系人缓存的只读名字快照；调用方必须先核对核心 DLL 指纹。"""
from __future__ import annotations

import struct

from .account import APP_GLOBAL_RVA, _pointer, read_current_account, validate_account
from .native_profiles import select_memory_profile

CACHE_PROVIDER_GETTER_RVA = 0x36C7B0
CACHE_PROVIDER_VTABLE_RVA = 0x8F9BF98
CONTACT_CACHE_VTABLE_RVA = 0x8FA2538


def read_contact_name(read_memory, core_base, wxid, *, expected_account=None):
    """按实际成员 ID 读取微信名。缺失、切换、重建缓存时返回 None，不猜名字。

    仅遍历已核对的缓存对象，不调用微信函数、不扫描整个进程、不读取群昵称或备注。
    """
    validate_account(wxid)
    account = read_current_account(read_memory, core_base)
    if not account or (expected_account is not None and account != expected_account):
        return None
    try:
        profile = select_memory_profile(read_memory, core_base)
        if profile is None:
            return None
        rva = profile.rva
        def read(address, length):
            value = read_memory(_pointer(address), length)
            if len(value) != length:
                raise ValueError("联系人名字读取不完整")
            return value

        def ptr(address):
            return struct.unpack("<Q", read(address, 8))[0]

        def string(address, limit=256):
            header = read(address, 32)
            length, capacity = struct.unpack_from("<QQ", header, 16)
            if not 1 <= length <= limit or capacity < length or not (capacity == 15 or 16 <= capacity <= 1048576):
                raise ValueError("联系人字符串布局不匹配")
            buffer = address if capacity == 15 else _pointer(struct.unpack_from("<Q", header)[0])
            body = read(buffer, length)
            if read(address, 32) != header or read(buffer, length) != body:
                raise ValueError("联系人字符串发生变化")
            return body.decode("utf-8")

        app = ptr(core_base + rva(APP_GLOBAL_RVA))
        context = ptr(app + 0x68)
        table = ptr(context)
        if ptr(table + 0x38) != core_base + rva(CACHE_PROVIDER_GETTER_RVA):
            return None
        if read(core_base + rva(CACHE_PROVIDER_GETTER_RVA) + 28, 8) != bytes.fromhex("48 83 b9 60 04 00 00 00"):
            return None
        provider = _pointer(ptr(context + 0x460))
        if ptr(provider) != core_base + rva(CACHE_PROVIDER_VTABLE_RVA):
            return None
        registry = read(provider + 0x90, 16)
        head, size = struct.unpack("<QQ", registry)
        if not 1 <= size <= 512:
            return None
        node = ptr(head)
        visited, cache = set(), None
        for _ in range(size):
            if node == head or node in visited:
                break
            visited.add(node)
            value = ptr(node + 0x30)
            if value and ptr(value) == core_base + rva(CONTACT_CACHE_VTABLE_RVA):
                cache = value
                break
            node = ptr(node)
        if not cache or read(provider + 0x90, 16) != registry:
            return None

        # MSVC unordered_map：哨兵、元素数、桶首尾和桶掩码都要在读取前后保持一致。
        layout = read(cache + 0xC8, 0x38)
        sentinel, count = struct.unpack_from("<QQ", layout, 8)
        buckets = struct.unpack_from("<Q", layout, 24)[0]
        mask = struct.unpack_from("<Q", layout, 48)[0]
        if not 1 <= count <= 100000 or not 1 <= mask <= 65535 or (mask + 1) & mask:
            return None
        hash_value = 0xCBF29CE484222325
        for byte in wxid.encode("ascii"):
            hash_value = ((hash_value ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
        bucket_address = buckets + (hash_value & mask) * 16
        bucket = read(bucket_address, 16)
        first, node = struct.unpack("<QQ", bucket)
        visited = set()
        for _ in range(min(count, 256)):
            if node == sentinel or node in visited:
                return None
            visited.add(node)
            if string(node + 0x10, 127) == wxid:
                contact = _pointer(ptr(node + 0x30))
                if string(contact + 8, 127) != wxid:
                    return None
                name = string(contact + 0xD8)
                if not name.strip() or len(name) > 64 or any(ord(ch) < 32 for ch in name):
                    return None
                if (ptr(node + 0x30) != contact or string(node + 0x10, 127) != wxid or
                        read(cache + 0xC8, 0x38) != layout or read(bucket_address, 16) != bucket or
                        ptr(context + 0x460) != provider or read_current_account(read_memory, core_base) != account):
                    return None
                return name.strip()
            if node == first:
                return None
            node = ptr(node + 8)
        return None
    except (OSError, ValueError, struct.error):
        return None
