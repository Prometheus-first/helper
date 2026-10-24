"""合成内存中的好友目录、快照边界与账户隔离，不连接微信。"""
import struct
import unittest
from unittest.mock import patch

from test_account import BASE, SELF, CONTEXT
from test_contacts import (ContactMemory, PEER, CONTACT, ENTRY, CACHE, SENTINEL,
                           BUCKETS, STRINGS)
from test_native_profiles import updated_memory
from wxbot_framework.contacts import (ContactDirectoryError, read_contact_directory,
                                     LABEL_CACHE_VTABLE_RVA)

COMPONENTS, COMPONENT_HEAD, COMPONENT_NODE, COMPONENT_BUCKETS, LABEL_CACHE, LABEL_HEAD, LABEL_NODE = (
    0x700000000 + i * 0x1000 for i in range(7))


class DirectoryMemory(ContactMemory):
    def __init__(self, *, updated=True):
        super().__init__('示例昵称')
        self.regions[CONTACT].extend(b'\0' * (0x1D8 - len(self.regions[CONTACT])))
        self.q(SENTINEL, ENTRY)
        self.q(SENTINEL + 8, ENTRY)
        self.q(ENTRY, SENTINEL)
        self.q(ENTRY + 8, SENTINEL)
        self.q(CACHE + 0xE8, BUCKETS + 32)
        self.q(CACHE + 0xF0, BUCKETS + 32)
        self.q(CACHE + 0x100, 2)
        self.write(CONTACT + 0x68, struct.pack('<II', 0, 3))
        self.s(CONTACT + 0x28, 'example_alias', STRINGS + 400)
        self.s(CONTACT + 0x78, '测试备注', STRINGS + 500)
        self.s(CONTACT + 0x1A0, '好友描述', STRINGS + 700)
        if updated:
            updated_memory(self)

    def scan(self, read=None, account=SELF):
        return read_contact_directory(read or self.read, BASE, expected_account=account)

    def labels(self):
        for address, size in ((COMPONENTS, 0x40), (COMPONENT_HEAD, 0x40), (COMPONENT_NODE, 0x48),
                              (COMPONENT_BUCKETS, 32), (LABEL_CACHE, 0x50), (LABEL_HEAD, 0x20),
                              (LABEL_NODE, 0x80)):
            self.add(address, size)
        self.q(CONTEXT + 0x28, COMPONENTS)
        self.write(COMPONENTS + 8, struct.pack('<QQQQQQQ', COMPONENT_HEAD, 1, COMPONENT_BUCKETS,
                                             COMPONENT_BUCKETS + 32, COMPONENT_BUCKETS + 32, 1, 2))
        self.q(COMPONENT_HEAD, COMPONENT_NODE)
        self.q(COMPONENT_HEAD + 8, COMPONENT_NODE)
        self.q(COMPONENT_NODE, COMPONENT_HEAD)
        self.q(COMPONENT_NODE + 8, COMPONENT_HEAD)
        self.q(COMPONENT_NODE + 0x30, LABEL_CACHE)
        self.q(LABEL_CACHE, BASE + LABEL_CACHE_VTABLE_RVA)
        self.write(LABEL_CACHE + 0x40, struct.pack('<QQ', LABEL_HEAD, 1))
        for offset in (0, 8, 16):
            self.q(LABEL_HEAD + offset, LABEL_NODE)
        self.write(LABEL_HEAD + 0x19, b'\x01')
        for offset in (0, 8, 16):
            self.q(LABEL_NODE + offset, LABEL_HEAD)
        self.s(LABEL_NODE + 0x20, '7', STRINGS + 800)
        self.write(LABEL_NODE + 0x40, struct.pack('<II', 7, 0))
        self.s(LABEL_NODE + 0x48, '演示标签', STRINGS + 900)


