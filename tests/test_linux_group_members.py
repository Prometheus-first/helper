import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from wxbot_framework.linux_group_members import LinuxGroupReader, read_group_snapshot

ROOM, SELF, PEER = '123@chatroom', 'wxid_self_fixture', 'wxid_peer_fixture'


def varint(value):
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    return bytes(out + bytes([value]))


def field(number, value):
    value = value.encode() if isinstance(value, str) else value
    return varint(number * 8 + 2) + varint(len(value)) + value


def extension(members):
    return b''.join(field(1, field(1, identifier) + field(2, name)) for identifier, name in members)


def seed(conn):
    conn.execute('CREATE TABLE contact (id INTEGER PRIMARY KEY,username TEXT,nick_name TEXT,remark TEXT,delete_flag INTEGER,is_in_chat_room INTEGER)')
    conn.execute('CREATE TABLE chat_room (id INTEGER PRIMARY KEY,username TEXT,owner TEXT,ext_buffer BLOB)')
    conn.execute('CREATE TABLE chatroom_member (room_id INTEGER,member_id INTEGER)')
    conn.executemany('INSERT INTO contact VALUES (?,?,?,?,?,?)',
        [(1,ROOM,'测试群','',0,1),(2,SELF,'自己','忽略备注',0,0),(3,PEER,'微信昵称','私人备注',0,0)])
    conn.execute('INSERT INTO chat_room VALUES (?,?,?,?)',
        (1,ROOM,SELF,extension([(SELF,''),(PEER,'群昵称')])) )
    conn.executemany('INSERT INTO chatroom_member VALUES (?,?)',[(1,2),(1,3)])
    conn.commit()


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        seed(self.conn)

    def snapshot(self):
        return read_group_snapshot(self.conn, ROOM, SELF)

    def test_complete_snapshot_names_and_verified_owner(self):
        rows = {row['wxid']:row for row in self.snapshot()}
        self.assertEqual(rows[PEER]['nickname'], '群昵称')
        self.assertEqual(rows[PEER]['wechat_name'], '微信昵称')
        self.assertNotIn('私人备注', str(rows))
        self.assertEqual(rows[SELF]['role'], 'owner')
        self.assertEqual(rows[PEER]['role'], 'unknown')
        self.assertIsNone(rows[PEER]['is_admin'])

    def test_missing_room_or_self_is_not_empty_membership(self):
        self.assertIsNone(read_group_snapshot(self.conn,'456@chatroom',SELF))
        self.conn.execute('UPDATE chat_room SET ext_buffer=?',(extension([(PEER,'')]),))
        self.conn.execute('DELETE FROM chatroom_member WHERE member_id=2')
        self.assertIsNone(self.snapshot())

    def test_admin_bit_owner_precedence_and_other_flags(self):
        for flags, expected in ((17, 'admin'), (25, 'admin'), (8209, 'admin'),
                                (1, 'member'), (9, 'member'), (8193, 'member'), (0, 'member')):
            with self.subTest(flags=flags):
                raw = field(1, field(1, SELF) + varint(24) + varint(17))
                raw += field(1, field(1, PEER) + varint(24) + varint(flags))
                self.conn.execute('UPDATE chat_room SET ext_buffer=?', (raw,))
                rows = {row['wxid']: row for row in self.snapshot()}
                self.assertEqual(rows[SELF]['role'], 'owner')
                self.assertFalse(rows[SELF]['is_admin'])
                self.assertEqual(rows[PEER]['role'], expected)
                self.assertEqual(rows[PEER]['is_admin'], expected == 'admin')
                self.assertEqual(rows[PEER]['role_source'], 'wechat_database')

    def test_missing_duplicate_wrong_wire_or_overflow_flags_remain_unknown(self):
        for flag in (b'', field(3, b'17'), varint(24) + varint(1 << 32),
                     varint(24) + varint(17) + varint(24) + varint(1)):
            raw = field(1, field(1, SELF)) + field(1, field(1, PEER) + flag)
            self.conn.execute('UPDATE chat_room SET ext_buffer=?', (raw,))
            peer = next(row for row in self.snapshot() if row['wxid'] == PEER)
            self.assertEqual(peer['role'], 'unknown')
            self.assertIsNone(peer['is_admin'])

    def test_index_disagreement_duplicate_or_unresolved_member_is_unavailable(self):
        for sql in ('DELETE FROM chatroom_member WHERE member_id=3',
                    'INSERT INTO chatroom_member VALUES (1,3)',
                    'UPDATE chatroom_member SET member_id=99 WHERE member_id=3'):
            with self.subTest(sql=sql):
                self.conn.execute('SAVEPOINT test')
                self.conn.execute(sql)
                self.assertIsNone(self.snapshot())
                self.conn.execute('ROLLBACK TO test')
                self.conn.execute('RELEASE test')

    def test_bad_extensions_never_return_partial_members(self):
        valid = extension([(SELF,''),(PEER,'')])
        for raw in (b'',None,b'\x80'*20,valid[:-1],valid+field(1,field(1,PEER)),
                    field(1,field(1,SELF)+field(1,PEER)),
                    extension([(SELF,''),('filehelper','')]),b'x'*(1024*1024+1)):
            with self.subTest(size=len(raw) if raw else 0):
                self.conn.execute('UPDATE chat_room SET ext_buffer=?',(raw,))
                self.assertIsNone(self.snapshot())

    def test_deleted_or_left_group_is_unavailable(self):
        for column in ('delete_flag','is_in_chat_room'):
            self.conn.execute('UPDATE contact SET delete_flag=0,is_in_chat_room=1 WHERE id=1')
            self.conn.execute('UPDATE contact SET '+column+'=? WHERE id=1', (1 if column=='delete_flag' else 0,))
            self.assertIsNone(self.snapshot())

    def test_invalid_name_falls_back_without_changing_identity(self):
        self.conn.execute('UPDATE chat_room SET ext_buffer=?', (extension([(SELF,''),(PEER,'坏\n名')]),))
        peer = next(row for row in self.snapshot() if row['wxid']==PEER)
        self.assertEqual(peer['nickname'],'微信昵称')

    def test_transaction_snapshot_is_not_mixed_with_new_membership(self):
        self.conn.execute('BEGIN')
        first = self.snapshot()
        self.assertEqual(len(first),2)
        self.conn.rollback()
        self.conn.execute('UPDATE chat_room SET ext_buffer=?',(extension([(SELF,'')]),))
        # The independent index still has PEER; never expose this partial update.
        self.assertIsNone(self.snapshot())


