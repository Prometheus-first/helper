"""普通好友内嵌 ContactExtData 的标签关联；仅使用合成内存。"""
import struct
import unittest

from test_account import BASE
from test_contact_directory import DirectoryMemory
from test_contacts import CONTACT

PROTO_VTABLE_RVA = 0x91798C8
LABEL_VALUE = 0x710000000
EMPTY_VALUE = 0x710001000


def tagged_memory(value="7", *, definitions=True, present=True):
    memory = DirectoryMemory()
    memory.regions[CONTACT].extend(b"\0" * (0x310 - len(memory.regions[CONTACT])))
    memory.write(CONTACT + 4, struct.pack("<I", 1))
    memory.q(CONTACT + 0x1E0, BASE + PROTO_VTABLE_RVA)
    memory.add(LABEL_VALUE, 0x400)
    memory.add(EMPTY_VALUE, 0x20)
    memory.s(LABEL_VALUE, value, LABEL_VALUE + 0x100)
    memory.s(EMPTY_VALUE, "", EMPTY_VALUE)
    for offset in (0x200, 0x208, 0x210, 0x218):
        memory.q(CONTACT + offset, EMPTY_VALUE)
    memory.q(CONTACT + 0x2A0, LABEL_VALUE)
    memory.write(CONTACT + 0x304, struct.pack("<I", 0x20000000 if present else 0))
    if definitions:
        memory.labels()
    return memory


class ContactLabelAssociationTests(unittest.TestCase):
    def test_matching_ids_resolve_to_this_accounts_tag_definition(self):
        row = tagged_memory().scan()["contacts"][0]
        self.assertEqual(row["tag_ids"], ["7"])
        self.assertEqual(row["tags"], ["演示标签"])
        self.assertEqual(row["tags_status"], "ready")

    def test_native_empty_segments_and_duplicates_do_not_create_tags(self):
        for value in ("7,", ",7,", ",,7,7,,"):
            with self.subTest(value=value):
                row = tagged_memory(value).scan()["contacts"][0]
                self.assertEqual(row["tag_ids"], ["7"])
                self.assertEqual(row["tags"], ["演示标签"])
                self.assertEqual(row["tags_status"], "ready")

    def test_explicitly_empty_membership_is_ready_without_a_definition_cache(self):
        for value in ("", ",", ",,"):
            with self.subTest(value=value):
                row = tagged_memory(value, definitions=False).scan()["contacts"][0]
                self.assertEqual(row["tag_ids"], [])
                self.assertEqual(row["tags"], [])
                self.assertEqual(row["tags_status"], "ready")

    def test_missing_presence_does_not_treat_a_string_as_membership(self):
        row = tagged_memory(present=False).scan()["contacts"][0]
        self.assertEqual(row["tag_ids"], [])
        self.assertEqual(row["tags"], [])
        self.assertEqual(row["tags_status"], "unavailable")

    def test_other_proto_or_record_type_never_reuses_normal_friend_offsets(self):
        for offset, data in ((0x1E0, struct.pack("<Q", BASE + PROTO_VTABLE_RVA + 8)),
                             (4, struct.pack("<I", 7))):
            with self.subTest(offset=offset):
                memory = tagged_memory()
                memory.write(CONTACT + offset, data)
                row = memory.scan()["contacts"][0]
                self.assertEqual(row["tag_ids"], [])
                self.assertEqual(row["tags_status"], "unavailable")

    def test_unknown_ids_are_retained_and_not_mislabeled_as_no_tags(self):
        row = tagged_memory("9").scan()["contacts"][0]
        self.assertEqual(row["tag_ids"], ["9"])
        self.assertEqual(row["tags"], [])
        self.assertEqual(row["tags_status"], "unavailable")

    def test_missing_definition_keeps_the_confirmed_ids_and_names(self):
        row = tagged_memory("7,9,").scan()["contacts"][0]
        self.assertEqual(row["tag_ids"], ["7", "9"])
        self.assertEqual(row["tags"], ["演示标签"])
        self.assertEqual(row["tags_status"], "unavailable")
        row = tagged_memory("7", definitions=False).scan()["contacts"][0]
        self.assertEqual(row["tag_ids"], ["7"])
        self.assertEqual(row["tags"], [])
        self.assertEqual(row["tags_status"], "unavailable")

    def test_membership_change_during_read_discards_the_snapshot(self):
        memory = tagged_memory()
        changed = False

        def changing(address, length):
            nonlocal changed
            data = memory.read(address, length)
            if address == LABEL_VALUE and not changed:
                changed = True
                memory.s(LABEL_VALUE, "8", LABEL_VALUE + 0x100)
            return data

        self.assertIsNone(memory.scan(changing))
        self.assertTrue(changed)


if __name__ == "__main__":
    unittest.main()
