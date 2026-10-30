"""已核对微信版本联系人缓存的只读快照；调用方必须先核对核心 DLL 指纹。"""
from __future__ import annotations

import struct
import json

from .account import APP_GLOBAL_RVA, _pointer, read_current_account, validate_account
from .native_profiles import select_memory_profile

CACHE_PROVIDER_GETTER_RVA = 0x36C7B0
CACHE_PROVIDER_VTABLE_RVA = 0x8F9BF98
CONTACT_CACHE_VTABLE_RVA = 0x8FA2538

MAX_DIRECTORY_CONTACTS = 10000
MAX_DIRECTORY_BYTES = 3 * 1024 * 1024
LABEL_CACHE_VTABLE_RVA = 0x901F638  # 4.1.15.56: 726EB0 constructs this account component.
CONTACT_EXT_VTABLE_RVA = 0x91798C8  # 4.1.15.56: micromsg.ContactExtData, size 0x130.


class ContactDirectoryError(RuntimeError):
    """联系人读取限制或尚未适配的字段布局；错误不包含个人资料。"""


class _DirectoryReader:
    """有界、双读的快照；读期间任一字段变化就放弃整份结果。"""
    def __init__(self, read_memory):
        self.read_memory = read_memory
        self.observed = {}
        self.total = 0

    def read(self, address, length):
        _pointer(address)
        if not 0 < length <= 1024 * 1024 or address + length >= 0x800000000000:
            raise ValueError('联系人缓存读取范围无效')
        value = self.read_memory(address, length)
        if len(value) != length:
            raise ValueError('联系人缓存读取不完整')
        key = (address, length)
        if key in self.observed:
            if self.observed[key] != value:
                raise ValueError('联系人缓存正在变化')
        else:
            self.total += length
            if self.total > 16 * 1024 * 1024:
                raise ContactDirectoryError('联系人缓存超出单次读取预算')
            self.observed[key] = value
        return value

    def ptr(self, address):
        return struct.unpack('<Q', self.read(address, 8))[0]

    def string(self, address, limit=512, *, encoding='utf-8'):
        header = self.read(address, 32)
        length, capacity = struct.unpack_from('<QQ', header, 16)
        if length > limit or capacity < length or not (capacity == 15 or 16 <= capacity <= 1048576):
            raise ValueError('联系人字符串布局无效')
        if not length:
            return ''
        start = address if capacity == 15 else _pointer(struct.unpack_from('<Q', header)[0])
        value = self.read(start, length).decode(encoding)
        if encoding == 'ascii' and any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ValueError('联系人标识含无效字符')
        return ''.join(ch for ch in value if ord(ch) >= 32 and ord(ch) != 127 or ch in '\n\r\t')

    def verify(self):
        for (address, length), value in reversed(tuple(self.observed.items())):
            if self.read_memory(address, length) != value:
                raise ValueError('联系人快照已失效')


def _directory_cache(reader, base, rva):
    app = _pointer(reader.ptr(base + rva(APP_GLOBAL_RVA)))
    context = _pointer(reader.ptr(app + 0x68))
    table = _pointer(reader.ptr(context))
    if reader.ptr(table + 0x38) != base + rva(CACHE_PROVIDER_GETTER_RVA):
        raise ValueError('联系人缓存提供者不匹配')
    if reader.read(base + rva(CACHE_PROVIDER_GETTER_RVA) + 28, 8) != bytes.fromhex('48 83 b9 60 04 00 00 00'):
        raise ValueError('联系人缓存访问布局不匹配')
    provider = _pointer(reader.ptr(context + 0x460))
    if reader.ptr(provider) != base + rva(CACHE_PROVIDER_VTABLE_RVA):
        raise ValueError('联系人缓存提供者未就绪')
    head, count = struct.unpack('<QQ', reader.read(provider + 0x90, 16))
    if not 1 <= count <= 512:
        raise ValueError('缓存注册表尚未就绪')
    _pointer(head)
    current, visited, matches = reader.ptr(head), set(), []
    for _ in range(count):
        if current == head or current in visited:
            raise ValueError('缓存注册表不完整')
        visited.add(current)
        obj = reader.ptr(current + 0x30)
        if obj and reader.ptr(obj) == base + rva(CONTACT_CACHE_VTABLE_RVA):
            matches.append(obj)
        current = reader.ptr(current)
    if current != head or len(matches) != 1:
        raise ValueError('联系人缓存尚未就绪')
    return context, matches[0]


