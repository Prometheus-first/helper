import hashlib
from concurrent.futures import ThreadPoolExecutor
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
        self.assertIn('备份 3 项', guard.protect(Path('.'), block_paths=False))
        self.assertFalse(self.updater.exists())
        self.assertTrue((self.version / guard.BACKUP).exists())
        self.assertTrue(untouched.exists())
        backups = list((self.local / 'helper-wechat-update-backup').glob('staged-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'update.data').read_text(encoding='utf-8'), 'test')
        self.assertEqual((backups[0] / 'patch-4.2.0.13' / 'fixture.bin').read_bytes(), b'data')
        self.assertIn('已处于停用状态', guard.protect(Path('.'), block_paths=False))
        self.assertEqual(guard.restore(Path('.')), '更新器已恢复')
        self.assertTrue(self.updater.is_file())
        self.assertFalse((self.cache / 'update.data').exists())

    def test_unknown_updater_is_never_modified(self):
        self.updater.write_bytes(b'unknown updater')
        with self.assertRaises(guard.GuardError):
            guard.protect(Path('.'), block_paths=False)
        self.assertEqual(self.updater.read_bytes(), b'unknown updater')
        self.assertFalse((self.version / guard.BACKUP).exists())

    def test_existing_backup_is_not_overwritten(self):
        backup = self.version / guard.BACKUP
        backup.write_bytes(b'previous backup')
        with self.assertRaises(guard.GuardError):
            guard.protect(Path('.'), block_paths=False)
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
                    guard.protect(Path('.'), block_paths=False)
            self.assertTrue(self.updater.exists())
            self.assertFalse((self.version / guard.BACKUP).exists())
        finally:
            self.install_patch.start()

    def staged(self, name, content=b'cached-update', stamp=1):
        directory = self.local / 'helper-wechat-update-backup' / name
        directory.mkdir(parents=True)
        (directory / 'update.data').write_bytes(content)
        os.utime(directory, ns=(stamp, stamp))
        return directory

    def test_repeated_protect_retains_only_latest_cache_and_restore_still_works(self):
        (self.cache / 'update.data').write_text('first', encoding='utf-8')
        guard.protect(Path('.'), block_paths=False)
        old = next((self.local / 'helper-wechat-update-backup').glob('staged-*'))
        os.utime(old, ns=(1, 1))
        (self.cache / 'update.data').write_text('second', encoding='utf-8')
        self.assertIn('已清理 1 份', guard.protect(Path('.'), block_paths=False))
        backups = list((self.local / 'helper-wechat-update-backup').glob('staged-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'update.data').read_text('utf-8'), 'second')
        self.assertFalse(old.exists())
        self.assertTrue((self.version / guard.BACKUP).is_file())
        self.assertEqual(guard.restore(Path('.')), '更新器已恢复')

    def test_protect_cleans_legacy_backups_even_without_new_downloads(self):
        old = self.staged('staged-4.2.0.13-20261110-094553')
        latest = self.staged('staged-' + 'b' * 32, b'latest', 2)
        self.assertIn('已清理 1 份', guard.protect(Path('.'), block_paths=False))
        self.assertFalse(old.exists())
        self.assertEqual((latest / 'update.data').read_bytes(), b'latest')
        self.assertTrue((self.version / guard.BACKUP).is_file())

    def test_cleanup_preserves_unknown_files_other_versions_and_pending_moves(self):
        old = self.staged('staged-' + 'a' * 32)
        latest = self.staged('staged-' + 'b' * 32, stamp=2)
        unknown = self.staged('staged-' + 'c' * 32, stamp=3)
        (unknown / 'personal.txt').write_text('keep', encoding='utf-8')
        other = self.staged('staged-4.2.0.14-20261110-094553', stamp=4)
        pending = self.staged('pending-' + 'd' * 32, stamp=5)
        self.assertIn('已清理 1 份', guard.cleanup_backups())
        self.assertFalse(old.exists())
        for path in (latest, unknown, other, pending):
            self.assertTrue(path.is_dir())
        # 单独清理缓存不会修改安装目录的更新器。
        self.assertTrue(self.updater.is_file())

    def test_nested_reparse_is_rejected_before_any_backup_is_deleted(self):
        old = self.staged('staged-' + 'a' * 32)
        newer = self.staged('staged-' + 'b' * 32, stamp=2)
        cache = old / 'download-4.2.0.13'
        cache.mkdir()
        os.utime(old, ns=(1, 1))
        original = guard._reparse
        with patch.object(guard, '_reparse', side_effect=lambda p: p == cache or original(p)):
            with self.assertRaises(guard.GuardError):
                guard.cleanup_backups()
        self.assertTrue(old.is_dir())
        self.assertTrue(newer.is_dir())

    def test_backup_root_reparse_is_rejected_without_touching_files(self):
        old = self.staged('staged-' + 'a' * 32)
        original = guard._reparse
        with patch.object(guard, '_reparse', side_effect=lambda p: p == old.parent or original(p)):
            with self.assertRaises(guard.GuardError):
                guard.cleanup_backups()
        self.assertTrue(old.is_dir())

    def test_failed_move_is_left_pending_and_existing_backups_are_not_pruned(self):
        old = self.staged('staged-' + 'a' * 32)
        (self.cache / 'update.data').write_text('new', encoding='utf-8')
        rename = Path.rename
        def fail_move(path, target):
            if path == self.cache / 'update.data':
                raise PermissionError('fixture locked cache')
            return rename(path, target)
        with patch.object(Path, 'rename', fail_move):
            with self.assertRaises(guard.GuardError):
                guard.protect(Path('.'), block_paths=False)
        self.assertTrue(old.is_dir())
        self.assertTrue((self.cache / 'update.data').is_file())
        self.assertTrue(any(old.parent.glob('pending-*')))

    def test_cleanup_with_no_backups_is_idempotent(self):
        self.assertIn('已清理 0 份', guard.cleanup_backups())
        only = self.staged('staged-' + 'a' * 32)
        self.assertIn('已清理 0 份', guard.cleanup_backups())
        self.assertTrue(only.is_dir())

    def test_cleanup_refuses_relative_local_app_data(self):
        with patch.dict(os.environ, {'LOCALAPPDATA': 'relative-directory'}):
            with self.assertRaises(guard.GuardError):
                guard.cleanup_backups()

    def test_default_protection_removes_all_packages_and_blocks_recreation(self):
        self.staged('staged-' + 'a' * 32)
        (self.cache / 'update.data').write_bytes(b'update metadata')
        for category in ('download', 'patch'):
            for version in ('4.2.0.13', '4.2.0.14'):
                directory = self.cache / category / version
                directory.mkdir(parents=True)
                (directory / 'WeixinUpdate.exe').write_bytes(b'new updater')
        self.assertIn('原路径已封锁', guard.protect(Path('.')))
        self.assertTrue(self.updater.is_dir())
        self.assertEqual(self.cache.read_bytes(), guard.BLOCKER_CONTENT)
        self.assertEqual((self.version / guard.BACKUP).read_bytes(), b'fixture-updater')
        self.assertEqual(list((self.local / 'helper-wechat-update-backup').glob('staged-*')), [])
        with self.assertRaises(FileExistsError):
            self.cache.mkdir()
        with self.assertRaises(OSError):
            self.updater.write_bytes(b'recreated updater')
        self.assertIn('已清除 0 项', guard.protect(Path('.')))
        self.assertEqual(guard.restore(Path('.')), '更新器已恢复')
        self.assertEqual(self.updater.read_bytes(), b'fixture-updater')
        self.assertFalse(self.cache.exists())

    def test_recreated_verified_updater_reuses_restore_backup(self):
        (self.version / guard.BACKUP).write_bytes(b'fixture-updater')
        guard.protect(Path('.'))
        self.assertTrue(self.updater.is_dir())
        self.assertEqual((self.version / guard.BACKUP).read_bytes(), b'fixture-updater')

    def test_unknown_cache_content_is_not_deleted(self):
        unknown = self.cache / 'notes.txt'
        unknown.write_text('unrelated', encoding='utf-8')
        with self.assertRaises(guard.GuardError):
            guard.protect(Path('.'))
        self.assertEqual(unknown.read_text('utf-8'), 'unrelated')

    def test_restore_refuses_unknown_updater_placeholder(self):
        guard.protect(Path('.'))
        unknown = self.updater / 'notes.txt'
        unknown.write_text('keep', encoding='utf-8')
        with self.assertRaises(guard.GuardError):
            guard.restore(Path('.'))
        self.assertEqual(unknown.read_text('utf-8'), 'keep')
        self.assertTrue(self.cache.is_file())

    def test_restore_preserves_blockers_when_backup_hash_is_wrong(self):
        guard.protect(Path('.'))
        (self.version / guard.BACKUP).write_bytes(b'unknown backup')
        with self.assertRaises(guard.GuardError):
            guard.restore(Path('.'))
        self.assertTrue(guard._owned_updater_blocker(self.updater))
        self.assertTrue(guard._owned_cache_blocker(self.cache))

    def test_multiple_accounts_share_a_serialized_protection_operation(self):
        (self.cache / 'update.data').write_bytes(b'new update')
        with ThreadPoolExecutor(3) as executor:
            results = list(executor.map(lambda _: guard.protect(Path('.')), range(3)))
        self.assertEqual(len(results), 3)
        self.assertTrue(all('原路径已封锁' in result for result in results))
        self.assertTrue(guard._owned_cache_blocker(self.cache))
        self.assertTrue(guard._owned_updater_blocker(self.updater))

    def test_startup_preflight_is_opt_in_and_runs_without_a_bot_connection(self):
        from wxbot_framework.update_guard import check_update_protection
        root = Path(self.temp.name) / 'project'
        self.assertIsNone(check_update_protection(root, {'plugins': {}}))
        self.assertTrue(self.updater.is_file())
        result = check_update_protection(root, {'plugins': {'wechat_update_guard': {'enabled': True}}})
        self.assertIn('原路径已封锁', result)
        self.assertTrue(guard._owned_cache_blocker(self.cache))

    def test_wechat_launch_does_not_run_when_protection_fails(self):
        from wxbot_framework.wechat_login import _launch
        from wxbot_framework.panel_common import PanelError
        with patch('wxbot_framework.update_guard.check_update_protection', side_effect=guard.GuardError('fixture refusal')), \
                patch('wxbot_framework.wechat_login.subprocess.Popen') as launch:
            with self.assertRaises(PanelError):
                _launch(Path(self.temp.name))
        launch.assert_not_called()

    def test_wechat_launch_checks_protection_before_creating_the_process(self):
        import json
        from wxbot_framework.wechat_login import _launch
        root = Path(self.temp.name) / 'project'
        root.mkdir()
        (root / 'bot_config.json').write_text(json.dumps({'plugins': {'wechat_update_guard': {'enabled': True}}}), encoding='utf-8')
        def launch(*args, **kwargs):
            self.assertTrue(guard._owned_cache_blocker(self.cache))
            self.assertTrue(guard._owned_updater_blocker(self.updater))
            return SimpleNamespace(pid=123)
        with patch('wxbot_framework.wechat_login._install_path', return_value=self.install / 'Weixin.exe'), \
                patch('wxbot_framework.wechat_login.subprocess.Popen', side_effect=launch):
            self.assertEqual(_launch(root).pid, 123)


if __name__ == '__main__':
    unittest.main()
