"""仅使用合成 SQLite 数据库和模拟进程，不读取微信或进程密钥。"""
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wxbot_framework.linux_voice_database import LinuxVoiceDatabaseReader, _voice_rows

ACCOUNT, ROOM, OTHER = 'wxid_voice_db', '12345678@chatroom', 'wxid_other_friend'
BODY = b'\x02#!SILK_V3' + b'\x03\0abc' * 50
SCHEMA = '''CREATE TABLE VoiceInfo(chat_name_id INTEGER,create_time INTEGER,local_id INTEGER,
    svr_id INTEGER,voice_data BLOB,data_index TEXT DEFAULT '0');
    CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY);'''


@unittest.skipUnless(os.name == 'posix', 'Linux 文件身份语义')
class VoiceDatabaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'home'
        self.directory = self.home / 'xwechat_files' / (ACCOUNT + '_abcd') / 'db_storage/message'
        self.directory.mkdir(parents=True)
        self.proc = self.root / 'proc'
        (self.proc / 'fd').mkdir(parents=True)
        self.paths, self.processes, self.connects = [], [], []
        self.changed = False
        test = self
        class Process:
            def __init__(self, pid, account, fingerprint):
                test.assertEqual((pid, account, fingerprint), (1234, ACCOUNT, 'a' * 64))
                self.proc, self.closed = test.proc, False
                test.processes.append(self)
            def __enter__(self): return self
            def read(self, address, size): return b'\0' * size
            def verify(self):
                if test.changed:
                    raise RuntimeError('private-account-and-key')
            def close(self): self.closed = True
        def connect(database, **kwargs):
            self.connects.append(database)
            return sqlite3.connect(database, **kwargs)
        for name, replacement in (
            ('wxbot_framework.linux_voice_database.VerifiedLinuxProcess', Process),
            ('wxbot_framework.linux_voice_database._database_key', lambda process, page: b'k' * 32)):
            mocked = patch(name, replacement)
            mocked.start()
            self.addCleanup(mocked.stop)
        mocked = patch.dict(sys.modules, {'sqlcipher3': SimpleNamespace(connect=connect)})
        mocked.start()
        self.addCleanup(mocked.stop)

    def shard(self, index=0, rows=(), *, opened=True):
        path = self.directory / f'media_{index}.db'
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA)
        for chat, identifier, body, data_index in rows:
            conn.execute('INSERT OR IGNORE INTO Name2Id(user_name) VALUES (?)', (chat,))
            conn.execute('INSERT INTO VoiceInfo VALUES((SELECT rowid FROM Name2Id WHERE user_name=?),0,1,?,?,?)',
                         (chat, identifier, body, data_index))
        conn.commit()
        conn.close()
        self.paths.append(path)
        if opened:
            (self.proc / 'fd' / str(index + 10)).symlink_to(path)
        return path

    def reader(self, **kwargs):
        reader = LinuxVoiceDatabaseReader(1234, expected_account=ACCOUNT,
            expected_sha256='a' * 64, instance_home=self.home, **kwargs)
        self.addCleanup(reader.close)
        return reader

    def test_exact_chat_id_and_server_id_readonly_and_no_extra_files(self):
        self.shard(rows=[(ROOM, 81, BODY, '0'), (OTHER, 81, b'other-private-audio', '0')])
        reader = self.reader()
        original = set(self.root.rglob('*'))
        self.assertEqual(reader.lookup(ROOM, '81'), BODY)
        self.assertIsNone(reader.lookup(ROOM, '82'))
        self.assertIsNone(reader.lookup('999999@chatroom', '81'))
        self.assertEqual(len(self.connects), 1)
        self.assertTrue(self.connects[0].endswith('?mode=ro'))
        conn = next(iter(reader._connections.values()))[0]
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute('DELETE FROM VoiceInfo')
        reader.close()
        self.assertEqual(set(self.root.rglob('*')), original)
        self.assertTrue(self.processes[0].closed)
        self.assertIsNone(reader.lookup(ROOM, '81'))

    def test_uint64_id_is_queried_as_signed_sqlite_integer(self):
        self.shard(rows=[(ROOM, -1, BODY, '0')])
        self.assertEqual(self.reader().lookup(ROOM, str((1 << 64) - 1)), BODY)

    def test_all_numbered_siblings_are_checked_even_if_only_one_is_opened(self):
        self.shard()
        self.shard(7, [(ROOM, 81, BODY, '0')], opened=False)
        reader = self.reader()
        self.assertEqual(reader.lookup(ROOM, '81'), BODY)
        self.assertEqual(len(self.connects), 2)
        self.shard(9, [(ROOM, 81, BODY, '0')], opened=False)
        self.assertIsNone(reader.lookup(ROOM, '81'))

    def test_duplicate_or_indexed_or_oversize_or_nonblob_data_is_unavailable(self):
        rows = [(ROOM, 1, b'x' * 12, '0'), (ROOM, 2, b'x' * 262145, '0'),
                (ROOM, 3, BODY, '1'), (ROOM, 4, BODY, None), (ROOM, 5, 'private-text-fixture', '0'),
                (ROOM, 6, BODY, '0'), (ROOM, 6, b'x' * 262145, '1')]
        self.shard(rows=rows)
        reader = self.reader()
        for identifier in range(1, 7):
            with self.subTest(identifier=identifier):
                self.assertIsNone(reader.lookup(ROOM, str(identifier)))
        # CASE bounds data in SQL, so oversized payloads never reach Python.
        conn = next(iter(reader._connections.values()))[0]
        self.assertEqual(_voice_rows(conn, ROOM, '2'), [(None,)])

    def test_changed_account_or_process_never_returns_cached_audio(self):
        self.shard(rows=[(ROOM, 81, BODY, '0')])
        reader = self.reader()
        self.assertEqual(reader.lookup(ROOM, '81'), BODY)
        self.changed = True
        self.assertIsNone(reader.lookup(ROOM, '81'))
        self.assertTrue(self.processes[0].closed)
        self.assertEqual(reader._connections, {})

    def test_identity_is_verified_again_after_query(self):
        self.shard(rows=[(ROOM, 81, BODY, '0')])
        reader = self.reader()
        def changed(conn, chat, identifier):
            result = _voice_rows(conn, chat, identifier)
            self.changed = True
            return result
        with patch('wxbot_framework.linux_voice_database._voice_rows', side_effect=changed):
            self.assertIsNone(reader.lookup(ROOM, '81'))

    def test_replaced_database_inode_invalidates_connection(self):
        path = self.shard(rows=[(ROOM, 81, BODY, '0')])
        reader = self.reader()
        self.assertEqual(reader.lookup(ROOM, '81'), BODY)
        path.rename(path.with_suffix('.old'))
        conn = sqlite3.connect(path)
        conn.executescript(SCHEMA)
        conn.close()
        self.assertIsNone(reader.lookup(ROOM, '81'))
        self.assertFalse(reader._connections)

    def test_no_open_anchor_wrong_account_and_symlink_or_hardlink_are_rejected(self):
        path = self.shard(rows=[(ROOM, 81, BODY, '0')], opened=False)
        reader = self.reader()
        self.assertIsNone(reader.lookup(ROOM, '81'))
        fd = self.proc / 'fd/10'
        fd.symlink_to(path)
        linked = self.directory / 'media_1.db'
        linked.symlink_to(path)
        self.assertIsNone(reader.lookup(ROOM, '81'))
        linked.unlink()
        os.link(path, linked)
        self.assertIsNone(reader.lookup(ROOM, '81'))
        linked.unlink()
        fd.unlink()
        foreign = self.home / 'xwechat_files/wxid_different_abcd/db_storage/message'
        foreign.mkdir(parents=True)
        path.rename(foreign / path.name)
        fd.symlink_to(foreign / path.name)
        self.assertIsNone(reader.lookup(ROOM, '81'))

    def test_home_symlink_and_excess_shards_are_rejected(self):
        self.shard(rows=[(ROOM, 81, BODY, '0')])
        alias = self.root / 'home-link'
        alias.symlink_to(self.home, target_is_directory=True)
        reader = LinuxVoiceDatabaseReader(1234, expected_account=ACCOUNT, expected_sha256='a' * 64,
                                          instance_home=alias)
        self.addCleanup(reader.close)
        self.assertIsNone(reader.lookup(ROOM, '81'))
        for index in range(1, 33):
            self.shard(index, opened=False)
        self.assertIsNone(self.reader().lookup(ROOM, '81'))

    def test_sqlite_sidecar_links_are_rejected(self):
        path = self.shard(rows=[(ROOM, 81, BODY, '0')])
        outside = self.root / 'private-fixture'
        outside.write_bytes(b'not a database journal')
        reader = self.reader()
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(path) + suffix)
            sidecar.symlink_to(outside)
            self.assertIsNone(reader.lookup(ROOM, '81'))
            sidecar.unlink()
        self.assertEqual(reader.lookup(ROOM, '81'), BODY)

    def test_failed_key_or_schema_does_not_expose_error_or_leave_open_connection(self):
        path = self.shard(rows=[(ROOM, 81, BODY, '0')])
        reader = self.reader()
        with patch('wxbot_framework.linux_voice_database._database_key', side_effect=RuntimeError('private-key')):
            self.assertIsNone(reader.lookup(ROOM, '81'))
        self.assertFalse(reader._connections)
        conn = sqlite3.connect(path)
        conn.execute('DROP TABLE VoiceInfo')
        conn.execute('CREATE VIEW VoiceInfo AS SELECT * FROM Name2Id')
        conn.commit()
        conn.close()
        self.assertIsNone(reader.lookup(ROOM, '81'))
        self.assertFalse(reader._connections)

    def test_query_progress_budget_cancels_expensive_sql(self):
        self.shard(rows=[(ROOM, 81, BODY, '0')])
        reader = self.reader(scan_timeout=.5)
        def expensive(conn, *_):
            return list(conn.execute('WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n'))
        start = time.monotonic()
        with patch('wxbot_framework.linux_voice_database._voice_rows', side_effect=expensive):
            self.assertIsNone(reader.lookup(ROOM, '81'))
        self.assertLess(time.monotonic() - start, 2)

    def test_close_cancels_inflight_key_reads_before_waiting_for_lock(self):
        self.shard(rows=[(ROOM, 81, BODY, '0')])
        reader = self.reader()
        entered, done = threading.Event(), threading.Event()
        results = []
        def key(process, page):
            entered.set()
            while True:
                process.read(0, 1)
                time.sleep(.001)
        with patch('wxbot_framework.linux_voice_database._database_key', side_effect=key):
            worker = threading.Thread(target=lambda: (results.append(reader.lookup(ROOM, '81')), done.set()), daemon=True)
            worker.start()
            self.assertTrue(entered.wait(2))
            reader.close()
            self.assertTrue(done.wait(2))
            worker.join(2)
        self.assertEqual(results, [None])
        self.assertTrue(self.processes[0].closed)


if __name__ == '__main__':
    unittest.main()
