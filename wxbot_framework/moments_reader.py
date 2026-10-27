"""只读电脑微信已经加载的朋友圈缓存；不拉取历史、不注入、不下载媒体。

布局来自 4.1.15.56 的离线反汇编及本机只读样本核对。入口同时核对核心
文件完整指纹与当前账号；原生地址、原始 XML 和媒体链接不进入返回数据。
"""
from __future__ import annotations

import ctypes as c
from datetime import datetime, timedelta, timezone
from pathlib import Path
import struct
import time

from .account import APP_GLOBAL_RVA, read_current_account, validate_account

_SOURCE_VTABLE = 0x9819B28
_SOURCE_SECONDARY_VTABLE = 0x9819BA8
_ADDRESS_END = 0x800000000000
_SCAN_BYTES = 512 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_SECONDS = 8.0
_UNLOADED = '请先在电脑微信打开朋友圈，待列表加载后重新读取'
_CHANGED = '朋友圈列表正在变化，请等加载完成后重新读取'
_IDENTITY = '微信登录账号已变化，请刷新账号后重新读取'
_DEADLINE = '朋友圈读取超时，请打开电脑微信朋友圈后重试'


class MomentsReadError(RuntimeError):
    """可以原样呈现给管理台的固定错误，不携带原生异常内容。"""


class _Changed(ValueError):
    pass


class _InteractionChanged(_Changed):
    """互动数据一旦变化，放弃整份读取，避免重试时混入其他状态。"""
    pass


class _Budget:
    def __init__(self, cancelled):
        self.cancelled = cancelled
        self.deadline = time.monotonic() + _SECONDS
        self.scanned = 0

    def check(self):
        if self.cancelled():
            raise MomentsReadError('朋友圈读取已取消')
        if time.monotonic() >= self.deadline:
            raise MomentsReadError(_DEADLINE)

    def consume(self, size):
        self.check()
        if self.scanned + size > _SCAN_BYTES:
            raise MomentsReadError('未在限定缓存范围找到朋友圈，请打开电脑微信朋友圈后重试')
        self.scanned += size


def _pointer(value):
    if type(value) is not int or not 0x10000 <= value < _ADDRESS_END:
        raise ValueError('invalid pointer')
    return value


def _read(memory, address, size):
    _pointer(address)
    if not 0 <= size <= _CHUNK_BYTES or address + size > _ADDRESS_END:
        raise ValueError('invalid read range')
    raw = memory.read(address, size)
    if len(raw) != size:
        raise OSError('incomplete read')
    return raw


def _q(memory, address):
    return struct.unpack('<Q', _read(memory, address, 8))[0]


def _string(memory, address, maximum=8192):
    header = _read(memory, address, 32)
    length, capacity = struct.unpack_from('<QQ', header, 16)
    if length > maximum or capacity < length or not (capacity == 15 or 16 <= capacity <= 16 * 1024 * 1024):
        raise ValueError('invalid string')
    pointer = address if capacity == 15 else _pointer(struct.unpack_from('<Q', header)[0])
    raw = _read(memory, pointer, length) if length else b''
    value = raw.decode('utf-8')
    if any(ord(char) < 32 and char not in '\r\n\t' for char in value):
        raise ValueError('invalid text')
    if header != _read(memory, address, 32) or (length and raw != _read(memory, pointer, length)):
        raise _Changed()
    return value


def _vector(memory, current):
    raw = _read(memory, current + 0x30, 24)
    begin, end, capacity = struct.unpack('<QQQ', raw)
    if begin == end == capacity == 0:
        return raw, 0, 0
    if not (0x10000 <= begin <= end <= capacity < _ADDRESS_END and
            (end - begin) % 16 == 0 and (capacity - begin) % 16 == 0 and
            capacity - begin <= 16 * 10000):
        raise ValueError('invalid vector')
    return raw, begin, (end - begin) // 16


