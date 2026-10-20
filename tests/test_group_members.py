"""完整群成员缓存、群昵称及变化读验证；不连接微信。"""
import struct
import unittest
from unittest.mock import patch

from test_account import BASE, SELF
from test_contacts import ContactMemory, PEER, PROVIDER, HEAD, NODE
from wxbot_framework.group_members import (CHATROOM_CACHE_VTABLE_RVA, MEMBER_DATA_VTABLE_RVA,
                                          MEMBER_VTABLE_RVA, read_group_members, _hash)

ROOM = "1234567890@chatroom"
GNODE, CACHE, LHEAD, ENTRY, SENTINEL, BUCKETS, HASH_ENTRY, RECORD, ARRAY, MEMBER1, MEMBER2, STRINGS = [
    0x700000000 + i * 0x1000 for i in range(12)]


class GroupMemory(ContactMemory):
    def __init__(self, group_name="群内称呼🌸"):
        super().__init__(name="DefaultName")
        for address, length in ((GNODE, 0x40), (CACHE, 0xB0), (LHEAD, 0x40), (ENTRY, 0x40),
                                (SENTINEL, 0x40), (BUCKETS, 32), (HASH_ENTRY, 0x40),
                                (RECORD, 0x128), (ARRAY, 16), (MEMBER1, 0x30),
                                (MEMBER2, 0x30), (STRINGS, 0x2000)):
            self.add(address, length)
        self.q(PROVIDER + 0x98, 2)
        self.q(NODE, GNODE)
        self.q(GNODE, HEAD)
        self.q(GNODE + 0x30, CACHE)
        self.s(GNODE + 0x10, "ChatroomCache", STRINGS)
        self.q(CACHE, BASE + CHATROOM_CACHE_VTABLE_RVA)
        layout = (LHEAD, 1, 0, SENTINEL, 1, BUCKETS, BUCKETS + 32, BUCKETS + 32, 1, 2)
        self.write(CACHE + 0x38, struct.pack("<" + "Q" * 10, *layout))
        self.write(LHEAD, struct.pack("<QQ", ENTRY, ENTRY))
        self.write(ENTRY, struct.pack("<QQ", LHEAD, LHEAD))
        self.s(ENTRY + 0x10, ROOM, STRINGS + 128)
        self.q(ENTRY + 0x30, RECORD)
        for offset in (0, 16):
            self.write(BUCKETS + offset, struct.pack("<QQ", SENTINEL, SENTINEL))
        self.write(BUCKETS + (_hash(ROOM) & 1) * 16, struct.pack("<QQ", HASH_ENTRY, HASH_ENTRY))
        self.s(HASH_ENTRY + 0x10, ROOM, STRINGS + 256)
        self.q(HASH_ENTRY + 0x30, ENTRY)
        self.s(RECORD + 8, ROOM, STRINGS + 384)
        self.write(RECORD + 0x60, struct.pack("<QQiiQ", BASE + MEMBER_DATA_VTABLE_RVA, ARRAY, 2, 2, 2))
        self.q(RECORD + 0xE0, 2)
        self.write(ARRAY, struct.pack("<QQ", MEMBER1, MEMBER2))
        for index, (member, wxid, nickname) in enumerate(((MEMBER1, SELF, "自己"), (MEMBER2, PEER, group_name))):
            text = STRINGS + 512 + index * 512
            self.write(member, struct.pack("<QQQ", BASE + MEMBER_VTABLE_RVA, text, text + 64))
            self.s(text, wxid, text + 128)
            self.s(text + 64, nickname, text + 256)

    def snapshot(self, reader=None, **kwargs):
        return read_group_members(reader or self.read, BASE, ROOM, expected_account=kwargs.get("account", SELF))


