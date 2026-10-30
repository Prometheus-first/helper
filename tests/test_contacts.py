"""默认微信名读取和缓存变化验证，不连接微信。"""
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from test_account import AccountMemory, BASE, CONTEXT, SELF
from wxbot_framework.account import CONTEXT_VTABLE_RVA, IDENTITY_GETTER_RVA
from wxbot_framework.contacts import (CACHE_PROVIDER_GETTER_RVA, CACHE_PROVIDER_VTABLE_RVA,
                                     CONTACT_CACHE_VTABLE_RVA, read_contact_name)

PEER = 'wxid_contact_name'
PROVIDER, HEAD, NODE, CACHE, SENTINEL, BUCKETS, ENTRY, CONTACT, STRINGS = [
    0x600000000 + i * 0x1000 for i in range(9)]


class ContactMemory(AccountMemory):
    def __init__(self, name='DemoUser'):
        super().__init__()
        self.add(BASE + CONTEXT_VTABLE_RVA, 0x80)
        self.write(BASE + CONTEXT_VTABLE_RVA + 0x18, struct.pack('<Q', BASE + IDENTITY_GETTER_RVA))
        self.add(BASE + CACHE_PROVIDER_GETTER_RVA + 28, 8)
        self.write(BASE + CACHE_PROVIDER_GETTER_RVA + 28, bytes.fromhex('48 83 b9 60 04 00 00 00'))
        self.add(CONTEXT + 0x460, 8)
        self.write(CONTEXT + 0x460, struct.pack('<Q', PROVIDER))
        self.write(BASE + CONTEXT_VTABLE_RVA + 0x38, struct.pack('<Q', BASE + CACHE_PROVIDER_GETTER_RVA))
        for address, length in ((PROVIDER, 0xC0), (HEAD, 0x40), (NODE, 0x40), (CACHE, 0x200),
                                (SENTINEL, 0x40), (BUCKETS, 32), (ENTRY, 0x40), (CONTACT, 0x100), (STRINGS, 1024)):
            self.add(address, length)
        self.q(PROVIDER, BASE + CACHE_PROVIDER_VTABLE_RVA)
        self.q(PROVIDER + 0x90, HEAD)
        self.q(PROVIDER + 0x98, 1)
        self.q(HEAD, NODE)
        self.q(NODE, HEAD)
        self.q(NODE + 0x30, CACHE)
        self.q(CACHE, BASE + CONTACT_CACHE_VTABLE_RVA)
        self.q(CACHE + 0xD0, SENTINEL)
        self.q(CACHE + 0xD8, 1)
        self.q(CACHE + 0xE0, BUCKETS)
        self.q(CACHE + 0xF8, 1)
        hashed = 0xCBF29CE484222325
        for byte in PEER.encode():
            hashed = ((hashed ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
        for slot in (0, 16):
            self.write(BUCKETS + slot, struct.pack('<QQ', SENTINEL, SENTINEL))
        self.write(BUCKETS + (hashed & 1) * 16, struct.pack('<QQ', ENTRY, ENTRY))
        self.q(ENTRY + 0x30, CONTACT)
        self.s(ENTRY + 0x10, PEER, STRINGS)
        self.s(CONTACT + 8, PEER, STRINGS + 128)
        self.s(CONTACT + 0xD8, name, STRINGS + 256)

    def q(self, address, value):
        self.write(address, struct.pack('<Q', value))

    def s(self, address, value, buffer):
        body = value.encode('utf-8')
        if len(body) <= 15:
            self.write(address, body.ljust(16, b'\0') + struct.pack('<QQ', len(body), 15))
        else:
            self.write(buffer, body)
            self.write(address, struct.pack('<QQQQ', buffer, 0, len(body), 511))


class ContactTests(unittest.TestCase):
    def test_verified_id_returns_wechat_name_for_inline_and_unicode_heap_strings(self):
        for name in ('DemoUser', '微信原始名字🌸很长'):
            ram = ContactMemory(name)
            self.assertEqual(read_contact_name(ram.read, BASE, PEER, expected_account=SELF), name)

    def test_other_account_and_unknown_member_never_get_someone_elses_name(self):
        ram = ContactMemory()
        self.assertIsNone(read_contact_name(ram.read, BASE, PEER, expected_account='wxid_other'))
        self.assertIsNone(read_contact_name(ram.read, BASE, 'wxid_missing'))
        ram.s(CONTACT + 8, 'wxid_other', STRINGS + 128)
        self.assertIsNone(read_contact_name(ram.read, BASE, PEER))

    def test_wrong_layout_and_unavailable_cache_are_skipped(self):
        for address, value in ((PROVIDER, BASE + 1234), (CACHE, BASE + 5678),
                               (CACHE + 0xF8, 123), (CONTEXT + 0x460, 0)):
            ram = ContactMemory()
            ram.q(address, value)
            self.assertIsNone(read_contact_name(ram.read, BASE, PEER))

    def test_cache_changes_during_read_do_not_return_a_torn_name(self):
        ram = ContactMemory()
        changed = False
        def changing(address, size):
            nonlocal changed
            result = ram.read(address, size)
            if address == CONTACT + 0xD8 and not changed:
                changed = True
                ram.q(CACHE + 0xD8, 2)
            return result
        self.assertIsNone(read_contact_name(changing, BASE, PEER))
        self.assertEqual(read_contact_name(ram.read, BASE, PEER), 'DemoUser')

    def test_empty_and_control_character_names_are_rejected(self):
        for name in ('', '错误\n昵称', 'x' * 65):
            ram = ContactMemory(name)
            self.assertIsNone(read_contact_name(ram.read, BASE, PEER))


class GroupContactCacheTests(unittest.TestCase):
    def setUp(self):
        self.ram = ContactMemory('新群友')
        self.ram.q(CACHE + 0xD8, 0)  # 不在好友缓存中
        head, entry, sentinel, buckets, node = [0x800000000 + i * 0x1000 for i in range(5)]
        for address in (head, entry, sentinel, buckets, node):
            self.ram.add(address, 0x100)
        self.ram.write(CACHE + 0x108, struct.pack('<10Q', head, 1, 0, sentinel, 1,
            buckets, buckets + 32, buckets + 32, 1, 2))
        self.ram.write(head, struct.pack('<QQ', entry, entry))
        self.ram.write(entry, struct.pack('<QQ', head, head))
        self.ram.s(entry + 0x10, PEER, STRINGS + 384)
        self.ram.q(entry + 0x30, CONTACT)
        for offset in (0, 16):
            self.ram.write(buckets + offset, struct.pack('<QQ', sentinel, sentinel))
        hashed = 0xCBF29CE484222325
        for byte in PEER.encode('ascii'):
            hashed = ((hashed ^ byte) * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
        self.ram.write(buckets + (hashed & 1) * 16, struct.pack('<QQ', node, node))
        self.ram.s(node + 0x10, PEER, STRINGS + 512)
        self.ram.q(node + 0x30, entry)
        profile = SimpleNamespace(version='4.2.0.11', rva=lambda value: value,
                                  rvas={CONTACT_CACHE_VTABLE_RVA: CONTACT_CACHE_VTABLE_RVA})
        self.profile = patch('wxbot_framework.contacts.select_memory_profile', return_value=profile)
        self.profile.start()
        self.addCleanup(self.profile.stop)

    def test_nonfriend_name_is_read_by_verified_member_id(self):
        self.assertEqual(read_contact_name(self.ram.read, BASE, PEER, expected_account=SELF), '新群友')
        self.assertIsNone(read_contact_name(self.ram.read, BASE, 'wxid_missing', expected_account=SELF))
        self.assertIsNone(read_contact_name(self.ram.read, BASE, PEER, expected_account='wxid_other'))

    def test_changed_snapshot_and_mismatched_identity_are_rejected(self):
        changed = False
        def changing(address, length):
            nonlocal changed
            result = self.ram.read(address, length)
            if address == CONTACT + 0xD8 and not changed:
                changed = True
                self.ram.s(CONTACT + 0xD8, '其他名字', STRINGS + 256)
            return result
        self.assertIsNone(read_contact_name(changing, BASE, PEER, expected_account=SELF))
        self.ram.s(CONTACT + 8, 'wxid_wrong', STRINGS + 128)
        self.assertIsNone(read_contact_name(self.ram.read, BASE, PEER, expected_account=SELF))

    def test_unknown_version_never_reads_nonfriend_cache(self):
        with patch('wxbot_framework.contacts.select_memory_profile',
                   return_value=SimpleNamespace(version='unknown', rva=lambda value: value,
                                                rvas={CONTACT_CACHE_VTABLE_RVA: CONTACT_CACHE_VTABLE_RVA})):
            self.assertIsNone(read_contact_name(self.ram.read, BASE, PEER, expected_account=SELF))
