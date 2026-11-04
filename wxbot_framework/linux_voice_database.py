"""只读当前微信账号的已验证 media_N.db；不导出数据库、密钥或音频文件。"""
from __future__ import annotations

import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from urllib.parse import quote

from .account import validate_account
from .linux_contacts import VerifiedLinuxProcess
from .linux_contact_database import _database_key, _PAGE_SIZE
from .recall import message_id
from .text import validate_target

MAX_SHARDS = 32
MAX_VOICE_BYTES = 262144
_SHARD = re.compile(r'media_(0|[1-9][0-9]{0,5})\.db\Z')


class _BudgetProcess:
    """让现有内存密钥扫描在本次查询预算或 close 时提前中断。"""
    def __init__(self, process, check):
        self.proc, self._process, self._check = process.proc, process, check

    def read(self, address, size):
        self._check()
        return self._process.read(address, size)


def _voice_rows(conn, chat, identifier):
    # SQLite INTEGER 是 signed int64；协议 message id 是 uint64。
    number = int(identifier)
    if number >= 1 << 63:
        number -= 1 << 64
    return list(conn.execute('''SELECT CASE
        WHEN typeof(voice_data)='blob' AND length(voice_data) BETWEEN 13 AND ?
             AND typeof(data_index)='text' AND data_index='0'
        THEN voice_data ELSE NULL END
        FROM VoiceInfo
        WHERE chat_name_id=(SELECT rowid FROM Name2Id WHERE user_name=?) AND svr_id=?
        LIMIT 2''', (MAX_VOICE_BYTES, chat, number)))


