"""按账号保存联系人快照和已发现群，不保存连接令牌或消息正文。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading

from .account import validate_account
from .text import validate_target

_MAX_BYTES = 4 * 1024 * 1024
_CONTACT_FIELDS = {'wxid': 127, 'nickname': 256, 'alias': 256, 'remark': 1024,
                   'description': 2048, 'country': 4096, 'province': 4096,
                   'city': 4096, 'signature': 4096}


def _text(value, maximum):
    if not isinstance(value, str) or len(value) > maximum or '\0' in value:
        raise ValueError('本地目录字段无效')
    return value


def _timestamp(value):
    _text(value, 64)
    if datetime.fromisoformat(value).tzinfo is None:
        raise ValueError('本地目录时间无效')
    return value


def _contacts(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError('联系人快照无效')
    rows, tags = snapshot.get('contacts'), snapshot.get('tags')
    if not isinstance(rows, list) or len(rows) > 10000 or not isinstance(tags, list) or len(tags) > 4096:
        raise ValueError('联系人快照无效')
    clean, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('联系人记录无效')
        peer = validate_account(row.get('wxid'))
        if peer in seen:
            raise ValueError('联系人快照包含重复记录')
        seen.add(peer)
        item = {key: _text(row.get(key, ''), limit) for key, limit in _CONTACT_FIELDS.items()}
        item['sex'] = row.get('sex', 'unknown')
        item['tags_status'] = row.get('tags_status', 'unavailable')
        if item['sex'] not in ('unknown', 'male', 'female') or item['tags_status'] not in ('ready', 'unavailable'):
            raise ValueError('联系人资料状态无效')
        for key, maximum in (('tag_ids', 16), ('tags', 256)):
            values = row.get(key, [])
            if not isinstance(values, list) or len(values) > 4096:
                raise ValueError('联系人标签无效')
            item[key] = [_text(value, maximum) for value in values]
        clean.append(item)
    definitions, identifiers = [], set()
    for row in tags:
        if not isinstance(row, dict):
            raise ValueError('联系人标签目录无效')
        key, name = _text(row.get('id'), 16), _text(row.get('name'), 256)
        if not key or key in identifiers:
            raise ValueError('联系人标签目录包含重复记录')
        identifiers.add(key)
        definitions.append({'id': key, 'name': name})
    if type(snapshot.get('complete', False)) is not bool:
        raise ValueError('联系人快照状态无效')
    return {'contacts': clean, 'tags': definitions, 'complete': snapshot.get('complete', False),
            'scanned_at': _timestamp(snapshot['scanned_at'])}


def _groups(rows):
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError('群聊目录无效')
    clean = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('群聊记录无效')
        room = row.get('chat_id', '')
        validate_target(room)
        if not room.endswith('@chatroom'):
            continue
        clean[room] = {'chat_id': room, 'is_group': True, 'name': _text(row.get('name') or '', 512)}
    return list(clean.values())


class DirectoryCache:
    def __init__(self, workspace):
        self.root = Path(workspace).resolve() / '.runtime/directory_cache'
        self.lock = threading.RLock()

    def path(self, account, kind):
        validate_account(account)
        if kind not in ('contacts', 'groups'):
            raise ValueError('未知目录类型')
        key = hashlib.sha256(account.encode('utf-8')).hexdigest()[:32]
        return self.root / key / (kind + '.json')

    def _load(self, account, kind):
        path = self.path(account, kind)
        try:
            with path.open('rb') as source:
                raw = source.read(_MAX_BYTES + 1)
            if len(raw) > _MAX_BYTES:
                return None
            data = json.loads(raw)
            if (not isinstance(data, dict) or type(data.get('version')) is not int or
                    data['version'] != 1 or data.get('account') != account):
                return None
            return data
        except (OSError, ValueError, TypeError):
            return None

    def _save(self, account, kind, data):
        path = self.path(account, kind)
        raw = json.dumps({'version': 1, 'account': account, **data}, ensure_ascii=False, indent=2).encode('utf-8')
        if len(raw) > _MAX_BYTES:
            raise ValueError('本地目录超过文件大小限制')
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.directory-', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def save_contacts(self, account, snapshot):
        if not isinstance(snapshot, dict) or snapshot.get('account') != account:
            raise ValueError('联系人快照不属于当前账号')
        clean = _contacts(snapshot)
        with self.lock:
            self._save(account, 'contacts', {'snapshot': clean})

    def contacts(self, account):
        with self.lock:
            data = self._load(account, 'contacts')
            if data is None:
                return None
            try:
                snapshot = _contacts(data['snapshot'])
            except (ValueError, TypeError, KeyError):
                return None
            return dict(snapshot, account=account, source='local_directory_cache', cached=True, session_id='')

    def groups(self, account):
        with self.lock:
            data = self._load(account, 'groups')
            try:
                return _groups(data['groups']) if data is not None else []
            except (ValueError, TypeError, KeyError):
                return []

    def remember_groups(self, account, rows):
        incoming = _groups(rows)
        with self.lock:
            previous = self.groups(account)
            merged = {row['chat_id']: row for row in previous}
            for row in incoming:
                old = merged.get(row['chat_id'])
                if old and not row['name']:
                    row = dict(row, name=old['name'])
                merged[row['chat_id']] = row
            result = sorted(merged.values(), key=lambda row: row['chat_id'])
            if result != sorted(previous, key=lambda row: row['chat_id']):
                self._save(account, 'groups', {'groups': result})
            return deepcopy(result)