class ContactDirectoryTests(unittest.TestCase):
    def test_current_friend_details_are_explicit_and_do_not_include_raw_metadata(self):
        result = DirectoryMemory().scan()
        self.assertEqual(len(result['contacts']), 1)
        row = result['contacts'][0]
        self.assertEqual((row['wxid'], row['nickname'], row['alias'], row['remark'], row['description']),
                         (PEER, '示例昵称', 'example_alias', '测试备注', '好友描述'))
        self.assertEqual(row['sex'], 'unknown')
        self.assertEqual(row['tags_status'], 'unavailable')
        self.assertEqual(row['tags'], [])
        self.assertEqual(result['source'], 'wechat_cache')
        self.assertFalse(result['complete'])
        self.assertNotIn('extra_buffer', row)
        self.assertNotIn('encrypt_username', row)
        self.assertNotIn('big_head_url', row)

    def test_deleted_and_stranger_records_are_not_current_friends(self):
        for deleted, flags in ((1, 3), (0, 4), (0, 0)):
            memory = DirectoryMemory()
            memory.write(CONTACT + 0x68, struct.pack('<II', deleted, flags))
            self.assertEqual(memory.scan()['contacts'], [])

    def test_room_official_system_and_self_are_not_friends(self):
        for wxid in ('12345678@chatroom', 'gh_example', 'filehelper', SELF):
            memory = DirectoryMemory()
            memory.s(ENTRY + 0x10, wxid, STRINGS)
            memory.s(CONTACT + 8, wxid, STRINGS + 128)
            self.assertEqual(memory.scan()['contacts'], [])

    def test_empty_fields_are_valid_and_visible_text_is_sanitized(self):
        memory = DirectoryMemory()
        memory.s(CONTACT + 0x78, '', STRINGS + 500)
        memory.s(CONTACT + 0xD8, '昵称\x01\x7f', STRINGS + 256)
        row = memory.scan()['contacts'][0]
        self.assertEqual(row['remark'], '')
        self.assertEqual(row['nickname'], '昵称')

    def test_cache_identity_and_container_corruption_discard_the_whole_snapshot(self):
        mutations = ((CONTACT + 8, b'other_id'.ljust(16, b'\0') + struct.pack('<QQ', 8, 15)),
                     (ENTRY, struct.pack('<Q', ENTRY)),
                     (SENTINEL + 8, struct.pack('<Q', SENTINEL)),
                     (CACHE + 0x100, struct.pack('<Q', 3)),
                     (CACHE + 0xE8, struct.pack('<Q', BUCKETS + 16)))
        for address, value in mutations:
            with self.subTest(address=address):
                memory = DirectoryMemory()
                memory.write(address, value)
                self.assertIsNone(memory.scan())

    def test_more_nodes_than_declared_is_not_a_partial_success(self):
        memory = DirectoryMemory()
        memory.q(CACHE + 0xD8, 0)
        self.assertIsNone(memory.scan())

    def test_empty_stable_cache_is_valid(self):
        memory = DirectoryMemory()
        memory.q(CACHE + 0xD8, 0)
        memory.q(SENTINEL, SENTINEL)
        memory.q(SENTINEL + 8, SENTINEL)
        self.assertEqual(memory.scan()['contacts'], [])

    def test_bad_utf8_is_not_exposed_as_success(self):
        memory = DirectoryMemory()
        memory.write(CONTACT + 0xD8, b'\xff'.ljust(16, b'\0') + struct.pack('<QQ', 1, 15))
        self.assertIsNone(memory.scan())

    def test_other_account_cannot_read_a_directory(self):
        self.assertIsNone(DirectoryMemory().scan(account='wxid_another_account'))

    def test_mutation_after_read_before_return_is_rejected(self):
        memory = DirectoryMemory()
        changed = False
        def changing(address, length):
            nonlocal changed
            value = memory.read(address, length)
            if address == CONTACT + 0x78 and not changed:
                changed = True
                memory.s(CONTACT + 0x78, '变动备注', STRINGS + 500)
            return value
        self.assertIsNone(memory.scan(changing))

    def test_unknown_or_unverified_directory_profile_never_guesses_layout(self):
        with self.assertRaisesRegex(ContactDirectoryError, '4.1.15.56'):
            DirectoryMemory(updated=False).scan()
        with patch('wxbot_framework.contacts.select_memory_profile', return_value=None):
            self.assertIsNone(DirectoryMemory().scan())

    def test_read_and_response_budgets_are_enforced(self):
        memory = DirectoryMemory()
        memory.q(CACHE + 0xD8, 10001)
        with self.assertRaises(ContactDirectoryError):
            memory.scan()
        with patch('wxbot_framework.contacts.MAX_DIRECTORY_BYTES', 100):
            with self.assertRaises(ContactDirectoryError):
                DirectoryMemory().scan()

    def test_tag_definition_tree_does_not_imply_a_friends_tag_membership(self):
        memory = DirectoryMemory()
        memory.labels()
        result = memory.scan()
        self.assertEqual(result['tags'], [{'id': '7', 'name': '演示标签'}])
        self.assertEqual(result['contacts'][0]['tags_status'], 'unavailable')
        self.assertEqual(result['contacts'][0]['tags'], [])

    def test_corrupt_tag_tree_is_unavailable_but_does_not_hide_friends(self):
        for address, value in ((LABEL_NODE, struct.pack('<Q', LABEL_NODE)),
                               (LABEL_NODE + 8, struct.pack('<Q', LABEL_NODE)),
                               (LABEL_HEAD + 0x19, b'\0'),
                               (LABEL_NODE + 0x40, struct.pack('<I', 8))):
            memory = DirectoryMemory()
            memory.labels()
            memory.write(address, value)
            result = memory.scan()
            self.assertEqual(len(result['contacts']), 1)
            self.assertEqual(result['tags'], [])

    def test_foreign_or_duplicate_label_components_are_not_used(self):
        memory = DirectoryMemory()
        memory.labels()
        memory.q(LABEL_CACHE, BASE + 1000)
        self.assertEqual(memory.scan()['tags'], [])

    def test_label_mutation_before_snapshot_return_is_rejected(self):
        memory = DirectoryMemory()
        memory.labels()
        changed = False
        def changing(address, length):
            nonlocal changed
            result = memory.read(address, length)
            if address == LABEL_NODE + 0x48 and not changed:
                changed = True
                memory.s(LABEL_NODE + 0x48, '更新标签', STRINGS + 900)
            return result
        self.assertIsNone(memory.scan(changing))


if __name__ == '__main__':
    unittest.main()
