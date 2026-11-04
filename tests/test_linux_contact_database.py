"""只使用合成 SQLite 和内存验证 Linux 联系人读取。"""
import os
import sqlite3
import struct
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from wxbot_framework.linux_contacts import LinuxContactsError, VerifiedLinuxProcess
from wxbot_framework.linux_contact_database import _CODEC_MASK, _database_key, _extra, read_snapshot


def vi(n):
    result = bytearray()
    while n > 127:
        result.append((n & 127) | 128)
        n >>= 7
    result.append(n)
    return bytes(result)


def blob_text(field, value):
    value = value.encode('utf8')
    return vi((field << 3) | 2) + vi(len(value)) + value


def blob_int(field, value):
    return vi(field << 3) + vi(value)


class ContactSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.execute('CREATE TABLE contact_label (label_id_ INTEGER, label_name_ TEXT, sort_order_ INTEGER)')
        self.conn.execute('CREATE TABLE contact (username TEXT, alias TEXT, nick_name TEXT, remark TEXT, description TEXT, extra_buffer BLOB, local_type INTEGER, flag INTEGER, delete_flag INTEGER)')
        self.conn.executemany('INSERT INTO contact_label VALUES (?, ?, ?)', [(7, '家人', 2), (8, '朋友', 1)])
        extra = (blob_int(2, 2) + blob_text(4, '签名') + blob_text(5, 'CN') +
                 blob_text(6, '省份') + blob_text(7, '城市') + blob_text(30, ',7,8,7,'))
        rows = [('wxid_friend_fixture', 'alias', '昵称', '备注', '描述', extra, 1, 3, 0),
                ('wxid_self_fixture', '', '自己', '', '', b'', 1, 1, 0),
                ('123@chatroom', '', '群', '', '', b'', 3, 0, 0),
                ('wxid_deleted_fixture', '', '已删', '', '', b'', 1, 1, 1),
                ('wxid_stranger_fixture', '', '陌生人', '', '', b'', 1, 0, 0)]
        self.conn.executemany('INSERT INTO contact VALUES (?,?,?,?,?,?,?,?,?)', rows)

    def test_filters_contacts_and_reads_profile_and_label_associations(self):
        result = read_snapshot(self.conn, 'wxid_self_fixture')
        self.assertFalse(result['complete'])
        self.assertEqual([x['name'] for x in result['tags']], ['朋友', '家人'])
        self.assertEqual(len(result['contacts']), 1)
        c = result['contacts'][0]
        self.assertEqual((c['wxid'], c['remark'], c['nickname']), ('wxid_friend_fixture', '备注', '昵称'))
        self.assertEqual((c['sex'], c['country'], c['province'], c['city'], c['signature']), ('female', 'CN', '省份', '城市', '签名'))
        self.assertEqual(c['tags'], ['家人', '朋友'])
        self.assertEqual(c['tag_ids'], ['7', '8'])
        self.assertEqual(c['tags_status'], 'ready')
        self.assertNotIn('extra_buffer', c)

    def test_empty_labels_are_not_reported_as_unavailable(self):
        item = _extra(blob_text(30, ''), {'7': '家人'})
        self.assertEqual(item['tag_ids'], [])
        self.assertEqual(item['tags_status'], 'ready')

    def test_unknown_label_and_invalid_or_truncated_protobuf_are_not_invented(self):
        self.assertEqual(_extra(blob_text(30, '999'), {'7': '家人'})['tags_status'], 'unavailable')
        for bad in (b'\xf2\x01\x02\x31', b'\x80' * 12, blob_text(30, '7,abc'), blob_text(30, '7') * 2):
            r = _extra(bad, {'7': '家人'})
            self.assertEqual(r['tags_status'], 'unavailable')
            self.assertEqual(r['tags'], [])

    def test_bad_and_duplicate_labels_rejected(self):
        self.conn.execute('INSERT INTO contact_label VALUES (7, ?, 4)', ('重复',))
        with self.assertRaises(LinuxContactsError):
            read_snapshot(self.conn, 'wxid_self_fixture')

    def test_oversized_string_is_rejected(self):
        self.conn.execute('UPDATE contact SET remark=? WHERE username=?', ('a' * 1025, 'wxid_friend_fixture'))
        with self.assertRaises(LinuxContactsError):
            read_snapshot(self.conn, 'wxid_self_fixture')


class KeyExtractionTests(unittest.TestCase):
    def test_finds_only_the_key_that_authenticates_the_target_page_header(self):
        key = bytes(range(32))
        iv = bytes(range(16))
        salt = b'SYNTHETIC-SALT!!'
        plain = b'\x10\x00\x02\x02\x50\x40\x20\x20' + bytes(8)
        encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
        page = bytearray(4096)
        page[:16] = salt
        page[16:32] = encryptor.update(plain) + encryptor.finalize()
        page[-80:-64] = iv
        salt_addr, reference, ctx, key_addr = 0x500100, 0x700100, 0x800000, 0x900000
        memory = {}
        def put(addr, value):
            memory.update({addr+i: b for i,b in enumerate(value)})
        put(reference - 56, struct.pack('<7I',32,16,16,4096,99,80,64))
        put(reference + 32, struct.pack('<Q', ctx))
        put(ctx + 8, struct.pack('<Q', key_addr))
        put(key_addr, bytes(a ^ b for a,b in zip(key, _CODEC_MASK)))
        class FakeProcess:
            def read(self, address, size):
                return bytes(memory.get(address+i, 0) for i in range(size))
        scans = [iter([(salt_addr, salt)]), iter([(reference, struct.pack('<Q', salt_addr))])]
        with patch('wxbot_framework.linux_contact_database._regions', return_value=[(0x1000, 0x2000)]), \
             patch('wxbot_framework.linux_contact_database._chunks', side_effect=scans):
            self.assertEqual(_database_key(FakeProcess(), bytes(page)), key)

    def test_wrong_process_fingerprint_rejected_before_memory_read(self):
        with self.assertRaisesRegex(LinuxContactsError, '版本不匹配'):
            with VerifiedLinuxProcess(os.getpid(), 'wxid_self_fixture', '0' * 64):
                pass


class ServiceSessionTests(unittest.TestCase):
    def test_contact_snapshot_tracks_current_session(self):
        from wxbot_framework.sdk import ServiceFacade
        class Backend:
            session_id = 'fixture-session'
            def contacts(self):
                return {'session_id': self.session_id, 'contacts': [], 'tags': []}
        backend = Backend()
        service = ServiceFacade()
        service.bind(backend)
        result = service.contacts(expected_session='fixture-session')
        self.assertEqual(result['session_id'], 'fixture-session')
        with self.assertRaisesRegex(RuntimeError, '连接已结束'):
            service.contacts(expected_session='old-session')

    def test_stale_result_is_rejected(self):
        from wxbot_framework.sdk import ServiceFacade
        class Backend:
            session_id = 'fixture-session'
            def contacts(self):
                self.session_id = 'replacement-session'
                return {'session_id': 'fixture-session', 'contacts': [], 'tags': []}
        service = ServiceFacade()
        service.bind(Backend())
        with self.assertRaisesRegex(RuntimeError, '连接已结束'):
            service.contacts(expected_session='fixture-session')


if __name__ == '__main__':
    unittest.main()
