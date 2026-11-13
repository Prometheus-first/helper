"""新版联系人基本资料与标签使用独立身份映射，不读取真实联系人。"""
import struct
from types import SimpleNamespace
import unittest

from test_account import BASE, SELF
from test_contacts import CONTACT, STRINGS, PEER
from test_contact_directory import LABEL_CACHE, LABEL_NODE
from test_contact_label_associations import LABEL_VALUE
from test_contact_profiles import profile_memory
from test_native_profiles import NEW, TEXT_42012, updated_memory
from wxbot_framework.contacts import read_contact_directory
from wxbot_framework.backend import NativeTextBackend


def current_directory_memory():
    memory = profile_memory()
    # Convert the independently defined 4.1.15.56 fixture, including its code
    # stubs and pointer identities. Heap field offsets remain unchanged.
    translations = {NEW.rva(key): TEXT_42012.rva(key) for key in NEW.rvas}
    translations.update({0x901F638: 0x9125D28, 0x91798C8: 0x9282FD8})
    return updated_memory(memory, SimpleNamespace(rvas=translations))


class ContactDirectory42012Tests(unittest.TestCase):
    def test_current_build_reads_friend_profile_remark_and_tag_names(self):
        memory = current_directory_memory()
        # Use nonempty association in addition to the profile fixture.
        memory.s(LABEL_VALUE, '7', LABEL_VALUE + 0x100)
        result = memory.scan()
        row = result['contacts'][0]
        self.assertEqual((row['wxid'], row['nickname'], row['remark'], row['alias']),
                         (PEER, '示例昵称', '测试备注', 'example_alias'))
        self.assertEqual((row['sex'], row['country'], row['province'], row['city']),
                         ('female', 'CN', '示例省份', '示例城市'))
        self.assertEqual(row['signature'], '这是一条签名')
        self.assertEqual(row['description'], '好友描述')
        self.assertEqual(row['tag_ids'], ['7'])
        self.assertEqual(row['tags'], ['演示标签'])
        self.assertEqual(row['tags_status'], 'ready')
        self.assertEqual(result['source'], 'wechat_cache')
        self.assertFalse(result['complete'])

    def test_old_extension_identity_is_not_accepted_for_current_build(self):
        memory = current_directory_memory()
        memory.q(CONTACT + 0x1E0, BASE + 0x91798C8)
        row = memory.scan()['contacts'][0]
        self.assertEqual(row['nickname'], '示例昵称')
        self.assertEqual(row['sex'], 'unknown')
        self.assertEqual(row['signature'], '')
        self.assertEqual(row['tags_status'], 'unavailable')

    def test_old_label_component_is_not_reused_by_current_build(self):
        memory = current_directory_memory()
        memory.s(LABEL_VALUE, '7', LABEL_VALUE + 0x100)
        memory.q(LABEL_CACHE, BASE + 0x901F638)
        row = memory.scan()['contacts'][0]
        self.assertEqual(row['tag_ids'], ['7'])
        self.assertEqual(row['tags'], [])
        self.assertEqual(row['tags_status'], 'unavailable')

    def test_corrupt_optional_label_cache_does_not_hide_basic_contacts(self):
        memory = current_directory_memory()
        memory.q(LABEL_NODE, LABEL_NODE)
        result = memory.scan()
        self.assertEqual(result['tags'], [])
        self.assertEqual(result['contacts'][0]['remark'], '测试备注')

    def test_profile_presence_still_controls_indirect_fields(self):
        memory = current_directory_memory()
        memory.write(CONTACT + 0x304, struct.pack('<I', 0))
        row = memory.scan()['contacts'][0]
        self.assertEqual(row['sex'], 'unknown')
        self.assertEqual(row['country'], '')
        self.assertEqual(row['signature'], '')
        self.assertEqual(row['tags_status'], 'unavailable')

    def test_labels_changing_during_snapshot_discard_the_entire_result(self):
        memory = current_directory_memory()
        changed = False
        def changing(address, length):
            nonlocal changed
            result = memory.read(address, length)
            if address == LABEL_NODE + 0x48 and not changed:
                changed = True
                memory.s(LABEL_NODE + 0x48, '改过的标签', STRINGS + 900)
            return result
        self.assertIsNone(read_contact_directory(changing, BASE, expected_account=SELF))
        self.assertTrue(changed)

    def test_other_account_cannot_read_current_directory(self):
        self.assertIsNone(current_directory_memory().scan(account='wxid_another_account'))

    def test_current_backend_advertises_contact_read_without_enabling_old_versions(self):
        backend = object.__new__(NativeTextBackend)
        backend.raw = SimpleNamespace(wechat_version='4.2.0.12')
        self.assertIn('contact.read', backend.capabilities)
        backend.raw.wechat_version = '4.2.0.11'
        self.assertNotIn('contact.read', backend.capabilities)


if __name__ == '__main__':
    unittest.main()
