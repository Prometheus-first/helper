"""微信 4.1.15.13 群缓存的只读完整快照；调用方须先核对核心 DLL 指纹。

不调用微信函数或触发网络同步。未缓存、成员读取中变化、账号不符时返回 None；
这与“群内无人”不同。可用于差分的快照必须含自己、无重复 ID，且成员索引数匹配。
"""
from __future__ import annotations

import struct

from .account import APP_GLOBAL_RVA, _pointer, read_current_account, read_current_profile, validate_account
from .contacts import (CACHE_PROVIDER_GETTER_RVA, CACHE_PROVIDER_VTABLE_RVA, read_contact_name)
from .group_management import validate_group_id

CHATROOM_CACHE_VTABLE_RVA = 0x8F9C228
MEMBER_DATA_VTABLE_RVA = 0x8FA3CB8
MEMBER_VTABLE_RVA = 0x8FA3C28
MAX_GROUP_MEMBERS = 2000  # 读取预算，不表示微信产品人数上限。


class _StableReader:
    def __init__(self, read_memory):
        self.read_memory = read_memory
        self.observed = {}
        self.total = 0

    def read(self, address, length):
        _pointer(address)
        if not 0 < length <= 65536 or address + length >= 0x800000000000:
            raise ValueError("群缓存读取边界无效")
        value = self.read_memory(address, length)
        if len(value) != length:
            raise ValueError("群缓存读取不完整")
        key = (address, length)
        if key in self.observed:
            if value != self.observed[key]:
                raise ValueError("群缓存正在变化")
        else:
            self.total += length
            if self.total > 4 * 1024 * 1024:
                raise ValueError("群缓存超出读取预算")
            self.observed[key] = value
        return value

    def ptr(self, address):
        return struct.unpack("<Q", self.read(address, 8))[0]

    def string(self, address, limit, *, encoding="utf-8", empty=False):
        header = self.read(address, 32)
        length, capacity = struct.unpack_from("<QQ", header, 16)
        if (length > limit or (not empty and not length) or capacity < length or
                not (capacity == 15 or 16 <= capacity <= 1048576)):
            raise ValueError("群缓存字符串布局无效")
        if not length:
            return ""
        buffer = address if capacity == 15 else _pointer(struct.unpack_from("<Q", header)[0])
        return self.read(buffer, length).decode(encoding)

    def verify(self):
        # 再读所有参与结果的结构和正文；成员数量相同但 ID 换人也必须拒绝。
        for (address, length), previous in reversed(tuple(self.observed.items())):
            if self.read_memory(address, length) != previous:
                raise ValueError("群缓存快照不稳定")


