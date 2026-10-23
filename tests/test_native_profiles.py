"""双版本指纹、只读账号发现、缓存快照和切号隔离；不连接微信。"""
from __future__ import annotations

import struct
import unittest

from test_account import AccountMemory, BASE, APP, CONTEXT, SELF
from test_contacts import ContactMemory, PEER, CONTACT
from test_group_members import GroupMemory, ROOM, MEMBER2
from test_emoticon_service import CacheMemory, SELF as STICKER_SELF, HEAP
from wxbot_framework.account import read_current_account, read_current_profile
from wxbot_framework.contacts import read_contact_name
from wxbot_framework.group_members import read_group_members
from wxbot_framework.emoticon_account import read_emoticon_cache_key
from wxbot_framework.native_profiles import CORE_PROFILES, profile_for_fingerprint, select_memory_profile

OLD, NEW = CORE_PROFILES


def updated_memory(ram):
    """将独立旧版测试夹具的核心地址/虚表引用迁移到新版，保留堆对象字段。"""
    values = {BASE + old: BASE + new for old, new in NEW.rvas.items()}

    def address(a):
        for old, new in NEW.rvas.items():
            if BASE + old <= a < BASE + old + 0x100:
                return a + new - old
        return a

    if hasattr(ram, "regions"):
        regions = {}
        for start, original in ram.regions.items():
            data = bytearray(original)
            for offset in range(0, len(data) - 7, 8):
                q = struct.unpack_from("<Q", data, offset)[0]
                if q in values:
                    struct.pack_into("<Q", data, offset, values[q])
            regions[address(start)] = data
        ram.regions = regions
    else:
        original = dict(ram.memory)
        for a in tuple(original):
            if a % 8 == 0 and all(a + i in original for i in range(8)):
                q = struct.unpack("<Q", bytes(original[a + i] for i in range(8)))[0]
                if q in values:
                    original.update({a + i: byte for i, byte in enumerate(struct.pack("<Q", values[q]))})
        ram.memory = {address(a): byte for a, byte in original.items()}
    return ram


