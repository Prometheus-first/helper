"""Linux 联系人库只读快照；密钥仅存在进程内存，不写入文件或日志。"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import struct
import time
from urllib.parse import quote

from .account import validate_account
from .linux_contacts import LinuxContactsError, VerifiedLinuxProcess

# Mask observed in the exact ELF guarded by VerifiedLinuxProcess.  It is part
# of the executable's codec implementation, not an account-specific secret.
_CODEC_MASK = struct.pack('<QQQQ', 0x969cd51b2938ecb8, 0x7e43d7f9d854463f,
                          0x3c31d6caa681cc1a, 0xafd43832e7f5d07b)
_PAGE_SIZE = 4096
_MAX_SCAN = 2 * 1024 * 1024 * 1024
_MAX_ROWS = 10000
_MAX_BLOB = 65536
_MAX_RESPONSE = 3 * 1024 * 1024


def _varint(data, index):
    value = 0
    for step in range(10):
        if index >= len(data):
            raise ValueError('truncated varint')
        byte = data[index]
        index += 1
        if step == 9 and byte > 1:
            raise ValueError('oversized varint')
        value |= (byte & 127) << (7 * step)
        if byte < 128:
            return value, index
    raise ValueError('invalid varint')


def _extra(blob, labels):
    result = {'sex': 'unknown', 'country': '', 'province': '', 'city': '', 'signature': '',
              'tag_ids': [], 'tags': [], 'tags_status': 'unavailable'}
    if not isinstance(blob, bytes) or len(blob) > _MAX_BLOB:
        return result
    fields, index = {}, 0
    try:
        while index < len(blob):
            key, index = _varint(blob, index)
            number, wire = key >> 3, key & 7
            if not 0 < number < (1 << 29):
                raise ValueError('invalid field')
            if wire == 0:
                value, index = _varint(blob, index)
            elif wire in (1, 5):
                length = 8 if wire == 1 else 4
                if length > len(blob) - index:
                    raise ValueError('truncated fixed field')
                value, index = blob[index:index + length], index + length
            elif wire == 2:
                length, index = _varint(blob, index)
                if length > len(blob) - index:
                    raise ValueError('truncated string')
                value, index = blob[index:index + length], index + length
            else:
                raise ValueError('unsupported wire type')
            if number in (2, 4, 5, 6, 7, 30):
                if number in fields:
                    raise ValueError('duplicate field')
                fields[number] = (wire, value)
        if 2 in fields and fields[2][0] == 0:
            result['sex'] = {1: 'male', 2: 'female'}.get(fields[2][1], 'unknown')
        for number, name in ((4, 'signature'), (5, 'country'), (6, 'province'), (7, 'city')):
            if number in fields:
                wire, value = fields[number]
                if wire != 2 or len(value) > 4096:
                    raise ValueError('invalid profile field')
                result[name] = value.decode('utf8')
        if 30 in fields:
            wire, value = fields[30]
            if wire != 2 or len(value) > 65536:
                raise ValueError('invalid label field')
            parts = [part for part in value.decode('ascii').split(',') if part]
            if len(parts) > 4096 or any(not part.isdecimal() or len(part) > 10 or not 0 < int(part) <= 0xffffffff for part in parts):
                raise ValueError('invalid label id')
            ids = list(dict.fromkeys(str(int(part)) for part in parts))
            result['tag_ids'] = ids
            result['tags'] = [labels[x] for x in ids if x in labels]
            result['tags_status'] = 'ready' if all(x in labels for x in ids) else 'unavailable'
    except (ValueError, UnicodeError, OverflowError):
        return {'sex': 'unknown', 'country': '', 'province': '', 'city': '', 'signature': '',
                'tag_ids': [], 'tags': [], 'tags_status': 'unavailable'}
    return result


def read_snapshot(conn, expected_account):
    """在已经开始的只读事务中查询，不暴露数据库扩展字段原文。"""
    labels = []
    for identifier, name, order in conn.execute(
            'SELECT label_id_,label_name_,sort_order_ FROM contact_label ORDER BY sort_order_,label_id_ LIMIT 4097'):
        if len(labels) >= 4096 or type(identifier) is not int or not 0 < identifier <= 0xffffffff or not isinstance(name, str) or not name or len(name) > 512:
            raise LinuxContactsError('好友标签数据无效或超过读取上限')
        labels.append({'id': str(identifier), 'name': name})
    names = {row['id']: row['name'] for row in labels}
    if len(names) != len(labels):
        raise LinuxContactsError('好友标签数据存在重复')
    rows = conn.execute("""SELECT username,alias,nick_name,remark,description,
        CASE WHEN length(extra_buffer)<=65536 THEN extra_buffer ELSE NULL END
        FROM contact WHERE local_type=1 AND (flag & 1)!=0 AND delete_flag=0 AND username<>?
        LIMIT 10001""", (expected_account,))
    contacts = []
    for username, alias, nickname, remark, description, extra in rows:
        if len(contacts) >= _MAX_ROWS:
            raise LinuxContactsError('联系人数量超过读取上限')
        try:
            validate_account(username)
        except (ValueError, TypeError):
            continue
        values = []
        for value, limit in ((alias, 256), (nickname, 256), (remark, 1024), (description, 2048)):
            if value is None:
                value = ''
            if not isinstance(value, str) or len(value) > limit or '\x00' in value:
                raise LinuxContactsError('联系人字段无效或过长')
            values.append(value)
        alias, nickname, remark, description = values
        contacts.append({'wxid': username, 'alias': alias, 'nickname': nickname, 'remark': remark,
                         'description': description, **_extra(extra, names)})
    contacts.sort(key=lambda row: (row['remark'] or row['nickname'] or row['wxid']).casefold())
    result = {'contacts': contacts, 'tags': labels, 'source': 'wechat_database', 'complete': False,
              'scanned_at': datetime.now(timezone.utc).isoformat()}
    if len(json.dumps(result, ensure_ascii=False).encode('utf8')) > _MAX_RESPONSE:
        raise LinuxContactsError('联系人数据超过单次响应上限')
    return result


def _contact_db(process, instance_home):
    home = Path(instance_home).resolve(strict=True)
    matches = {}
    for fd in (process.proc / 'fd').iterdir():
        try:
            path = Path(os.readlink(fd))
            if path.name != 'contact.db' or path.is_symlink():
                continue
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(home):
                continue
            disk, opened = resolved.stat(), fd.stat()
            if not stat.S_ISREG(disk.st_mode) or disk.st_uid != os.geteuid() or (disk.st_dev, disk.st_ino) != (opened.st_dev, opened.st_ino):
                continue
            matches[(disk.st_dev, disk.st_ino)] = resolved
        except (OSError, RuntimeError):
            continue
    if len(matches) != 1:
        raise LinuxContactsError('当前账号联系人库未就绪或身份不唯一')
    return next(iter(matches.values()))


def _regions(process):
    regions, total = [], 0
    for line in (process.proc / 'maps').read_text().splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) < 5 or fields[1] != 'rw-p':
            continue
        if len(fields) > 5 and fields[5] != '[heap]':
            continue
        start, end = (int(x, 16) for x in fields[0].split('-'))
        if end <= start or end - start >= 256 * 1024 * 1024:
            continue
        total += end - start
        if total > _MAX_SCAN:
            raise LinuxContactsError('微信内存范围超过联系人扫描上限')
        regions.append((start, end))
    return regions


def _chunks(process, regions, deadline):
    for start, end in regions:
        tail = b''
        while start < end:
            if time.monotonic() > deadline:
                raise LinuxContactsError('联系人扫描超时，请稍后重试')
            try:
                data = process.read(start, min(1024 * 1024, end - start))
            except OSError:
                break
            if not data:
                break
            yield start - len(tail), tail + data
            tail = data[-100:]
            start += len(data)


def _ptr(process, address):
    try:
        data = process.read(address, 8)
        return struct.unpack('<Q', data)[0] if len(data) == 8 else 0
    except (OSError, OverflowError, ValueError):
        return 0


def _database_key(process, page):
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise LinuxContactsError('缺少联系人读取依赖，请安装项目依赖') from None
    if len(page) != _PAGE_SIZE:
        raise LinuxContactsError('联系人库未就绪')
    salt, deadline = page[:16], time.monotonic() + 15
    regions = _regions(process)
    salts = set()
    for start, chunk in _chunks(process, regions, deadline):
        cursor = 0
        while (cursor := chunk.find(salt, cursor)) >= 0:
            salts.add(start + cursor)
            if len(salts) > 128:
                raise LinuxContactsError('联系人库缓存身份不唯一')
            cursor += len(salt)
    if not salts:
        raise LinuxContactsError('微信尚未加载联系人库，请稍后重试')
    references = set()
    for start, chunk in _chunks(process, regions, deadline):
        for address in salts:
            needle, cursor = struct.pack('<Q', address), 0
            while (cursor := chunk.find(needle, cursor)) >= 0:
                reference = start + cursor
                if reference % 8 == 0:
                    references.add(reference)
                if len(references) > 512:
                    raise LinuxContactsError('联系人库缓存身份不唯一')
                cursor += 8
    keys = set()
    for reference in references:
        try:
            raw = process.read(reference - 56, 28)
            if len(raw) != 28:
                continue
            params = struct.unpack('<7I', raw)
            if params[:4] != (32, 16, 16, _PAGE_SIZE) or params[5:7] != (80, 64):
                continue
            for offset in (32, 40):
                context = _ptr(process, reference + offset)
                pointer = _ptr(process, context + 8) if context else 0
                masked = process.read(pointer, 32) if pointer else b''
                if len(masked) != 32 or process.read(pointer, 32) != masked:
                    continue
                key = bytes(a ^ b for a, b in zip(masked, _CODEC_MASK))
                decryptor = Cipher(algorithms.AES(key), modes.CBC(page[-80:-64])).decryptor()
                header = decryptor.update(page[16:32])
                if header[:2] == b'\x10\x00' and header[2] in (1, 2) and header[3] in (1, 2) and header[5:8] == b'\x40\x20\x20':
                    keys.add(key)
        except (OSError, OverflowError, ValueError, struct.error):
            continue
    if len(keys) != 1:
        raise LinuxContactsError('联系人库读取准备未就绪，请稍后重试')
    return next(iter(keys))


def read_process_linux_contacts(pid, *, expected_account, expected_sha256, instance_home):
    """在当前受管微信内存中取得当前库的临时密钥，以只读事务读取好友。"""
    try:
        import sqlcipher3
    except ImportError:
        raise LinuxContactsError('缺少联系人读取依赖，请安装项目依赖') from None
    with VerifiedLinuxProcess(pid, expected_account, expected_sha256) as process:
        conn = None
        try:
            db = _contact_db(process, instance_home)
            with db.open('rb') as file:
                original_stat = os.fstat(file.fileno())
                page = file.read(_PAGE_SIZE)
            key = _database_key(process, page)
            process.verify()
            if (db.stat().st_dev, db.stat().st_ino) != (original_stat.st_dev, original_stat.st_ino):
                raise LinuxContactsError('联系人库已变化，请重新扫描')
            uri = 'file:' + quote(str(db), safe='/') + '?mode=ro'
            conn = sqlcipher3.connect(uri, uri=True, timeout=3)
            conn.execute('PRAGMA key = "x\'' + (key + page[:16]).hex() + '\'"')
            conn.execute('PRAGMA temp_store=MEMORY')
            conn.execute('PRAGMA query_only=ON')
            conn.execute('PRAGMA trusted_schema=OFF')
            conn.execute('BEGIN')
            result = read_snapshot(conn, expected_account)
            conn.rollback()
            process.verify()
            return result
        except LinuxContactsError:
            raise
        except Exception:
            # Database errors may include SQL text; never expose them or key material.
            raise LinuxContactsError('联系人库读取失败，请稍后重试') from None
        finally:
            if conn is not None:
                conn.close()