def _directory_records(reader, cache):
    # 4.1.15.56 schema f47000 records remark(+78), nickname(+D8),
    # description(+1A0) in the 1D8-byte common prefix. Type 1 additionally
    # embeds ContactExtData at +1E0. The LRU at +108
    # contains strangers/group members and is deliberately not a friend list.
    layout = reader.read(cache + 0xC8, 0x40)
    head, count, buckets, end, capacity, mask, bucket_count = struct.unpack_from('<QQQQQQQ', layout, 8)
    _pointer(head)
    _pointer(buckets)
    if count > MAX_DIRECTORY_CONTACTS:
        raise ContactDirectoryError('联系人缓存数量超过单次扫描上限')
    if (not 1 <= mask <= 65535 or (mask + 1) & mask or bucket_count != mask + 1 or
            end - buckets != bucket_count * 16 or capacity < end):
        raise ValueError('联系人缓存容器布局不匹配')
    current, last, visited, identities = reader.ptr(head), head, set(), set()
    for _ in range(count):
        if current == head or current in visited:
            raise ValueError('联系人缓存容器不完整')
        visited.add(current)
        following, previous = struct.unpack('<QQ', reader.read(current, 16))
        if reader.ptr(previous) != current or reader.ptr(following + 8) != current:
            raise ValueError('联系人缓存链接发生变化')
        key = reader.string(current + 0x10, 127, encoding='ascii')
        record = _pointer(reader.ptr(current + 0x30))
        if key in identities or reader.string(record + 8, 127, encoding='ascii') != key:
            raise ValueError('联系人缓存身份不一致')
        identities.add(key)
        yield key, record
        last = current
        current = following
    if current != head or reader.ptr(head + 8) != last:
        raise ValueError('联系人数量发生变化')


def _directory_component(reader, context, base, vtable_rva):
    # 3823D0 returns AccountContext+28; 32E7E0 queries its first hash table.
    # Keys are opaque binary interface identities, so do not decode or expose them.
    components = _pointer(reader.ptr(context + 0x28))
    header = reader.read(components, 0x40)
    head, count, buckets, end, capacity, mask, bucket_count = struct.unpack_from('<QQQQQQQ', header, 8)
    _pointer(head)
    _pointer(buckets)
    if (count > 1024 or not 1 <= mask <= 65535 or (mask + 1) & mask or
            bucket_count != mask + 1 or end - buckets != bucket_count * 16 or capacity < end):
        raise ValueError('账号组件容器不匹配')
    current, last, visited, matches = reader.ptr(head), head, set(), []
    for _ in range(count):
        if current == head or current in visited:
            raise ValueError('账号组件列表不完整')
        visited.add(current)
        following, previous = struct.unpack('<QQ', reader.read(current, 16))
        if reader.ptr(previous) != current or reader.ptr(following + 8) != current:
            raise ValueError('账号组件列表发生变化')
        obj = reader.ptr(current + 0x30)
        if obj and reader.ptr(obj) == base + vtable_rva:
            matches.append(obj)
        last, current = current, following
    if current != head or reader.ptr(head + 8) != last or len(matches) > 1:
        raise ValueError('账号组件身份不唯一')
    return matches[0] if matches else None


