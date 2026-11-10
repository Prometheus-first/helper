import hashlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from plugins.wechat_update_guard import guard


class WechatUpdateGuardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.install = root / 'install'
        self.version = self.install / guard.VERSION
        self.version.mkdir(parents=True)
        self.updater = self.version / guard.UPDATER
        self.updater.write_bytes(b'fixture-updater')
        self.roaming, self.local = root / 'roaming', root / 'local'
        self.cache = self.roaming / 'Tencent' / 'xwechat' / 'update'
        self.cache.mkdir(parents=True)
        self.local.mkdir()
        self.env = patch.dict(os.environ, {'APPDATA': str(self.roaming), 'LOCALAPPDATA': str(self.local)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.install_patch = patch.object(guard, '_installation', return_value=self.install)
        self.install_patch.start()
        self.addCleanup(self.install_patch.stop)
        self.hash_patch = patch.object(guard, 'UPDATER_SHA256', hashlib.sha256(b'fixture-updater').hexdigest())
        self.hash_patch.start()
        self.addCleanup(self.hash_patch.stop)

    def test_protect_backup_restore_and_idempotence(self):
        (self.cache / 'update.data').write_text('test', encoding='utf-8')
        for name in ('download', 'patch'):
            directory = self.cache / name / '4.2.0.13'
            directory.mkdir(parents=True)
            (directory / 'fixture.bin').write_bytes(b'data')
        untouched = self.cache / 'download' / 'unrelated'
        untouched.mkdir()
        self.assertIn('备份 3 项', guard.protect(Path('.')))
        self.assertFalse(self.updater.exists())
        self.assertTrue((self.version / guard.BACKUP).exists())
        self.assertTrue(untouched.exists())
        backups = list((self.local / 'helper-wechat-update-backup').iterdir())
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'update.data').read_text(encoding='utf-8'), 'test')
        self.assertEqual((backups[0] / 'patch-4.2.0.13' / 'fixture.bin').read_bytes(), b'data')
        self.assertIn('已处于停用状态', guard.protect(Path('.')))
        self.assertEqual(guard.restore(Path('.')), '更新器已恢复')
        self.assertTrue(self.updater.is_file())
        self.assertFalse((self.cache / 'update.data').exists())

    def test_unknown_updater_is_never_modified(self):
        self.updater.write_bytes(b'unknown updater')
        with self.assertRaises(guard.GuardError):
            guard.protect(Path('.'))
        self.assertEqual(self.updater.read_bytes(), b'unknown updater')
        self.assertFalse((self.version / guard.BACKUP).exists())

    def test_existing_backup_is_not_overwritten(self):
        backup = self.version / guard.BACKUP
        backup.write_bytes(b'previous backup')
        with self.assertRaises(guard.GuardError):
            guard.protect(Path('.'))
        self.assertEqual(backup.read_bytes(), b'previous backup')
        self.assertTrue(self.updater.exists())

    def test_reparse_cache_is_rejected(self):
        (self.cache / 'update.data').write_text('test', encoding='utf-8')
        original = guard._reparse
        with patch.object(guard, '_reparse', side_effect=lambda p: p.name == 'update.data' or original(p)):
            with self.assertRaises(guard.GuardError):
                guard._backup_staged_updates()
        self.assertTrue((self.cache / 'update.data').exists())

    @unittest.skipUnless(os.name == 'nt', '微信安装位置检查仅适用于 Windows')
    def test_other_version_is_not_modified(self):
        self.install_patch.stop()
        try:
            with patch('wxbot_framework.wechat_login._install_path', return_value=self.install / 'Weixin.exe'), \
                    patch('native.moments_transport._core_profile', return_value=SimpleNamespace(version=guard.VERSION)), \
                    patch.object(guard, '_version', return_value=(4, 2, 0, 13)):
                with self.assertRaises(guard.GuardError):
                    guard.protect(Path('.'))
            self.assertTrue(self.updater.exists())
            self.assertFalse((self.version / guard.BACKUP).exists())
        finally:
            self.install_patch.start()


if __name__ == '__main__':
    unittest.main()
