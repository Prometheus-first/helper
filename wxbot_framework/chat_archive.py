"""将消息监控中的显示字段批量写入当前机器人的本地 SQLite。"""
from contextlib import closing, contextmanager
import json
import logging
import sqlite3
import threading
import time

from .panel_common import PanelError
from .recall import message_id


def native_key(row):
    return message_id(row.get('message_id')) or None, row.get('forwarded_index', 0)


def merge_replay(original, replay, *, same_row=False, migrating=False):
    # An unresolved recall can be replaced by the real message. A replay
    # otherwise keeps the original capture time and its known contents.
    replace = not replay.get('recall_missing') and (
        original.get('recall_missing') or same_row or
        (migrating and replay['timestamp'] < original['timestamp']))
    chosen = dict(replay if replace else original, id=original['id'])
    if original.get('recalled') or replay.get('recalled'):
        notice = original if original.get('recalled') else replay
        chosen.update(recalled=True, recalled_at=notice['recalled_at'], recall_notice=notice['recall_notice'])
    return chosen


def find_native(db, account, chat, identifier, part):
    return db.execute('''SELECT stream, id, capture_session, capture_sequence, payload FROM messages
        WHERE account=? AND chat_id=? AND native_id=? AND forwarded_part=?''',
        (account, chat, identifier, part)).fetchone()