def _source(memory, address, core):
    if (_q(memory, address) != core + _SOURCE_VTABLE or
            _q(memory, address + 0x10) != core + _SOURCE_SECONDARY_VTABLE or
            _read(memory, address + 0x1D0, 4) != b'\0' * 4):
        return None
    shared = _read(memory, address + 0x1A0, 16)
    current, control = struct.unpack('<QQ', shared)
    _pointer(current)
    _pointer(control)
    raw, begin, count = _vector(memory, current)
    return {'address': address, 'current': current, 'shared': shared,
            'vector': raw, 'begin': begin, 'count': count}


def _locate(memory, core, nearby, budget):
    regions = memory.regions(budget)
    regions.sort(key=lambda row: abs(row[0] - nearby))
    signature = struct.pack('<Q', core + _SOURCE_VTABLE)
    empty = None
    for base, size in regions:
        # One signature's overlap makes an 8-byte vtable spanning a chunk visible.
        tail = b''
        for offset in range(0, size, _CHUNK_BYTES):
            size_now = min(_CHUNK_BYTES, size - offset)
            budget.consume(size_now)
            try:
                raw = _read(memory, base + offset, size_now)
            except OSError:
                tail = b''
                continue
            chunk = tail + raw
            cursor = 0
            while True:
                budget.check()
                index = chunk.find(signature, cursor)
                if index < 0:
                    break
                cursor = index + 1
                address = base + offset - len(tail) + index
                if address % 8:
                    continue
                try:
                    candidate = _source(memory, address, core)
                except (OSError, ValueError, struct.error):
                    continue
                if candidate:
                    if candidate['count']:
                        return candidate
                    empty = candidate
            tail = chunk[-7:]
    return empty


def _interactions(memory, item_data, offset, budget, snapshots):
    """复制有界的互动列表，并记录供整份快照结束前再次核对的内存。"""
    begin, end, capacity = struct.unpack_from('<QQQ', item_data, offset)
    if begin == end == capacity == 0:
        return [], 0, False
    if not (0x10000 <= begin <= end <= capacity < _ADDRESS_END and
            (end - begin) % 16 == 0 and (capacity - begin) % 16 == 0 and
            capacity - begin <= 16 * 10000):
        raise ValueError('invalid interaction vector')
    total = (end - begin) // 16
    count = min(total, 100)
    pairs = _read(memory, begin, count * 16) if count else b''
    if count:
        snapshots.append(('interaction_bytes', begin, pairs, 0))
    records = []
    for index in range(count):
        budget.check()
        obj, control = struct.unpack_from('<QQ', pairs, index * 16)
        _pointer(obj)
        _pointer(control)
        before = _read(memory, obj, 0x90)
        try:
            username = _string(memory, obj + 0x10, 127)
            validate_account(username)
            nickname = _string(memory, obj + 0x30, 512)
            content = _string(memory, obj + 0x50, 8192)
        except _Changed:
            raise _InteractionChanged() from None
        created = struct.unpack_from('<I', before, 0x78)[0]
        if not 946684800 <= created <= 2147483647:
            raise ValueError('invalid interaction time')
        if before != _read(memory, obj, 0x90):
            raise _InteractionChanged()
        snapshots.append(('interaction_bytes', obj, before, 0))
        for position, value, maximum in ((0x10, username, 127), (0x30, nickname, 512),
                                         (0x50, content, 8192)):
            snapshots.append(('interaction_text', obj + position, value, maximum))
        records.append({'username': username, 'nickname': nickname, 'content': content,
                        'type': struct.unpack_from('<I', before, 0x74)[0],
                        'timestamp': created, 'comment_id': str(struct.unpack_from('<Q', before, 8)[0])})
    return records, total, total > count