class NativeProfileTests(unittest.TestCase):
    def test_only_exact_size_and_hash_pair_selects_a_known_build(self):
        for profile in CORE_PROFILES:
            self.assertIs(profile_for_fingerprint(profile.size, profile.sha256), profile)
            self.assertIs(profile_for_fingerprint(profile.size, profile.sha256.upper()), profile)
            self.assertIsNone(profile_for_fingerprint(profile.size + 1, profile.sha256))
            self.assertIsNone(profile_for_fingerprint(profile.size, "0" * 64))
        self.assertIsNone(profile_for_fingerprint(NEW.size, OLD.sha256))
        self.assertIsNone(profile_for_fingerprint(str(NEW.size), NEW.sha256))
        self.assertIsNone(profile_for_fingerprint(NEW.size, None))

    def test_profiles_are_immutable_and_unknown_offsets_are_not_guessed(self):
        with self.assertRaises(TypeError):
            NEW.rvas[0x1234] = 0x5678
        with self.assertRaises(KeyError):
            NEW.rva(0x1234)
        self.assertEqual(len(OLD.emoticon_cache_mask), 32)
        self.assertEqual(len(NEW.emoticon_cache_mask), 32)
        self.assertNotEqual(OLD.emoticon_cache_mask, NEW.emoticon_cache_mask)

    def test_both_versions_discover_identity_and_name_without_cached_binding(self):
        for upgrade, profile in ((lambda m: m, OLD), (updated_memory, NEW)):
            for account in (SELF, "old_username"):
                with self.subTest(version=profile.version, account=account):
                    ram = upgrade(AccountMemory(account))
                    raw = "合成测试名🌸".encode("utf-8")
                    name_heap = 0x501000000
                    ram.add(name_heap, 128)
                    ram.write(name_heap, raw)
                    ram.write(CONTEXT + 0x88, struct.pack("<QQQQ", name_heap, 0, len(raw), 127))
                    self.assertIs(select_memory_profile(ram.read, BASE), profile)
                    self.assertEqual(read_current_account(ram.read, BASE), account)
                    self.assertEqual(read_current_profile(ram.read, BASE), {"wxid": account, "name": "合成测试名🌸"})

    def test_unknown_vtables_or_getter_bytes_cannot_select_new_profile(self):
        for address, raw in ((APP, struct.pack("<Q", BASE + 0x1234)),
                             (BASE + NEW.rva(0x36F930), b"\0" * 5),
                             (BASE + NEW.rva(0x8DFE2F8) + 0x18, struct.pack("<Q", BASE + 0x1234))):
            ram = updated_memory(AccountMemory())
            ram.write(address, raw)
            self.assertIsNone(select_memory_profile(ram.read, BASE))
            self.assertIsNone(read_current_account(ram.read, BASE))

    def test_ambiguous_valid_profiles_are_rejected_instead_of_choosing_first(self):
        old = AccountMemory()
        new = updated_memory(AccountMemory())
        for regions, app, context in ((old.regions, APP, CONTEXT), (new.regions, APP + 0x1000, CONTEXT + 0x1000)):
            if app == APP:
                continue
            moved = {}
            for start, value in regions.items():
                data = bytearray(value)
                for offset in range(0, len(data) - 7, 8):
                    q = struct.unpack_from("<Q", data, offset)[0]
                    if q in (APP, CONTEXT):
                        struct.pack_into("<Q", data, offset, q + 0x1000)
                moved[start + 0x1000 if start in (APP, CONTEXT) else start] = data
            old.regions.update(moved)
        self.assertIsNone(select_memory_profile(old.read, BASE))
        self.assertIsNone(read_current_account(old.read, BASE))

    def test_new_version_rejects_account_or_context_changes_during_snapshot(self):
        for change_context in (False, True):
            ram = updated_memory(AccountMemory())
            reads = 0
            def changing(address, size):
                nonlocal reads
                if address == CONTEXT + 0x48 and size == 32:
                    reads += 1
                    if reads == 2:
                        if change_context:
                            ram.write(APP + 0x68, b"\0" * 8)
                        else:
                            ram.set_account("wxid_switched")
                return ram.read(address, size)
            self.assertIsNone(read_current_account(changing, BASE))

    def test_new_contact_cache_and_group_roles_keep_existing_layout(self):
        contact = updated_memory(ContactMemory("新的微信名🌸"))
        self.assertEqual(read_contact_name(contact.read, BASE, PEER, expected_account=SELF), "新的微信名🌸")
        group = updated_memory(GroupMemory())
        from test_group_members import RECORD, STRINGS
        group.s(RECORD + 0x28, SELF, STRINGS + 1800)
        group.write(RECORD + 0x90, struct.pack("<Qiii", STRINGS + 1900, 1, 1, 1))
        group.q(STRINGS + 1900, STRINGS + 1950)
        group.s(STRINGS + 1950, PEER, STRINGS + 2020)
        rows = {row["wxid"]: row for row in read_group_members(group.read, BASE, ROOM, expected_account=SELF)}
        self.assertEqual(rows[SELF]["role"], "owner")
        self.assertEqual(rows[PEER]["role"], "admin")
        self.assertEqual(rows[PEER]["group_nickname"], "群内称呼🌸")
        self.assertEqual(rows[PEER]["wechat_name"], "DefaultName")

    def test_new_contact_and_group_cache_reject_account_switch(self):
        for fixture, trigger, reader in (
            (ContactMemory, CONTACT + 0xD8, lambda m, r: read_contact_name(r, BASE, PEER, expected_account=SELF)),
            (GroupMemory, MEMBER2, lambda m, r: read_group_members(r, BASE, ROOM, expected_account=SELF)),
        ):
            ram = updated_memory(fixture())
            changed = False
            def changing(address, size):
                nonlocal changed
                value = ram.read(address, size)
                if address == trigger and not changed:
                    changed = True
                    ram.set_account("wxid_switched")
                return value
            self.assertIsNone(reader(ram, changing))

    def test_new_emoticon_key_uses_verified_account_profile_and_stable_key(self):
        raw = b"synthetic_key_012345"
        ram = updated_memory(CacheMemory(raw))
        ram.set_string(CONTEXT + 0x2D8,
                       bytes(value ^ NEW.emoticon_cache_mask[index & 31] for index, value in enumerate(raw)),
                       HEAP + 0x100)
        self.assertEqual(read_emoticon_cache_key(ram.read, BASE, expected_account=STICKER_SELF), raw[:16])
        changed = False
        def changing(address, size):
            nonlocal changed
            value = ram.read(address, size)
            if address == HEAP + 0x100 and not changed:
                changed = True
                ram.set_string(CONTEXT + 0x48, b"wxid_switched", HEAP)
            return value
        with self.assertRaises(RuntimeError):
            read_emoticon_cache_key(changing, BASE, expected_account=STICKER_SELF)


if __name__ == "__main__":
    unittest.main()
