"""Linux 导出使用合成账号/密钥/图片，不访问真实微信。"""
from io import BytesIO
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image
from test_emoticons import encrypted, KEY
from wxbot_framework.emoticons import EmoticonSessionExpired
from wxbot_framework.linux_emoticons import export_linux_emoticons, _cache_key

ACCOUNT = 'wxid_export_fixture'
IDENTIFIER = '1234567890abcdef1234567890abcdef'


@unittest.skipUnless(os.name == 'posix', 'Linux private directories')
class LinuxEmoticonExportTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.home = self.root / 'home'
        self.profile = self.home / 'xwechat_files' / (ACCOUNT + '_abcd')
        self.database = self.profile / 'db_storage/contact/contact.db'
        self.database.parent.mkdir(parents=True)
        self.database.touch()
        self.source = self.profile / 'business/emoticon/Persist/12'
        self.source.mkdir(parents=True)
        raw = BytesIO()
        Image.new('RGB', (4, 3), 'red').save(raw, format='GIF')
        self.gif = raw.getvalue()
        (self.source / IDENTIFIER).write_bytes(encrypted(self.gif))
        self.process = MagicMock()
        self.process.__enter__.return_value = self.process
        self.process_patch = patch('wxbot_framework.linux_emoticons.VerifiedLinuxProcess', return_value=self.process)
        self.process_patch.start()
        self.addCleanup(self.process_patch.stop)
        p = patch('wxbot_framework.linux_emoticons._contact_db', return_value=self.database)
        p.start(); self.addCleanup(p.stop)
        p = patch('wxbot_framework.linux_emoticons._cache_key', return_value=KEY)
        p.start(); self.addCleanup(p.stop)

    def export(self, **options):
        return export_linux_emoticons(123, expected_account=ACCOUNT, expected_sha256='a' * 64,
            instance_home=self.home, output_directory=options.pop('output_directory', self.root / 'export'), **options)

    def test_real_shared_decrypt_and_private_original_gif(self):
        rows = self.export()
        self.assertEqual(len(rows), 1)
        item = rows[0]
        self.assertTrue(item.sendable)
        self.assertEqual(item.gif_path.read_bytes(), self.gif)
        self.assertEqual(item.error, '')
        for path in (item.gif_path.parent, item.gif_path.parent.parent, item.gif_path.parent.parent.parent):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
        self.assertEqual(item.gif_path.stat().st_mode & 0o777, 0o600)

    def test_no_cache_is_empty_collection(self):
        (self.source / IDENTIFIER).unlink()
        self.assertEqual(self.export(convert=False), ())

    def test_wrong_account_profile_is_rejected_and_run_cleaned(self):
        wrong = self.home / 'xwechat_files/wxid_other_abcd/db_storage/contact/contact.db'
        wrong.parent.mkdir(parents=True); wrong.touch()
        with patch('wxbot_framework.linux_emoticons._contact_db', return_value=wrong):
            with self.assertRaises(ValueError): self.export()
        self.assertEqual(list((self.root / 'export').iterdir()), [])

    def test_session_change_cleans_partial_exports(self):
        active = True
        def changed():
            nonlocal active
            old, active = active, False
            return old
        with self.assertRaises(EmoticonSessionExpired): self.export(is_current=changed)
        self.assertEqual(list((self.root / 'export').iterdir()), [])

    def test_link_hardlink_and_embedded_output_are_rejected(self):
        with self.assertRaises(ValueError): self.export(output_directory=self.home / 'exports')
        real = self.root / 'real'; real.mkdir(mode=0o700)
        link = self.root / 'link'; link.symlink_to(real, target_is_directory=True)
        with self.assertRaises(ValueError): self.export(output_directory=link)
        os.link(self.source / IDENTIFIER, self.root / 'hardlink')
        with self.assertRaises(ValueError): self.export()

    def test_wrong_key_reports_fixed_item_failure_without_payload(self):
        with patch('wxbot_framework.linux_emoticons._cache_key', return_value=b'0' * 16):
            rows = self.export()
        self.assertFalse(rows[0].sendable)
        self.assertIsNone(rows[0].original_path)
        self.assertNotIn(repr(KEY), repr(rows))


class CacheKeyTests(unittest.TestCase):
    def test_stable_getter_layout_and_mutated_header_rejected(self):
        # Values are generated test data, never a live cache secret.
        base, app, table, hub, address = 0x10000000, 0x20000000, 0x30000000, 0x40000000, 0x50000000
        mask = struct.pack('<4Q', 0x24e867175f37c146, 0x6462eb5e9fa90471,
                           0xb3d17f98dc8f123b, 0x2959d0276c89b57c)
        plain = bytes(range(16))
        encoded = bytes(a ^ b for a, b in zip(plain, mask))
        header = b'\x20' + encoded + b'\0' * 7
        memory = {base + 0xacf6710: struct.pack('<Q', app), app: struct.pack('<Q', table),
            table + 0x68: struct.pack('<Q', base + 0x6041b30), app + 0x68: struct.pack('<Q', hub),
            hub + 0x240: header, hub + 0x241: encoded}
        process = MagicMock(base=base)
        process.read.side_effect = lambda pointer, length: memory[pointer][:length]
        self.assertEqual(_cache_key(process), plain[:16])
        memory[hub + 0x240] = struct.pack('<QQQ', 33, 16, address)
        memory[address] = encoded
        self.assertEqual(_cache_key(process), plain[:16])
        calls = 0
        def unstable(pointer, length):
            nonlocal calls
            if pointer == hub + 0x240:
                calls += 1
                if calls > 1: return b'\0' * length
            return memory[pointer][:length]
        process.read.side_effect = unstable
        with self.assertRaises(EmoticonSessionExpired): _cache_key(process)