def _decode(memory, candidate, account, core, limit, budget):
    rows, identifiers, snapshots = [], set(), []
    for index in range(min(limit, candidate['count'])):
        budget.check()
        pair_address = candidate['begin'] + 16 * index
        shared = _read(memory, pair_address, 16)
        item, control = struct.unpack('<QQ', shared)
        _pointer(item)
        _pointer(control)
        before = _read(memory, item, 0x310)
        identifier = struct.unpack_from('<Q', before)[0]
        username = _string(memory, item + 0x190, 127)
        validate_account(username)
        created = struct.unpack_from('<I', before, 0x1B4)[0]
        if not identifier or identifier in identifiers or not 946684800 <= created <= 2147483647:
            raise ValueError('invalid feed identity')
        text = _string(memory, item + 0x1B8)
        nickname = _string(memory, item + 8, 512)
        private = before[0x1B0]
        if private not in (0, 1):
            raise ValueError('invalid visibility')
        raw_like_flag = before[0xC2]
        if raw_like_flag not in (0, 1):
            raise ValueError('invalid like flag')
        likes, likes_total, likes_truncated = _interactions(memory, before, 0x50, budget, snapshots)
        comments, comments_total, comments_truncated = _interactions(memory, before, 0x70, budget, snapshots)
        content, content_control = struct.unpack_from('<QQ', before, 0x300)
        content_type, media_count = None, 0
        if content:
            _pointer(content)
            _pointer(content_control)
            media_before = _read(memory, content + 0x118, 16)
            type_before = _read(memory, content + 0x60, 4)
            content_type = struct.unpack('<I', type_before)[0]
            begin, end = struct.unpack('<QQ', media_before)
            if (end < begin or (end - begin) % 16 or end - begin > 16 * 100 or
                    (begin and not 0x10000 <= begin <= end < _ADDRESS_END) or
                    (not begin and end)):
                raise ValueError('invalid media vector')
            media_count = (end - begin) // 16
            if (media_before != _read(memory, content + 0x118, 16) or
                    type_before != _read(memory, content + 0x60, 4)):
                raise _Changed()
        elif content_control:
            raise ValueError('invalid content shared pointer')
        after = _read(memory, item, 0x310)
        if (before[0x50:0x90] != after[0x50:0x90] or before[0xC2] != after[0xC2]):
            raise _InteractionChanged()
        if before != after or shared != _read(memory, pair_address, 16):
            raise _Changed()
        snapshots.append(('item', item, before, 0))
        snapshots.append(('bytes', pair_address, shared, 0))
        identifiers.add(identifier)
        rows.append({'sns_id': str(identifier), 'author': username, 'nickname': nickname,
                     'is_self': username == account, 'timestamp': created,
                     'time': datetime.fromtimestamp(created, timezone(timedelta(hours=8))).isoformat(),
                     'private': bool(private), 'content': text, 'content_type': content_type,
                     'media_count': media_count, 'raw_like_flag': bool(raw_like_flag),
                     'likes': likes, 'likes_total': likes_total, 'likes_truncated': likes_truncated,
                     'comments': comments, 'comments_total': comments_total, 'comments_truncated': comments_truncated})
    budget.check()
    for kind, address, value, maximum in snapshots:
        budget.check()
        try:
            if kind in ('bytes', 'item', 'interaction_bytes'):
                current = _read(memory, address, len(value))
            else:
                current = _string(memory, address, maximum)
        except _Changed:
            if kind == 'interaction_text':
                raise _InteractionChanged() from None
            raise
        if current != value:
            if kind.startswith('interaction_') or (kind == 'item' and
                    (current[0x50:0x90] != value[0x50:0x90] or current[0xC2] != value[0xC2])):
                raise _InteractionChanged()
            raise _Changed()
    if _source(memory, candidate['address'], core) != candidate:
        raise _Changed()
    return {'source': 'loaded_pc_moments_cache', 'rows': rows,
            'loaded_count': candidate['count'], 'truncated': len(rows) < candidate['count']}


class _Region(c.Structure):
    _fields_ = [('base', c.c_uint64), ('allocation', c.c_uint64),
                ('allocation_protect', c.c_uint32), ('partition', c.c_uint16), ('pad', c.c_uint16),
                ('size', c.c_uint64), ('state', c.c_uint32), ('protect', c.c_uint32), ('kind', c.c_uint32)]


assert c.sizeof(_Region) == 48


