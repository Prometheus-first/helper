"""Linux 标签缓存解析：只用合成账号和内存，不访问真实微信。"""
import struct
import unittest

from wxbot_framework.linux_contacts import LinuxContactsError, read_linux_labels

BASE = 0x100000000
ACCOUNT = 'wxid_self_fixture'


class Memory:
    def __init__(self):
        self.data = {}

    def put(self, address, data):
        self.data.update({address + i: b for i, b in enumerate(data)})

    def ptr(self, address, value):
        self.put(address, struct.pack('<Q', value))

    def cxx(self, address, text):
        text = text.encode('utf8')
        if len(text) > 22:
            raise ValueError('fixture string too long')
        self.put(address, bytes([len(text) * 2]) + text + bytes(23 - len(text)))

    def read(self, address, size):
        return bytes(self.data.get(address + i, 0) for i in range(size))


class LinuxLabelCacheTests(unittest.TestCase):
    def setUp(self):
        self.m = Memory()
        self.app, self.hub, self.center = 0x200000, 0x201000, 0x202000
        self.item, self.manager = 0x203000, 0x204000
        self.root, self.left, self.right = 0x205000, 0x206000, 0x207000
        self.key_ptr, self.key_string = 0x208000, 0x209000
        m = self.m
        m.ptr(BASE + 0xACF6710, self.app)
        m.ptr(self.app, BASE + 0xA89CBA8)
        m.ptr(self.app + 0x68, self.hub)
        m.ptr(self.hub, BASE + 0xA8AA750)
        m.put(self.hub + 0x38, b'\x01')
        m.cxx(self.hub + 0x48, ACCOUNT)
        m.ptr(self.hub + 0x28, self.center)
        m.ptr(self.center + 8, 2)
        m.ptr(self.center + 16, self.item)
        m.ptr(BASE + 0x61BB8E7 + 0x47C5AB9 + 8, self.key_ptr)
        key = b'a' * 32
        m.put(self.key_ptr, key + b'\x00')
        m.put(self.key_string, key)
        m.put(self.item + 16, b'\x01' + bytes(7) + struct.pack('<QQ', len(key), self.key_string))
        m.ptr(self.item + 0x28, self.manager)
        m.ptr(self.manager, BASE + 0xA981370)
        m.ptr(self.manager + 0x38, self.left)
        m.ptr(self.manager + 0x40, self.root)
        m.ptr(self.manager + 0x48, 3)
        self.node(self.root, self.left, self.right, self.manager + 0x40, 2, 20, '朋友')
        self.node(self.left, 0, 0, self.root, 1, 10, '测试')
        self.node(self.right, 0, 0, self.root, 3, 30, '同事')

    def node(self, address, left, right, parent, identifier, order, name):
        self.m.put(address, struct.pack('<QQQ', left, right, parent))
        self.m.put(address + 0x38, struct.pack('<II', identifier, order))
        self.m.cxx(address + 0x40, name)

    def read(self):
        return read_linux_labels(self.m.read, BASE, expected_account=ACCOUNT)

    def test_reads_ordered_labels_without_claiming_completeness(self):
        result = self.read()
        self.assertEqual([row['name'] for row in result['tags']], ['测试', '朋友', '同事'])
        self.assertEqual([row['id'] for row in result['tags']], ['1', '2', '3'])
        self.assertFalse(result['complete'])

    def test_wrong_account_is_rejected(self):
        with self.assertRaisesRegex(LinuxContactsError, '账号已变化'):
            read_linux_labels(self.m.read, BASE, expected_account='wxid_other_fixture')

    def test_loop_and_inconsistent_parent_are_rejected(self):
        self.m.ptr(self.left, self.root)
        with self.assertRaises(LinuxContactsError):
            self.read()

    def test_duplicate_label_id_is_rejected(self):
        self.m.put(self.right + 0x38, struct.pack('<II', 2, 30))
        with self.assertRaisesRegex(LinuxContactsError, '不完整'):
            self.read()

    def test_changing_cache_is_rejected(self):
        original = self.m.read
        calls = 0
        def changing(address, size):
            nonlocal calls
            if address == self.manager + 0x48:
                calls += 1
                if calls == 2:
                    return struct.pack('<Q', 4)
            return original(address, size)
        with self.assertRaisesRegex(LinuxContactsError, '变化'):
            read_linux_labels(changing, BASE, expected_account=ACCOUNT)

    def test_empty_cache_requires_consistent_sentinel(self):
        self.m.ptr(self.manager + 0x38, self.manager + 0x40)
        self.m.ptr(self.manager + 0x40, 0)
        self.m.ptr(self.manager + 0x48, 0)
        self.assertEqual(self.read()['tags'], [])
        self.m.ptr(self.manager + 0x38, self.root)
        with self.assertRaises(LinuxContactsError):
            self.read()


if __name__ == '__main__':
    unittest.main()