class ReaderTests(unittest.TestCase):
    def test_encrypted_read_only_connection_is_reused_but_not_member_results(self):
        import sqlcipher3
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'contact.db'
            key = bytes(range(32))
            conn = sqlcipher3.connect(str(path))
            conn.execute('PRAGMA key = "x\'' + key.hex() + '\'"')
            seed(conn)
            fake = Mock()
            fake.__enter__ = Mock(return_value=fake)
            fake.__exit__ = Mock()
            reader = LinuxGroupReader(os.getpid(),expected_account=SELF,expected_sha256='fixture',instance_home=folder)
            self.addCleanup(reader.close)
            self.addCleanup(conn.close)
            with patch('wxbot_framework.linux_group_members.VerifiedLinuxProcess',return_value=fake), \
                 patch('wxbot_framework.linux_group_members._contact_db',return_value=path), \
                 patch('wxbot_framework.linux_group_members._database_key',return_value=key) as extract:
                self.assertEqual(len(reader.members(ROOM)),2)
                conn.execute('UPDATE chat_room SET ext_buffer=?',(extension([(SELF,''),(PEER,'新群昵称')]),))
                conn.commit()
                self.assertIn('新群昵称',str(reader.members(ROOM)))
                self.assertEqual(extract.call_count,1)
                with self.assertRaises(sqlcipher3.DatabaseError):
                    reader._conn.execute('DELETE FROM chatroom_member')
                fake.verify.side_effect=RuntimeError('account changed')
                self.assertIsNone(reader.members(ROOM))
                self.assertIsNone(reader._conn)
                self.assertIsNone(reader.members(ROOM))
                self.assertEqual(extract.call_count,1)
            reader.close()
            with self.assertRaises(RuntimeError):reader.members(ROOM)

    def test_failed_open_never_exposes_secret_error_or_retries_each_room(self):
        reader=LinuxGroupReader(123,expected_account=SELF,expected_sha256='fixture',instance_home='/tmp')
        with patch.object(reader,'_open',side_effect=RuntimeError('PRIVATE_KEY')) as opening:
            self.assertIsNone(reader.members(ROOM))
            self.assertIsNone(reader.members('456@chatroom'))
            self.assertEqual(opening.call_count,1)
        reader.close()


if __name__ == '__main__':unittest.main()
