"""Linux 微信联系人数据的只读基础解析，当前仅支持已核对的标签缓存。"""
from __future__ import annotations

import struct

from .account import validate_account


class LinuxContactsError(RuntimeError):
    """缓存不稳定、不可读取或对应的微信账号已变化。"""


# These offsets are for the exact Linux ELF build verified by the bridge.  A
# caller MUST validate the executable fingerprint before providing its base.
_APP = 0xACF6710
_APP_VTABLE = 0xA89CBA8
_HUB_VTABLE = 0xA8AA750
_LABEL_TYPE_NAME_PTR = 0x61BB8E7 + 0x47C5AB9 + 8
_LABEL_VTABLE = 0xA981370
_MAX_COMPONENTS = 4096
_MAX_LABELS = 4096


class _Reader:
    def __init__(self, read_memory):
        self.read_memory = read_memory
        self.remaining = 8 * 1024 * 1024

    def read(self, address, length):
        if not isinstance(address, int) or address < 4096 or length < 0 or length > self.remaining:
            raise LinuxContactsError('联系人缓存读取范围无效')
        self.remaining -= length
        try:
            data = self.read_memory(address, length)
        except (OSError, ValueError, OverflowError):
            raise LinuxContactsError('联系人缓存不可读取') from None
        if len(data) != length:
            raise LinuxContactsError('联系人缓存读取不完整')
        return data

    def u64(self, address):
        return struct.unpack('<Q', self.read(address, 8))[0]

    def cstring(self, address, limit=128):
        # The metadata is a fixed 32-byte ASCII type key with a terminating NUL.
        data = self.read(address, 33)
        if data[32] != 0 or any(c not in b'0123456789abcdef' for c in data[:32]):
            raise LinuxContactsError('组件身份信息无效')
        return data[:32]

    def string(self, address, limit=1024, *, ascii_only=False):
        header = self.read(address, 24)
        if header[0] & 1:
            size, pointer = struct.unpack_from('<QQ', header, 8)
            if size > limit or (size and not pointer):
                raise LinuxContactsError('联系人字符串布局无效')
            data = self.read(pointer, size) if size else b''
        else:
            size = header[0] >> 1
            if size > min(limit, 22):
                raise LinuxContactsError('联系人字符串布局无效')
            data = header[1:1 + size]
        try:
            text = data.decode('ascii' if ascii_only else 'utf-8')
        except UnicodeError:
            raise LinuxContactsError('联系人字符串编码无效') from None
        if '\x00' in text:
            raise LinuxContactsError('联系人字符串含无效字符')
        if (self.read(address, 24) != header or
                header[0] & 1 and size and self.read(pointer, size) != data):
            raise LinuxContactsError('联系人缓存正在变化')
        return text


def _identity(reader, base, expected_account):
    app = reader.u64(base + _APP)
    if not app or reader.u64(app) != base + _APP_VTABLE:
        raise LinuxContactsError('微信账号组件未就绪')
    hub = reader.u64(app + 0x68)
    if not hub or reader.u64(hub) != base + _HUB_VTABLE:
        raise LinuxContactsError('微信账号组件不匹配')
    if not reader.read(hub + 0x38, 1)[0] & 1:
        raise LinuxContactsError('微信尚未登录')
    account = reader.string(hub + 0x48, 127, ascii_only=True)
    if account != expected_account:
        raise LinuxContactsError('微信账号已变化，请重新扫描')
    return app, hub


def _label_manager(reader, base, hub):
    center = reader.u64(hub + 0x28)
    if not center:
        raise LinuxContactsError('微信账号组件未就绪')
    key_ptr = reader.u64(base + _LABEL_TYPE_NAME_PTR)
    key = reader.cstring(key_ptr)
    buckets = reader.u64(center + 8)
    node = reader.u64(center + 16)
    if not 0 < buckets <= _MAX_COMPONENTS:
        raise LinuxContactsError('微信账号组件布局不匹配')
    seen = set()
    matches = []
    while node:
        if node in seen or len(seen) >= _MAX_COMPONENTS:
            raise LinuxContactsError('微信账号组件列表不稳定')
        seen.add(node)
        following = reader.u64(node)
        node_key = reader.string(node + 16, 128, ascii_only=True).encode('ascii')
        if node_key == key:
            obj = reader.u64(node + 0x28)
            if not obj or reader.u64(obj) != base + _LABEL_VTABLE:
                raise LinuxContactsError('标签缓存身份不匹配')
            matches.append(obj)
        if reader.u64(node) != following:
            raise LinuxContactsError('微信账号组件列表正在变化')
        node = following
    if len(matches) != 1 or reader.u64(hub + 0x28) != center:
        raise LinuxContactsError('标签缓存未就绪或身份不唯一')
    return matches[0]