class ChatArchive:
    def __init__(self, workspace, *, read_json, write_json, interval=15, batch_size=200):
        directory = workspace / '.runtime'
        self.path = directory / 'chat_archive.sqlite3'
        self.settings = directory / 'chat_archive.json'
        self.write_json = write_json
        self.enabled = set(read_json(self.settings).get('enabled_accounts', [])) if self.settings.exists() else set()
        self.interval, self.batch_size = interval, batch_size
        self.condition = threading.Condition()
        self.pending = {}
        self.thread = self.due = None
        self.version = self.saved = self.failures = 0
        self.immediate = self.closed = self.failed = False

    def status(self, account):
        with self.condition:
            return {'enabled': account in self.enabled, 'write_failed': self.failed}

    def configure(self, account, enabled):
        with self.condition:
            updated = self.enabled | {account} if enabled else self.enabled - {account}
            if updated != self.enabled:
                self.settings.parent.mkdir(parents=True, exist_ok=True)
                self.write_json(self.settings, {'enabled_accounts': sorted(updated)})
                self.enabled = updated

    def record(self, stream, row):
        with self.condition:
            if self.closed or row['account'] not in self.enabled or not row['chat_id']:
                return
            self.pending[stream, row['id']] = row
            self.version += 1
            first_pending = self.due is None
            if first_pending:
                self.due = time.monotonic() + self.interval
            batch_ready = len(self.pending) >= self.batch_size and not self.failed and not self.immediate
            if batch_ready:
                self.immediate = True
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name='ChatArchive', daemon=True)
                self.thread.start()
            if first_pending or batch_ready:
                self.condition.notify_all()

    def flush(self, timeout=6):
        with self.condition:
            target, failures = self.version, self.failures
            if self.saved < target:
                if self.pending:
                    self.immediate = True
                    self.condition.notify_all()
                self.condition.wait_for(lambda: self.saved >= target or self.failures != failures or self.closed, timeout)
            return self.saved >= target

    def close(self):
        with self.condition:
            self.closed = self.immediate = True
            self.condition.notify_all()
            thread = self.thread
        if thread is not None:
            thread.join(6)

    def _take_batch(self):
        with self.condition:
            while True:
                if not self.pending:
                    if self.closed:
                        return None
                    self.condition.wait()
                    continue
                wait = self.due - time.monotonic()
                if self.immediate or wait <= 0:
                    rows, version = self.pending, self.version
                    self.pending = {}
                    self.due = None
                    self.immediate = False
                    return rows, version
                self.condition.wait(wait)

    def _open_writer(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=2)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            self._ensure_schema(db)
            return db
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            db.close()
            raise

    @staticmethod
    def _ensure_schema(db):
        if db.execute('PRAGMA user_version').fetchone()[0] >= 1:
            return
        with db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('PRAGMA user_version').fetchone()[0] >= 1:
                return
            db.execute('''CREATE TABLE IF NOT EXISTS messages (
                stream TEXT NOT NULL, id INTEGER NOT NULL, account TEXT NOT NULL, chat_id TEXT NOT NULL,
                captured_at REAL NOT NULL, day TEXT NOT NULL, capture_session TEXT, capture_sequence INTEGER,
                payload TEXT NOT NULL, PRIMARY KEY (stream, id)
            ) WITHOUT ROWID''')
            db.execute('CREATE INDEX IF NOT EXISTS message_dates ON messages (account, chat_id, day, captured_at)')
            db.execute('''CREATE INDEX IF NOT EXISTS message_captures
                ON messages (account, chat_id, capture_session, capture_sequence) WHERE capture_session IS NOT NULL''')
            db.execute('ALTER TABLE messages ADD COLUMN native_id TEXT')
            db.execute('ALTER TABLE messages ADD COLUMN forwarded_part INTEGER NOT NULL DEFAULT 0')
            db.execute('''CREATE UNIQUE INDEX message_native_key
                ON messages (account, chat_id, native_id, forwarded_part) WHERE native_id IS NOT NULL''')
            db.execute('''CREATE TABLE message_capture_aliases (
                account TEXT NOT NULL, chat_id TEXT NOT NULL, session TEXT NOT NULL, sequence INTEGER NOT NULL,
                PRIMARY KEY (account, chat_id, session, sequence)
            ) WITHOUT ROWID''')
            cursor = ('', 0)
            while batch := db.execute('''SELECT stream, id, capture_session, capture_sequence, payload FROM messages
                WHERE (stream, id) > (?, ?) ORDER BY stream, id LIMIT 500''', cursor).fetchall():
                for stream, identifier, session, sequence, payload in batch:
                    row = json.loads(payload)
                    if native_key(row)[0] is not None:
                        ChatArchive._save_row(db, stream, identifier, row, (session, sequence), migrating=True)
                cursor = batch[-1][:2]
            db.execute('PRAGMA user_version=1')

    @staticmethod
    def _save_row(db, stream, identifier, row, capture, *, migrating=False):
        account, chat = row['account'], row['chat_id']
        native_id, part = native_key(row)
        old = find_native(db, account, chat, native_id, part) if native_id is not None else None
        if old:
            previous_stream, previous_id, session, sequence, payload = old
            previous_capture = (session, sequence)
            same_row = (stream, identifier) == (previous_stream, previous_id)
            if (row['is_group'] and capture[0] is not None and previous_capture[0] is not None
                    and previous_capture != capture):
                db.execute('INSERT OR IGNORE INTO message_capture_aliases VALUES (?, ?, ?, ?)',
                           (account, chat, *capture))
            if migrating and not same_row:
                db.execute('DELETE FROM messages WHERE stream=? AND id=?', (stream, identifier))
            original = json.loads(payload)
            row = merge_replay(original, row, same_row=same_row, migrating=migrating)
            capture = previous_capture if previous_capture[0] is not None else capture
            stream, identifier = previous_stream, previous_id
            if row == original and capture == previous_capture:
                return

        if native_id is not None and part and not row.get('recalled'):
            root = find_native(db, account, chat, native_id, 0)
            if root:
                row = merge_replay(row, json.loads(root[-1]))
        ChatArchive._upsert(db, stream, identifier, row, capture, native_id, part)

        if native_id is not None and not part and row.get('recalled'):
            children = db.execute('''SELECT stream, id, payload FROM messages
                WHERE account=? AND chat_id=? AND native_id=? AND forwarded_part>0''',
                (account, chat, native_id)).fetchall()
            for child_stream, child_id, child_payload in children:
                child = json.loads(child_payload)
                if not child.get('recalled'):
                    updated = merge_replay(child, row)
                    db.execute('UPDATE messages SET payload=? WHERE stream=? AND id=?',
                               (ChatArchive._payload(updated), child_stream, child_id))

    @staticmethod
    def _payload(row):
        return json.dumps(row, ensure_ascii=False, separators=(',', ':'))

    @staticmethod
    def _upsert(db, stream, identifier, row, capture, native_id, part):
        db.execute('''INSERT INTO messages
            (stream, id, account, chat_id, captured_at, day, capture_session, capture_sequence,
             payload, native_id, forwarded_part)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(stream, id) DO UPDATE SET captured_at=excluded.captured_at, day=excluded.day,
            capture_session=excluded.capture_session, capture_sequence=excluded.capture_sequence,
            payload=excluded.payload, native_id=excluded.native_id, forwarded_part=excluded.forwarded_part''',
            (stream, identifier, row['account'], row['chat_id'], row['timestamp'], row['date'],
             *capture, ChatArchive._payload(row), native_id, part))

    @staticmethod
    def _write(db, rows):
        with db:
            for (stream, identifier), row in rows.items():
                capture = row.get('_capture') or (None, None)
                public = {key: value for key, value in row.items() if key != '_capture'}
                ChatArchive._save_row(db, stream, identifier, public, capture)

    def _run(self):
        logger = logging.getLogger('ChatArchive')
        db = None
        try:
            while (batch := self._take_batch()) is not None:
                rows, version = batch
                try:
                    if db is None:
                        db = self._open_writer()
                    self._write(db, rows)
                except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
                    if db is not None:
                        db.close()
                        db = None
                    with self.condition:
                        if not self.failed or self.closed:
                            logger.warning('本地聊天归档写入失败，请检查磁盘空间和文件权限')
                        self.failed = True
                        self.failures += 1
                        self.pending = rows | self.pending
                        self.due = time.monotonic() + self.interval
                        self.immediate = False
                        self.condition.notify_all()
                        if self.closed:
                            return
                else:
                    with self.condition:
                        self.saved = version
                        self.failed = False
                        self.condition.notify_all()
        finally:
            if db is not None:
                db.close()

    @contextmanager
    def reader(self):
        if not self.path.is_file():
            yield None
            return
        try:
            with closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
                if db.execute('PRAGMA user_version').fetchone()[0] < 1:
                    with closing(self._open_writer()):
                        pass
                db.execute('BEGIN')
                yield db
        except (sqlite3.Error, OSError):
            raise PanelError('无法读取 Helper 的本地聊天归档', 503) from None