def _tree_nodes(reader, address, limit):
    head, count = struct.unpack('<QQ', reader.read(address, 16))
    _pointer(head)
    if count > limit or reader.read(head + 0x19, 1) != b'\x01':
        raise ValueError('联系人标签树布局无效')
    root = reader.ptr(head + 8)
    if not count:
        if any(reader.ptr(head + offset) != head for offset in (0, 8, 16)):
            raise ValueError('空标签树尚未稳定')
        return []
    stack, visited, nodes = [(root, head)], set(), []
    while stack:
        node, parent = stack.pop()
        if node == head:
            continue
        if node in visited or len(visited) >= count:
            raise ValueError('联系人标签树存在循环')
        visited.add(node)
        header = reader.read(node, 0x20)
        left, actual_parent, right = struct.unpack_from('<QQQ', header)
        if actual_parent != parent or header[0x19] != 0:
            raise ValueError('联系人标签树连接不一致')
        nodes.append(node)
        stack.extend(((right, node), (left, node)))
    if len(nodes) != count:
        raise ValueError('联系人标签树数量不匹配')
    minimum, maximum = root, root
    while reader.ptr(minimum) != head:
        minimum = reader.ptr(minimum)
    while reader.ptr(maximum + 16) != head:
        maximum = reader.ptr(maximum + 16)
    if reader.ptr(head) != minimum or reader.ptr(head + 16) != maximum:
        raise ValueError('联系人标签树边界不匹配')
    return nodes


def _directory_labels(reader, context, base):
    cache = _directory_component(reader, context, base, LABEL_CACHE_VTABLE_RVA)
    if cache is None:
        return None
    labels, visited = [], set()
    # 16AFC30 copies {id,index,name} to node+40/+44/+48, node size 80.
    for node in _tree_nodes(reader, cache + 0x40, 4096):
        key = reader.string(node + 0x20, 16, encoding='ascii')
        identifier, position = struct.unpack('<II', reader.read(node + 0x40, 8))
        name = reader.string(node + 0x48, 256)
        if key != str(identifier) or not identifier or identifier in visited or not name:
            raise ValueError('联系人标签身份不一致')
        visited.add(identifier)
        labels.append((position, {'id': str(identifier), 'name': name}))
    return [row for _, row in sorted(labels, key=lambda value: (value[0], int(value[1]['id'])))]


def _contact_details(reader, record, base, labels):
    details = {'sex': 'unknown', 'country': '', 'province': '', 'city': '', 'signature': '',
               'tag_ids': [], 'tags': [], 'tags_status': 'unavailable'}
    try:
        kind = struct.unpack('<I', reader.read(record + 4, 4))[0]
        if kind != 1 or reader.ptr(record + 0x1E0) != base + CONTACT_EXT_VTABLE_RVA:
            return details
        presence = struct.unpack('<I', reader.read(record + 0x304, 4))[0]
    except (OSError, ValueError, struct.error):
        return details

    # F66DA0 constructs the embedded proto; 27B8A10 transfers these fields.
    if presence & 2:
        try:
            sex = struct.unpack('<I', reader.read(record + 0x1F8, 4))[0]
            details['sex'] = {1: 'male', 2: 'female'}.get(sex, 'unknown')
        except (OSError, ValueError, struct.error):
            pass
    for name, offset, bit in (('signature', 0x200, 8), ('country', 0x208, 0x10),
                              ('province', 0x210, 0x20), ('city', 0x218, 0x40)):
        if presence & bit:
            try:
                details[name] = reader.string(reader.ptr(record + offset), 4096)
            except (OSError, ValueError, struct.error):
                pass

    # 27AE6E0 reads LabelIDList into +2A0 and sets proto presence bit 29.
    # Native values may have leading/trailing commas; empty segments are valid.
    if presence & 0x20000000:
        try:
            value = reader.string(reader.ptr(record + 0x2A0), 65536, encoding='ascii')
            parts = [part for part in value.split(',') if part]
            if len(parts) > 4096 or any(not part.isdecimal() or len(part) > 10 or
                                        not 0 < int(part) <= 0xFFFFFFFF for part in parts):
                raise ValueError('联系人标签关联无效')
            identifiers = list(dict.fromkeys(str(int(part)) for part in parts))
            names = {row['id']: row['name'] for row in labels or []}
            details.update(tag_ids=identifiers,
                           tags=[names[key] for key in identifiers if key in names],
                           tags_status='ready' if all(key in names for key in identifiers) else 'unavailable')
        except (OSError, ValueError, struct.error):
            pass
    return details