class LinuxVoiceDatabaseReader:
    """复用只读连接，每次查询重新开事务；结果不缓存，最多读取 32 个分片。

    至少一个 media_N.db 必须由目标微信进程打开，用它证明账号目录。
    同目录的全部规范编号分片均参与唯一性核对；任一分片未就绪则返回 None。
    """

    def __init__(self, pid, *, expected_account, expected_sha256, instance_home, scan_timeout=3):
        if type(pid) is not int or pid <= 0 or not isinstance(expected_sha256, str) or not re.fullmatch('[0-9a-fA-F]{64}', expected_sha256):
            raise ValueError('语音数据库进程或版本指纹无效')
        if type(scan_timeout) not in (int, float) or not math.isfinite(scan_timeout) or not 0 < scan_timeout <= 10:
            raise ValueError('语音数据库查询预算无效')
        self.pid, self.account, self.sha256 = pid, validate_account(expected_account), expected_sha256
        self.home = Path(instance_home).expanduser().absolute()
        self.scan_timeout = scan_timeout
        self._lock, self._stop = threading.RLock(), threading.Event()
        self._process, self._connections = None, {}

    def _check(self, deadline):
        if self._stop.is_set() or time.monotonic() >= deadline:
            raise RuntimeError('语音数据库读取已取消或超时')

    def _file(self, path):
        if self.home.resolve(strict=True) != self.home or path.resolve(strict=True) != path:
            raise ValueError('语音数据库路径不受支持')
        parts = path.relative_to(self.home).parts
        if (len(parts) != 5 or parts[0] != 'xwechat_files' or
                not re.fullmatch(re.escape(self.account) + r'(?:_[0-9a-f]{4,16})?', parts[1]) or
                parts[2:4] != ('db_storage', 'message') or not _SHARD.fullmatch(parts[4])):
            raise ValueError('语音数据库不属于当前账号目录')
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1 or
                info.st_size < _PAGE_SIZE):
            raise ValueError('语音数据库文件身份无效')
        # SQLite 会自行打开旁边的 WAL/SHM；它们也必须留在已验证目录。
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(path) + suffix)
            try:
                extra = sidecar.lstat()
            except FileNotFoundError:
                continue
            if (sidecar.resolve(strict=True) != sidecar or not stat.S_ISREG(extra.st_mode) or
                    extra.st_uid != os.geteuid() or extra.st_nlink != 1):
                raise ValueError('语音数据库附属文件身份无效')
        return info

    def _discover(self, deadline):
        directories = set()
        with os.scandir(self._process.proc / 'fd') as entries:
            for count, entry in enumerate(entries):
                self._check(deadline)
                if count >= 4096:
                    raise ValueError('微信打开文件数量超过读取预算')
                try:
                    path = Path(os.readlink(entry.path))
                    if not _SHARD.fullmatch(path.name):
                        continue
                    disk = self._file(path)
                    opened = os.stat(entry.path)
                    if (disk.st_dev, disk.st_ino) != (opened.st_dev, opened.st_ino):
                        continue
                    directories.add(path.parent)
                except (OSError, ValueError):
                    continue
        if len(directories) != 1:
            raise ValueError('当前账号语音数据库目录未就绪或不唯一')
        directory = next(iter(directories))
        shards = []
        with os.scandir(directory) as entries:
            for count, entry in enumerate(entries):
                self._check(deadline)
                if count >= 4096:
                    raise ValueError('语音数据库目录超过读取预算')
                if not _SHARD.fullmatch(entry.name):
                    continue
                path = Path(entry.path)
                self._file(path)
                shards.append(path)
                if len(shards) > MAX_SHARDS:
                    raise ValueError('语音数据库分片超过读取预算')
        if not shards:
            raise ValueError('语音数据库尚未就绪')
        return tuple(sorted(shards, key=lambda p: int(_SHARD.fullmatch(p.name)[1])))

    def _page(self, path):
        original = self._file(path)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, 'rb') as source:
            info = os.fstat(source.fileno())
            if (info.st_dev, info.st_ino) != (original.st_dev, original.st_ino):
                raise ValueError('语音数据库身份已变化')
            page = source.read(_PAGE_SIZE)
        current = self._file(path)
        if len(page) != _PAGE_SIZE or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError('语音数据库身份已变化')
        return page, (info.st_dev, info.st_ino, page[:16])

    @staticmethod
    def _schema(conn):
        tables = dict(conn.execute("SELECT name,type FROM sqlite_schema WHERE name IN ('VoiceInfo','Name2Id') LIMIT 3"))
        if tables != {'VoiceInfo': 'table', 'Name2Id': 'table'}:
            raise ValueError('语音数据库表结构不可用')
        for name, required in (('VoiceInfo', {'chat_name_id': 'INTEGER', 'create_time': 'INTEGER',
                'local_id': 'INTEGER', 'svr_id': 'INTEGER', 'voice_data': 'BLOB', 'data_index': 'TEXT'}),
                ('Name2Id', {'user_name': 'TEXT'})):
            rows = conn.execute('PRAGMA table_info(' + name + ')').fetchmany(33)
            fields = {row[1]: str(row[2]).upper() for row in rows}
            if len(rows) > 32 or any(fields.get(key) != value for key, value in required.items()):
                raise ValueError('语音数据库字段不可用')

    def _connection(self, path, deadline):
        page, identity = self._page(path)
        current = self._connections.get(path)
        if current is not None:
            if current[1] != identity:
                raise ValueError('语音数据库已更换')
            return current[0]
        import sqlcipher3
        key = _database_key(_BudgetProcess(self._process, lambda: self._check(deadline)), page)
        self._check(deadline)
        self._process.verify()
        conn = None
        try:
            conn = sqlcipher3.connect('file:' + quote(str(path), safe='/') + '?mode=ro',
                uri=True, timeout=.1, check_same_thread=False)
            conn.execute('PRAGMA key = "x\'' + (key + page[:16]).hex() + '\'"')
            del key
            conn.execute('PRAGMA temp_store=MEMORY')
            conn.execute('PRAGMA query_only=ON')
            conn.execute('PRAGMA trusted_schema=OFF')
            conn.execute('PRAGMA cache_size=-2048')
            conn.execute('PRAGMA mmap_size=0')
            self._progress(conn, deadline)
            self._schema(conn)
            if self._page(path)[1] != identity:
                raise ValueError('语音数据库已更换')
            self._connections[path] = (conn, identity)
            return conn
        except BaseException:
            if conn is not None:
                conn.close()
            raise

    def _progress(self, conn, deadline):
        steps = 0
        def cancelled():
            nonlocal steps
            steps += 1000
            return self._stop.is_set() or time.monotonic() >= deadline or steps > 2_000_000
        conn.set_progress_handler(cancelled, 1000)

    def _discard(self):
        for conn, _ in self._connections.values():
            try:
                conn.close()
            except Exception:
                pass
        self._connections.clear()
        if self._process is not None:
            self._process.close()
            self._process = None

    def lookup(self, chat, nativeid):
        validate_target(chat)
        identifier = message_id(nativeid)
        if not identifier:
            raise ValueError('语音原始消息编号无效')
        deadline = time.monotonic() + self.scan_timeout
        with self._lock:
            if self._stop.is_set():
                return None
            try:
                if self._process is None:
                    self._process = VerifiedLinuxProcess(self.pid, self.account, self.sha256).__enter__()
                self._check(deadline)
                self._process.verify()
                paths = self._discover(deadline)
                if self._connections.keys() - set(paths):
                    raise ValueError('语音数据库分片已变化')
                matches = []
                for path in paths:
                    self._check(deadline)
                    conn = self._connection(path, deadline)
                    self._progress(conn, deadline)
                    conn.execute('BEGIN')
                    try:
                        matches.extend(_voice_rows(conn, chat, identifier))
                    finally:
                        conn.rollback()
                    if self._page(path)[1] != self._connections[path][1]:
                        raise ValueError('语音数据库已更换')
                    if len(matches) > 1:
                        return None
                self._process.verify()
                if self._discover(deadline) != paths:
                    raise ValueError('语音数据库分片已变化')
                self._check(deadline)
                body = matches[0][0] if len(matches) == 1 else None
                return body if isinstance(body, bytes) and 13 <= len(body) <= MAX_VOICE_BYTES else None
            except Exception:
                # SQLCipher 异常可能带 PRAGMA key 文本；不向上游暴露异常。
                self._discard()
                return None

    def close(self):
        self._stop.set()
        with self._lock:
            self._discard()