class GroupMemberTests(unittest.TestCase):
    def test_complete_snapshot_prefers_real_group_name_to_wechat_name(self):
        ram = GroupMemory()
        rows = {row["wxid"]: row for row in ram.snapshot()}
        self.assertEqual(set(rows), {SELF, PEER})
        self.assertEqual(rows[PEER]["nickname"], "群内称呼🌸")
        self.assertEqual(rows[PEER]["wechat_name"], "DefaultName")
        self.assertEqual(rows[PEER]["group_nickname"], "群内称呼🌸")

    def test_missing_names_do_not_turn_a_complete_list_into_missing_members(self):
        ram = GroupMemory("")
        with patch("wxbot_framework.group_members.read_contact_name", return_value=None):
            rows = {row["wxid"]: row for row in ram.snapshot()}
        self.assertEqual(rows[PEER]["nickname"], "")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["wxid"]: row for row in ram.snapshot()}[PEER]["nickname"], "DefaultName")

    def test_repeated_field_capacity_uses_int32_and_ignores_padding(self):
        ram = GroupMemory()
        ram.write(RECORD + 0x7C, b"\xff\x23\x01\x00")
        self.assertEqual(len(ram.snapshot()), 2)

    def test_other_account_and_unknown_room_are_not_returned(self):
        ram = GroupMemory()
        self.assertIsNone(ram.snapshot(account="wxid_other"))
        self.assertIsNone(read_group_members(ram.read, BASE, "777@chatroom", expected_account=SELF))
        with self.assertRaises(ValueError):
            read_group_members(ram.read, BASE, PEER, expected_account=SELF)

    def test_incomplete_and_rebuilding_containers_are_rejected(self):
        for address, data in ((RECORD + 0x70, struct.pack("<ii", 0, 2)),
                              (RECORD + 0x70, struct.pack("<ii", 2001, 2001)),
                              (RECORD + 0xE0, struct.pack("<Q", 1)),
                              (CACHE + 0x58, struct.pack("<Q", 2)),
                              (CACHE + 0x78, struct.pack("<Q", 13)),
                              (MEMBER2, struct.pack("<Q", BASE + 0x1234)),
                              (ARRAY, struct.pack("<QQ", MEMBER2, MEMBER2))):
            with self.subTest(address=address, data=data):
                ram = GroupMemory()
                ram.write(address, data)
                self.assertIsNone(ram.snapshot())

    def test_duplicate_ids_or_a_list_without_self_are_not_complete(self):
        ram = GroupMemory()
        ram.s(STRINGS + 512, PEER, STRINGS + 640)
        self.assertIsNone(ram.snapshot())
        ram.s(STRINGS + 512, "wxid_unknown", STRINGS + 640)
        self.assertIsNone(ram.snapshot())

    def test_membership_and_account_changes_during_read_are_rejected(self):
        for mutation in (lambda ram: ram.q(RECORD + 0xE0, 1),
                         lambda ram: ram.set_account("wxid_switched")):
            ram = GroupMemory()
            changed = False
            def changing(address, length):
                nonlocal changed
                value = ram.read(address, length)
                if address == MEMBER2 and not changed:
                    changed = True
                    mutation(ram)
                return value
            self.assertIsNone(ram.snapshot(changing))

    def test_changed_member_pointer_array_with_same_count_is_rejected(self):
        ram = GroupMemory()
        changed = False
        def changing(address, length):
            nonlocal changed
            value = ram.read(address, length)
            if address == MEMBER2 and not changed:
                changed = True
                ram.write(ARRAY, struct.pack("<QQ", MEMBER2, MEMBER1))
            return value
        self.assertIsNone(ram.snapshot(changing))

    def test_strings_and_pointer_boundaries_are_checked(self):
        ram = GroupMemory()
        ram.write(STRINGS + 512 + 16, struct.pack("<QQ", 128, 127))
        self.assertIsNone(ram.snapshot())
        ram = GroupMemory()
        ram.q(RECORD + 0x68, 1)
        self.assertIsNone(ram.snapshot())


if __name__ == "__main__":
    unittest.main()
