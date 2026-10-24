"""好友扩展资料存在位、间接字符串及快照一致性，不连接微信。"""
import struct
import unittest

from test_contact_label_associations import tagged_memory
from test_contacts import CONTACT

VALUES = 0x720000000


def profile_memory():
    memory = tagged_memory('')
    memory.add(VALUES, 0x1000)
    memory.write(CONTACT + 0x304, struct.pack('<I', 0x2000007A))
    memory.write(CONTACT + 0x1F8, struct.pack('<I', 2))
    for index, (offset, text) in enumerate(((0x200, '这是一条签名'), (0x208, 'CN'),
                                           (0x210, '示例省份'), (0x218, '示例城市'))):
        address = VALUES + index * 0x100
        memory.q(CONTACT + offset, address)
        memory.s(address, text, address + 0x40)
    return memory


class ContactProfileTests(unittest.TestCase):
    def test_profile_strings_use_indirect_addresses_and_description_is_separate(self):
        row = profile_memory().scan()['contacts'][0]
        self.assertEqual((row['sex'], row['country'], row['province'], row['city']),
                         ('female', 'CN', '示例省份', '示例城市'))
        self.assertEqual(row['signature'], '这是一条签名')
        self.assertEqual(row['description'], '好友描述')

    def test_absent_fields_are_not_inferred_from_stale_pointers(self):
        memory = profile_memory()
        memory.write(CONTACT + 0x304, struct.pack('<I', 0x20000000))
        row = memory.scan()['contacts'][0]
        self.assertEqual(row['sex'], 'unknown')
        for key in ('country', 'province', 'city', 'signature'):
            self.assertEqual(row[key], '')

    def test_optional_bad_pointer_or_utf8_does_not_hide_valid_basic_details(self):
        for offset, data in ((CONTACT + 0x200, struct.pack('<Q', 0)),
                             (VALUES, b'\xff'.ljust(16, b'\0') + struct.pack('<QQ', 1, 15))):
            memory = profile_memory()
            memory.write(offset, data)
            row = memory.scan()['contacts'][0]
            self.assertEqual(row['signature'], '')
            self.assertEqual(row['country'], 'CN')
            self.assertEqual(row['nickname'], '示例昵称')

    def test_sex_values_are_explicit_and_unknown_stays_unknown(self):
        for value, expected in ((0, 'unknown'), (1, 'male'), (2, 'female'), (255, 'unknown')):
            memory = profile_memory()
            memory.write(CONTACT + 0x1F8, struct.pack('<I', value))
            self.assertEqual(memory.scan()['contacts'][0]['sex'], expected)

    def test_profile_change_before_return_discards_snapshot(self):
        memory = profile_memory()
        changed = False

        def changing(address, length):
            nonlocal changed
            data = memory.read(address, length)
            if address == VALUES + 0x40 and not changed:
                changed = True
                memory.s(VALUES, '变化后的签名', VALUES + 0x40)
            return data

        self.assertIsNone(memory.scan(changing))
        self.assertTrue(changed)

    def test_invalid_label_values_do_not_become_successful_empty_memberships(self):
        for value in ('text', '0', '-1', '4294967296', '7;9', '7, 9', '７'):
            with self.subTest(value=value):
                row = tagged_memory(value).scan()['contacts'][0]
                self.assertEqual(row['tag_ids'], [])
                self.assertEqual(row['tags_status'], 'unavailable')


if __name__ == '__main__':
    unittest.main()
