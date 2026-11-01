"""Followed publishers from synthetic contact memory; no WeChat or network."""
from contextlib import AbstractContextManager
import json
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_account import BASE
from test_contacts import CONTACT, ENTRY, STRINGS
from test_contact_directory import DirectoryMemory
from wxbot_framework import official_accounts as reader
from wxbot_framework.official_account_sync import OfficialAccountSync

ACCOUNT = 'wxid_synthetic_publisher_owner'
OWNER = {'username':'gh_synthetic_followed', 'nickname':'合成公众号', 'alias':'synthetic_publication'}


class PublisherDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.memory = DirectoryMemory(updated=False)
        self.memory.s(ENTRY + 0x10, OWNER['username'], STRINGS)
        self.memory.s(CONTACT + 8, OWNER['username'], STRINGS + 128)
        self.memory.s(CONTACT + 0xD8, OWNER['nickname'], STRINGS + 256)
        self.memory.s(CONTACT + 0x28, OWNER['alias'], STRINGS + 400)
        self.platform = reader._WindowsPlatform()
        self.target = SimpleNamespace(core=BASE, profile=SimpleNamespace(rva=lambda value:value))

    def scan(self, read=None, accounts=None):
        with patch.object(self.platform, 'account', side_effect=accounts or [ACCOUNT, ACCOUNT]):
            return self.platform.publishers(SimpleNamespace(read=read or self.memory.read), self.target, ACCOUNT)

    def test_followed_only_public_fields_and_no_personal_details_are_read(self):
        reads = []
        def read(address, size):
            reads.append((address, size))
            return self.memory.read(address, size)
        self.assertEqual(self.scan(read), [OWNER])
        for field in (CONTACT + 0x78, CONTACT + 0x1A0, CONTACT + 0x1E0):
            self.assertFalse(any(address <= field < address + size for address, size in reads))

    def test_deleted_unfollowed_and_non_publisher_records_are_excluded(self):
        for deleted, flags in ((1,3), (0,0), (0,4)):
            with self.subTest(deleted=deleted, flags=flags):
                self.memory.write(CONTACT + 0x68, struct.pack('<II', deleted, flags))
                self.assertEqual(self.scan(), [])
        self.memory.write(CONTACT + 0x68, struct.pack('<II', 0,3))
        for name in ('wxid_synthetic_friend', 'filehelper', '123@chatroom', 'gh_bad-private-key!'):
            self.memory.s(ENTRY + 0x10, name, STRINGS)
            self.memory.s(CONTACT + 8, name, STRINGS + 128)
            self.assertEqual(self.scan(), [])

    def test_changed_account_or_follow_marker_discards_snapshot(self):
        with self.assertRaises(reader.OfficialAccountReadError):
            self.scan(accounts=[ACCOUNT, 'wxid_synthetic_other'])
        calls = 0
        def changed(address, size):
            nonlocal calls
            if address == CONTACT + 0x68:
                calls += 1
                if calls > 1:
                    return struct.pack('<II', 0,0)
            return self.memory.read(address, size)
        with self.assertRaises(ValueError):
            self.scan(changed)


class Guard(AbstractContextManager):
    def __init__(self, scenario):
        self.scenario = scenario
        self.platform = SimpleNamespace(publishers=scenario.publishers)
        self.memory, self.target = object(), object()
    def verify(self):
        if self.scenario.changed:
            raise reader.OfficialAccountReadError('合成账号会话变化', 409)
    def __exit__(self, *args):
        self.scenario.closed += 1


class PublisherSyncTests(unittest.TestCase):
    def setUp(self):
        self.changed, self.closed = False, 0
        self.publishers = Mock(return_value=[dict(OWNER, private_key='synthetic-never-expose')])
        self.factory = Mock(side_effect=lambda *args:Guard(self))
        self.transport = Mock()
        self.sync = OfficialAccountSync(guard_factory=self.factory, transport_factory=self.transport)

    def read(self, **kwargs):
        return self.sync.list_publishers(1234, ACCOUNT, **kwargs)

    def test_no_injection_no_article_read_and_only_projected_fields(self):
        result = self.read()
        self.assertEqual(result, {'publishers':[OWNER], 'source':'wechat_contacts', 'complete':False})
        self.factory.assert_called_once_with(1234, ACCOUNT, None)
        self.transport.assert_not_called()
        self.assertNotIn('synthetic-never-expose', json.dumps(result))
        self.assertEqual(self.closed, 1)

    def test_cancelled_or_changed_session_does_not_return_followers(self):
        with self.assertRaises(reader.OfficialAccountReadError):
            self.read(cancelled=lambda:True)
        self.publishers.assert_not_called()
        def change(*args):
            self.changed = True
            return [OWNER]
        self.publishers.side_effect = change
        with self.assertRaises(reader.OfficialAccountReadError):
            self.read()
        self.assertEqual(self.closed, 2)

    def test_invalid_duplicate_or_oversized_identity_results_fail_without_private_values(self):
        for rows in ([OWNER, OWNER], [dict(OWNER, username='wxid_private_friend')],
                     [dict(OWNER, nickname='x'*257)], [OWNER]*1001):
            self.publishers.return_value = rows
            with self.assertRaises(reader.OfficialAccountReadError) as error:
                self.read()
            self.assertNotIn('wxid_private_friend', str(error.exception))
        self.publishers.side_effect = OSError('synthetic-local-secret-path')
        with self.assertRaises(reader.OfficialAccountReadError) as error:
            self.read()
        self.assertNotIn('synthetic-local-secret-path', str(error.exception))


if __name__ == '__main__':
    unittest.main()