def _hash(value):
    result = 0xCBF29CE484222325
    for byte in value.encode("ascii"):
        result = ((result ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return result


def _find_room(reader, cache, room):
    # ChatroomCache +0x38 为 LRU：list + 其 unordered_map 索引。
    # 6e2ff0 -> 62cd40 使用索引，再从 LRU 节点 +0x30 取 shared_ptr。
    layout = reader.read(cache + 0x38, 0x50)
    head, count = struct.unpack_from("<QQ", layout)
    sentinel, index_count, buckets, end, capacity, mask, bucket_count = struct.unpack_from("<QQQQQQQ", layout, 24)
    _pointer(head)
    _pointer(sentinel)
    _pointer(buckets)
    if (not 1 <= count <= 10000 or count != index_count or
            not 1 <= mask <= 65535 or (mask + 1) & mask or bucket_count != mask + 1 or
            end - buckets != bucket_count * 16 or capacity < end):
        return None
    first, node = struct.unpack("<QQ", reader.read(buckets + (_hash(room) & mask) * 16, 16))
    visited = set()
    for _ in range(min(count, 256)):
        if node == sentinel or node in visited:
            return None
        visited.add(node)
        if reader.string(node + 0x10, 127, encoding="ascii") == room:
            entry = _pointer(reader.ptr(node + 0x30))
            if reader.string(entry + 0x10, 127, encoding="ascii") != room:
                return None
            following, previous = struct.unpack("<QQ", reader.read(entry, 16))
            if reader.ptr(previous) != entry or reader.ptr(following + 8) != entry:
                return None
            return _pointer(reader.ptr(entry + 0x30))
        if node == first:
            return None
        node = reader.ptr(node + 8)
    return None


def _group_name(reader, address):
    # 名字不可用不代表成员不存在。只有 ID 与完整容器参与成员差分。
    try:
        name = reader.string(address, 256, empty=True).strip()
        return name if len(name) <= 128 and not any(ord(ch) < 32 or ord(ch) == 127 for ch in name) else ""
    except (OSError, ValueError, struct.error):
        return ""


def read_group_members(read_memory, core_base, room, *, expected_account):
    """返回完整成员 tuple[dict]，未知或不稳定返回 None。

    每项含 wxid、nickname、group_nickname、wechat_name。nickname 优先实际群昵称，
    再用微信名；两者都不可读时为空。名字从不跨群推断，也不读取好友备注。
    一份完整缓存仍可能滞后于服务器，调用方应连续确认后再产生退出事件。
    """
    validate_group_id(room)
    validate_account(expected_account)
    account = read_current_account(read_memory, core_base)
    if account != expected_account:
        return None
    reader = _StableReader(read_memory)
    try:
        app = _pointer(reader.ptr(core_base + APP_GLOBAL_RVA))
        context = _pointer(reader.ptr(app + 0x68))
        table = _pointer(reader.ptr(context))
        if reader.ptr(table + 0x38) != core_base + CACHE_PROVIDER_GETTER_RVA:
            return None
        if reader.read(core_base + CACHE_PROVIDER_GETTER_RVA + 28, 8) != bytes.fromhex("48 83 b9 60 04 00 00 00"):
            return None
        provider = _pointer(reader.ptr(context + 0x460))
        if reader.ptr(provider) != core_base + CACHE_PROVIDER_VTABLE_RVA:
            return None
        head, size = struct.unpack("<QQ", reader.read(provider + 0x90, 16))
        if not 1 <= size <= 512:
            return None
        node, cache, visited = reader.ptr(head), None, set()
        for _ in range(size):
            if node == head or node in visited:
                break
            visited.add(node)
            value = reader.ptr(node + 0x30)
            if value and reader.ptr(value) == core_base + CHATROOM_CACHE_VTABLE_RVA:
                if reader.string(node + 0x10, 64, encoding="ascii") != "ChatroomCache":
                    return None
                cache = value
                break
            node = reader.ptr(node)
        if cache is None:
            return None
        record = _find_room(reader, cache, room)
        if record is None or reader.string(record + 8, 127, encoding="ascii") != room:
            return None
        data = reader.read(record + 0x60, 0x20)
        # RepeatedPtrField 的 capacity 是 int32；其后 4 字节为 padding，
        # 实机里可含未初始化高位，不能按 uint64 当成容量。
        vtable, array, count, allocated, capacity = struct.unpack_from("<QQiii", data)
        if (vtable != core_base + MEMBER_DATA_VTABLE_RVA or
                not 1 <= count <= MAX_GROUP_MEMBERS or not count <= allocated <= capacity <= MAX_GROUP_MEMBERS):
            return None
        # 1730f10 拷贝整个 protobuf，随后为全部成员重建 +0xd0 索引。
        # 两边数量不一致表示正在重建或读取不完整，不能发退群事件。
        index = reader.read(record + 0xD0, 0x38)
        if struct.unpack_from("<Q", index, 16)[0] != count:
            return None
        pointers = struct.unpack("<" + "Q" * count, reader.read(_pointer(array), count * 8))
        if len(set(pointers)) != count:
            return None
        result, seen = [], set()
        for pointer in pointers:
            member = reader.read(_pointer(pointer), 0x20)
            member_vtable, wxid_address, nickname_address = struct.unpack_from("<QQQ", member)
            if member_vtable != core_base + MEMBER_VTABLE_RVA:
                return None
            wxid = validate_account(reader.string(_pointer(wxid_address), 127, encoding="ascii"))
            if wxid in seen or wxid in (".", ".."):
                return None
            seen.add(wxid)
            nickname = _group_name(reader, nickname_address) if nickname_address else ""
            result.append({"wxid": wxid, "group_nickname": nickname})
        if account not in seen:
            return None
        reader.verify()
        # 联系人名字是可选补充；缺少某个微信名不丢弃完整的成员 ID 集合。
        for row in result:
            name = read_contact_name(read_memory, core_base, row["wxid"], expected_account=account)
            if not name and row["wxid"] == account:
                profile = read_current_profile(read_memory, core_base)
                name = profile["name"] if profile and profile["wxid"] == account else None
            row["wechat_name"] = name or ""
            row["nickname"] = row["group_nickname"] or name or ""
        reader.verify()
        if read_current_account(read_memory, core_base) != account:
            return None
        return tuple(sorted(result, key=lambda row: row["wxid"]))
    except (OSError, ValueError, struct.error):
        return None