class _Memory:
    """持有独立只读句柄；关闭它不会影响运行中的桥接。"""
    def __init__(self, pid):
        kernel = c.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [c.c_uint32, c.c_int, c.c_uint32]
        kernel.OpenProcess.restype = c.c_void_p
        kernel.CloseHandle.argtypes = [c.c_void_p]
        kernel.VirtualQueryEx.argtypes = [c.c_void_p, c.c_void_p, c.POINTER(_Region), c.c_size_t]
        kernel.VirtualQueryEx.restype = c.c_size_t
        kernel.ReadProcessMemory.argtypes = [c.c_void_p, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)]
        kernel.ReadProcessMemory.restype = c.c_int
        self.kernel = kernel
        self.handle = kernel.OpenProcess(0x410, False, pid)  # QUERY_INFORMATION | VM_READ
        if not self.handle:
            raise OSError('cannot open process')

    def read(self, address, size):
        buffer, received = c.create_string_buffer(size), c.c_size_t()
        if not self.kernel.ReadProcessMemory(self.handle, address, buffer, size, c.byref(received)) or received.value != size:
            raise OSError('cannot read process')
        return buffer.raw

    def regions(self, budget):
        regions, address = [], 0
        while address < _ADDRESS_END:
            budget.check()
            region = _Region()
            if self.kernel.VirtualQueryEx(self.handle, address, c.byref(region), c.sizeof(region)) != c.sizeof(region):
                break
            next_address = region.base + region.size
            if next_address <= address or next_address > _ADDRESS_END:
                break
            if (region.state == 0x1000 and region.kind == 0x20000 and
                    region.protect & 0xFF == 4 and not region.protect & 0x100):
                regions.append((region.base, region.size))
            address = next_address
        return regions

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _target(pid):
    # Delayed imports keep decoder tests portable and do not load any bridge DLL.
    from native.moments_transport import _core_profile, _native
    module = _native().modules(pid).get('weixin.dll')
    if not module:
        raise MomentsReadError('未找到目标电脑微信，请刷新账号后重试')
    core, path = module
    profile = _core_profile(Path(path))
    if profile is None or profile.version != '4.1.15.56':
        raise MomentsReadError('朋友圈读取仅适配已核对核心指纹的微信 4.1.15.56')
    return core, profile


def read_loaded_moments(pid: int, expected_account: str, *, limit: int = 20,
                        cancelled=lambda: False) -> dict:
    """返回目标账号当前已加载列表的一份稳定快照，最多读取 50 条。

    此操作只读进程内存，不请求微信服务器、不自动翻页，亦不保存正文到文件。
    调用方应把返回数据限制在选中账号的本地认证会话内，并在切换连接后丢弃。
    """
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        raise ValueError('朋友圈目标进程无效')
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('朋友圈读取条数应为 1–50')
    validate_account(expected_account)
    if not callable(cancelled):
        raise ValueError('朋友圈取消回调无效')
    budget = _Budget(cancelled)
    memory = None
    try:
        budget.check()
        core, profile = _target(pid)
        budget.check()
        memory = _Memory(pid)
        if read_current_account(memory.read, core) != expected_account:
            raise MomentsReadError(_IDENTITY)
        nearby = _q(memory, core + profile.rva(APP_GLOBAL_RVA))
        _pointer(nearby)
        candidate = _locate(memory, core, nearby, budget)
        if candidate is None:
            raise MomentsReadError(_UNLOADED)
        result = None
        for attempt in range(2):
            budget.check()
            try:
                result = _decode(memory, candidate, expected_account, core, limit, budget)
                break
            except _InteractionChanged:
                raise MomentsReadError(_CHANGED) from None
            except _Changed:
                if attempt:
                    raise MomentsReadError(_CHANGED) from None
                candidate = _source(memory, candidate['address'], core)
                if candidate is None:
                    raise MomentsReadError(_CHANGED) from None
        if read_current_account(memory.read, core) != expected_account:
            raise MomentsReadError(_IDENTITY)
        budget.check()
        return result
    except MomentsReadError:
        raise
    except (OSError, ValueError, UnicodeError, struct.error, ImportError, AttributeError):
        raise MomentsReadError('暂时无法读取朋友圈缓存，请确认目标微信在线并打开朋友圈后重试') from None
    finally:
        if memory is not None:
            memory.close()