def read_contact_directory(read_memory, core_base, *, expected_account):
    """当前微信已加载的好友快照；不触发网络同步，不推断未知详情。"""
    validate_account(expected_account)
    if read_current_account(read_memory, core_base) != expected_account:
        return None
    profile = select_memory_profile(read_memory, core_base)
    if profile is None:
        return None
    if profile.version != '4.1.15.56':
        raise ContactDirectoryError('当前联系人扫描已核对微信 4.1.15.56，请使用该适配版本')
    reader = _DirectoryReader(read_memory)
    try:
        context, cache = _directory_cache(reader, core_base, profile.rva)
        try:
            labels = _directory_labels(reader, context, core_base)
        except (OSError, ValueError, struct.error):
            labels = None
        contacts = []
        for wxid, record in _directory_records(reader, cache):
            deleted, flags = struct.unpack('<II', reader.read(record + 0x68, 8))
            if deleted or not flags & 1 or wxid == expected_account:
                continue
            try:
                validate_account(wxid)
            except ValueError:
                continue
            contacts.append({'wxid': wxid, 'nickname': reader.string(record + 0xD8, 256),
                             'alias': reader.string(record + 0x28, 256),
                             'remark': reader.string(record + 0x78, 1024),
                             'description': reader.string(record + 0x1A0, 2048),
                             **_contact_details(reader, record, core_base, labels)})
        contacts.sort(key=lambda row: (row['remark'] or row['nickname'] or row['wxid']).casefold())
        result = {'contacts': contacts, 'tags': labels or [], 'source': 'wechat_cache', 'complete': False}
        if len(json.dumps(result, ensure_ascii=False).encode('utf-8')) > MAX_DIRECTORY_BYTES:
            raise ContactDirectoryError('联系人信息超过单次响应上限，暂不支持该规模的扫描')
        reader.verify()
        if read_current_account(read_memory, core_base) != expected_account:
            return None
        return result
    except (OSError, ValueError, struct.error):
        return None


def _read_friend_cache_name(read_memory, core_base, wxid, *, expected_account=None):
    """按实际成员 ID 读取微信名。缺失、切换、重建缓存时返回 None，不猜名字。

    仅遍历已核对的缓存对象，不调用微信函数、不扫描整个进程、不读取群昵称或备注。
    """
    validate_account(wxid)
    account = read_current_account(read_memory, core_base)
    if not account or (expected_account is not None and account != expected_account):
        return None
    try:
        profile = select_memory_profile(read_memory, core_base)
        if profile is None or CONTACT_CACHE_VTABLE_RVA not in profile.rvas:
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


def _read_group_contact_cache_name(read_memory, core_base, wxid, *, expected_account=None):
    """读取 4.2.0.11 已验证的群成员／非好友 LRU，不触发网络请求。"""
    account = read_current_account(read_memory, core_base)
    if not account or (expected_account is not None and account != expected_account):
        return None
    try:
        profile = select_memory_profile(read_memory, core_base)
        if profile is None or profile.version != '4.2.0.11':
            return None
        # 与群缓存使用相同的 LRU 链表及哈希索引布局；仅按真实 ID 查找。
        from .group_members import _StableReader, _find_room
        directory = _DirectoryReader(read_memory)
        _, cache = _directory_cache(directory, core_base, profile.rva)
        reader = _StableReader(read_memory)
        contact = _find_room(reader, cache + 0xD0, wxid)  # LRU 起点为 cache + 0x108
        if not contact or reader.string(contact + 8, 127, encoding='ascii') != wxid:
            return None
        name = reader.string(contact + 0xD8, 256, empty=True).strip()
        if not name or len(name) > 64 or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
            return None
        reader.verify()
        directory.verify()
        if read_current_account(read_memory, core_base) != account:
            return None
        return name
    except (OSError, ValueError, ContactDirectoryError, struct.error):
        return None


def read_contact_name(read_memory, core_base, wxid, *, expected_account=None):
    """按成员 ID 查询微信名；好友缓存优先，缺失时查询已验证的群成员缓存。"""
    validate_account(wxid)
    return (_read_friend_cache_name(read_memory, core_base, wxid, expected_account=expected_account)
            or _read_group_contact_cache_name(read_memory, core_base, wxid, expected_account=expected_account))
