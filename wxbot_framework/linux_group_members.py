"""已核对 Linux 微信联系人库的群成员快照；密钥和解密数据仅在内存中。"""
from pathlib import Path
import os
import threading
import time
from urllib.parse import quote

from .account import validate_account
from .group_management import validate_group_id
from .linux_contacts import LinuxContactsError, VerifiedLinuxProcess
from .linux_contact_database import _contact_db, _database_key, _varint, _PAGE_SIZE

MAX_MEMBERS = 2000
MAX_EXT_BYTES = 1024 * 1024


def _fields(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_EXT_BYTES:
        raise ValueError('群成员扩展数据不可用')
    cursor, count = 0, 0
    while cursor < len(raw):
        key, cursor = _varint(raw, cursor)
        number, wire = key >> 3, key & 7
        count += 1
        if not 0 < number < (1 << 29) or count > MAX_MEMBERS * 16:
            raise ValueError('群成员扩展字段越界')
        if wire == 0:
            value, cursor = _varint(raw, cursor)
        elif wire in (1, 2, 5):
            if wire == 2:
                size, cursor = _varint(raw, cursor)
            else:
                size = 8 if wire == 1 else 4
            if size > len(raw) - cursor:
                raise ValueError('群成员扩展数据不完整')
            value, cursor = raw[cursor:cursor + size], cursor + size
        else:
            raise ValueError('不支持的群成员扩展字段')
        yield number, wire, value


def _member_id(value):
    value = value.decode('ascii') if isinstance(value, bytes) else value
    validate_account(value)
    if value in ('.', '..', 'notify@all') or value.startswith(('v3_', 'v4_')):
        raise ValueError('群成员 ID 无效')
    return value


def _name(value):
    try:
        value = value.decode('utf8') if isinstance(value, bytes) else value
        if not isinstance(value, str) or len(value) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            return ''
        return value.strip()
    except UnicodeError:
        return ''


def _members(raw):
    members = {}
    for number, wire, value in _fields(raw):
        if number != 1:
            continue
        if wire != 2 or len(members) >= MAX_MEMBERS:
            raise ValueError('群成员数量或字段类型不匹配')
        fields = {}
        for child, child_wire, item in _fields(value):
            if child in (1, 2):
                if child_wire != 2 or child in fields:
                    raise ValueError('群成员身份或昵称字段不唯一')
                fields[child] = item
        identifier = _member_id(fields.get(1, b''))
        if identifier in members:
            raise ValueError('群成员 ID 重复')
        members[identifier] = _name(fields.get(2, b''))
    return members


def _admin_flags(raw):
    """4.1.13.26 ChatroomMemberLocalData field 3 contains member flags.

    Missing or malformed flags remain unknown; membership validation is separate.
    """
    result = {}
    for number, wire, child in _fields(raw):
        if number != 1 or wire != 2:
            continue
        fields = list(_fields(child))
        identifier = _member_id(next(value for n, w, value in fields if n == 1 and w == 2))
        flags = [(w, value) for n, w, value in fields if n == 3]
        result[identifier] = (bool(flags[0][1] & 0x10) if len(flags) == 1 and
            flags[0][0] == 0 and 0 <= flags[0][1] <= 0xffffffff else None)
    return result


def read_group_snapshot(conn, room, expected_account):
    """事务内核对 protobuf 完整名单与独立 SQL 成员索引；未就绪返回 None。"""
    validate_group_id(room)
    validate_account(expected_account)
    try:
        records = list(conn.execute('''SELECT r.id,r.owner,
            CASE WHEN length(r.ext_buffer)<=? THEN r.ext_buffer ELSE NULL END,
            c.delete_flag,c.is_in_chat_room
            FROM chat_room r JOIN contact c ON c.username=r.username
            WHERE r.username=? LIMIT 2''', (MAX_EXT_BYTES, room)))
        if len(records) != 1:
            return None
        room_id, owner, raw, deleted, joined = records[0]
        if type(room_id) is not int or room_id <= 0 or deleted != 0 or joined != 1:
            return None
        members = _members(raw)
        if not members or expected_account not in members:
            return None
        rows = list(conn.execute('''SELECT c.username,c.nick_name FROM chatroom_member m
            LEFT JOIN contact c ON c.id=m.member_id WHERE m.room_id=? LIMIT ?''',
            (room_id, MAX_MEMBERS + 1)))
        if len(rows) != len(members):
            return None
        names = {_member_id(identifier): _name(name) for identifier, name in rows}
        if len(names) != len(rows) or names.keys() != members.keys():
            return None
        admins = _admin_flags(raw)
        owner = owner if isinstance(owner, str) and owner in members else None
        return tuple(dict(wxid=identifier, group_nickname=members[identifier], wechat_name=names[identifier],
            nickname=members[identifier] or names[identifier], role='owner' if identifier == owner else
                'admin' if admins.get(identifier) is True else 'member' if admins.get(identifier) is False else 'unknown',
            is_owner=True if identifier == owner else False if owner else None,
            is_admin=False if identifier == owner else admins.get(identifier),
            role_source='wechat_database' if identifier == owner or admins.get(identifier) is not None else '')
            for identifier in sorted(members))
    except (ValueError, TypeError, UnicodeError):
        return None


class LinuxGroupReader:
    """一进程一账号的只读连接；每次开启新事务，不缓存成员名单或密钥文件。"""
    def __init__(self, pid, *, expected_account, expected_sha256, instance_home):
        self.pid, self.account, self.sha256 = pid, validate_account(expected_account), expected_sha256
        self.home = Path(instance_home)
        self._lock = threading.RLock()
        self._process = self._conn = self._db = self._identity = None
        self._closed = False
        self._retry_after = 0

    def _discard(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._process is not None:
            self._process.close()
            self._process = None
        self._db = self._identity = None

    def _open(self):
        import sqlcipher3
        self._process = VerifiedLinuxProcess(self.pid, self.account, self.sha256).__enter__()
        self._db = _contact_db(self._process, self.home)
        with self._db.open('rb') as file:
            status, page = os.fstat(file.fileno()), file.read(_PAGE_SIZE)
        self._identity = (status.st_dev, status.st_ino, page[:16])
        key = _database_key(self._process, page)
        self._process.verify()
        self._conn = sqlcipher3.connect('file:' + quote(str(self._db), safe='/') + '?mode=ro',
                                       uri=True, timeout=3, check_same_thread=False)
        self._conn.execute('PRAGMA key = "x\'' + (key + page[:16]).hex() + '\'"')
        del key
        self._conn.execute('PRAGMA temp_store=MEMORY')
        self._conn.execute('PRAGMA query_only=ON')
        self._conn.execute('PRAGMA trusted_schema=OFF')

    def _verify(self):
        self._process.verify()
        path = _contact_db(self._process, self.home)
        with path.open('rb') as file:
            status, salt = os.fstat(file.fileno()), file.read(16)
        if path != self._db or (status.st_dev, status.st_ino, salt) != self._identity:
            raise LinuxContactsError('群成员数据库已更换')

    def members(self, room):
        validate_group_id(room)
        with self._lock:
            if self._closed:
                raise RuntimeError('群成员读取所属的微信连接已结束')
            if time.monotonic() < self._retry_after:
                return None
            try:
                if self._conn is None:
                    self._open()
                self._verify()
                self._conn.execute('BEGIN')
                try:
                    result = read_group_snapshot(self._conn, room, self.account)
                finally:
                    self._conn.rollback()
                self._verify()
                return result
            except Exception:
                self._discard()
                self._retry_after = time.monotonic() + 15
                # SQL errors can include key-bearing pragma text. Expose only
                # an unavailable snapshot and never an old cached member list.
                return None

    def close(self):
        with self._lock:
            self._closed = True
            self._discard()