def read_linux_labels(read_memory, core_base, *, expected_account):
    """读取当前账号已加载的标签；调用前必须核验目标程序的 ELF 指纹。

    返回的 complete=False 表示只读当前内存缓存，不能断定微信已同步全部标签。
    不读取聊天消息、不记录账号或标签内容，也不保存数据库密钥。
    """
    validate_account(expected_account)
    if not isinstance(core_base, int) or core_base < 4096:
        raise LinuxContactsError('微信程序地址无效')
    reader = _Reader(read_memory)
    identity = _identity(reader, core_base, expected_account)
    manager = _label_manager(reader, core_base, identity[1])
    sentinel = manager + 0x40
    first = reader.u64(manager + 0x38)
    root = reader.u64(sentinel)
    count = reader.u64(sentinel + 8)
    if count > _MAX_LABELS:
        raise LinuxContactsError('标签数量超过读取上限')
    if not count:
        if root or first != sentinel:
            raise LinuxContactsError('空标签缓存尚未稳定')
        labels = []
    else:
        if not root or not first:
            raise LinuxContactsError('标签缓存尚未稳定')
        stack, seen, labels, identifiers = [(root, sentinel)], set(), [], set()
        while stack:
            node, parent = stack.pop()
            if not node:
                continue
            if node in seen or len(seen) >= count:
                raise LinuxContactsError('标签树存在循环或数量变化')
            seen.add(node)
            left, right, actual_parent = struct.unpack('<QQQ', reader.read(node, 24))
            if actual_parent != parent:
                raise LinuxContactsError('标签树连接正在变化')
            identifier, order = struct.unpack('<II', reader.read(node + 0x38, 8))
            name = reader.string(node + 0x40, 512)
            if not identifier or identifier in identifiers or not name:
                raise LinuxContactsError('标签缓存内容不完整')
            identifiers.add(identifier)
            labels.append((order, {'id': str(identifier), 'name': name}))
            stack.extend(((right, node), (left, node)))
        if len(seen) != count:
            raise LinuxContactsError('标签数量发生变化')
        smallest = root
        while reader.u64(smallest):
            smallest = reader.u64(smallest)
        if smallest != first:
            raise LinuxContactsError('标签树边界发生变化')
        labels = [item for _, item in sorted(labels, key=lambda row: (row[0], int(row[1]['id'])))]
    if (reader.u64(manager + 0x38) != first or reader.u64(sentinel) != root or
            reader.u64(sentinel + 8) != count or _identity(reader, core_base, expected_account) != identity):
        raise LinuxContactsError('微信账号或标签缓存已变化，请重新扫描')
    return {'tags': labels, 'source': 'wechat_cache', 'complete': False}


class VerifiedLinuxProcess:
    """绑定版本、账号和进程生命周期的只读内存访问。"""
    def __init__(self, pid, expected_account, expected_sha256):
        import os
        from pathlib import Path
        self.pid, self.account, self.expected_sha256 = pid, expected_account, expected_sha256
        self.proc = Path('/proc') / str(pid)
        self.fd = None
        if type(pid) is not int or pid <= 0 or not isinstance(expected_sha256, str) or len(expected_sha256) != 64:
            raise LinuxContactsError('微信进程或版本指纹无效')
        validate_account(expected_account)

    def __enter__(self):
        import hashlib
        import os
        try:
            if self.proc.stat().st_uid != os.geteuid():
                raise LinuxContactsError('拒绝读取其他用户的微信进程')
            self.start_time = (self.proc / 'stat').read_bytes().rsplit(b')', 1)[1].split()[19]
            with (self.proc / 'exe').open('rb') as exe:
                self.exe_stat = os.fstat(exe.fileno())
                if hashlib.file_digest(exe, 'sha256').hexdigest() != self.expected_sha256:
                    raise LinuxContactsError('微信版本不匹配，不能读取联系人')
            bases = []
            for line in (self.proc / 'maps').read_text().splitlines():
                parts = line.split(maxsplit=5)
                if len(parts) >= 5 and parts[2] == '00000000' and 'r' in parts[1] and int(parts[4]) == self.exe_stat.st_ino:
                    bases.append(int(parts[0].split('-', 1)[0], 16))
            if len(bases) != 1:
                raise LinuxContactsError('微信进程映射未就绪或身份不唯一')
            self.base = bases[0]
            self.fd = os.open(self.proc / 'mem', os.O_RDONLY | os.O_CLOEXEC)
            self.identity = _identity(_Reader(self.read), self.base, self.account)
            return self
        except LinuxContactsError:
            self.close()
            raise
        except (OSError, IndexError, ValueError, OverflowError):
            self.close()
            raise LinuxContactsError('微信进程不可读取或已经退出') from None

    def read(self, address, size):
        import os
        return os.pread(self.fd, size, address)

    def verify(self):
        import os
        try:
            end_time = (self.proc / 'stat').read_bytes().rsplit(b')', 1)[1].split()[19]
            if (self.start_time != end_time or os.stat(self.proc / 'exe').st_ino != self.exe_stat.st_ino or
                    _identity(_Reader(self.read), self.base, self.account) != self.identity):
                raise LinuxContactsError('微信进程或账号已变化，请重新扫描')
        except LinuxContactsError:
            raise
        except (OSError, IndexError, ValueError, OverflowError):
            raise LinuxContactsError('微信进程已退出，请重新扫描') from None

    def close(self):
        import os
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __exit__(self, *args):
        self.close()


def read_process_linux_labels(pid, *, expected_account, expected_sha256):
    """只读当前受管进程的标签缓存；不解密或复制联系人数据库。"""
    with VerifiedLinuxProcess(pid, expected_account, expected_sha256) as process:
        snapshot = read_linux_labels(process.read, process.base, expected_account=expected_account)
        process.verify()
        return snapshot
