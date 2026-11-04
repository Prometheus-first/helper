"""Local GIF transport guards and snapshot lifetime; no real WeChat messages."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from PIL import Image

from wxbot_framework.linux_sender import LinuxNativeSender, LinuxSendError, LinuxSendIndeterminate

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'posix' and hasattr(socket, 'SO_PEERCRED') and shutil.which('gcc'), 'requires Linux/GCC')
class LinuxEmoticonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='helper-emoticon-tests-')
        folder = Path(cls.build.name)
        cls.fixture, cls.bridge = folder / 'fixture', folder / 'send.so'
        flags = ['gcc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread']
        subprocess.run(flags + ['-rdynamic', str(ROOT / 'tests/fixtures/linux_send_fixture.c'), '-o', str(cls.fixture)], check=True)
        subprocess.run(flags + ['-fPIC', '-shared', '-DSDK_FIXTURE', '-Wno-unused-function',
            str(ROOT / 'native/linux/send_bridge.c'), '-ldl', '-o', str(cls.bridge)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='helper-gif-')
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.gif = self.folder / '表情.gif'
        first, second = Image.new('RGB', (8, 8), 'red'), Image.new('RGB', (8, 8), 'blue')
        first.save(self.gif, format='GIF', save_all=True, append_images=[second], duration=[50, 100], loop=0)
        self.gif.chmod(0o600)

    def spawn(self, *, bind=True, mode='', timeout=2):
        sender = LinuxNativeSender(timeout=timeout, allowed_targets=('123@chatroom',), directory=self.folder)
        profile, report = self.folder / 'profile', self.folder / 'report'
        profile.write_text('wxid_self_fixture')
        env = dict(os.environ, **sender.environment(), LD_PRELOAD=str(self.bridge),
            HELPER_FIXTURE_PROFILE_FILE=str(profile), HELPER_FIXTURE_EMOTICON_REPORT=str(report),
            HELPER_FIXTURE_EMOTICON_MODE=mode)
        child = subprocess.Popen([str(self.fixture)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        sender.bind_pid(child.pid)
        def stop():
            sender.close()
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)
            child.stderr.close()
        self.addCleanup(stop)
        sender.wait_ready(process=child)
        if bind:
            sender.bind_account('wxid_self_fixture')
        return sender, child, profile, report

    def raw(self, sender, path):
        sender.ensure_context()
        frame = ('123@chatroom\n' + os.fsencode(path).hex()).encode('ascii')
        return sender._result(sender._request(b'O', frame), sending=True)

    def test_preserves_gif_bytes_and_uses_private_copy_until_completion(self):
        sender, child, _, report = self.spawn()
        original = self.gif.read_bytes()
        request = sender._request
        seen = []
        def capture(command, body=b''):
            if command == b'O':
                path = Path(os.fsdecode(bytes.fromhex(body.split(b'\n')[1].decode())))
                seen.append(path)
                self.assertEqual(path.parent, sender._directory)
                self.assertNotEqual(path, self.gif)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.read_bytes(), original)
                self.gif.unlink()
            return request(command, body)
        with patch.object(sender, '_request', capture):
            self.assertTrue(sender.send_emoticon('123@chatroom', self.gif))
        self.assertEqual(report.read_text().splitlines()[:2], ['O', '123@chatroom'])
        self.assertFalse(seen[0].exists())
        self.assertIsNone(child.poll())

    def test_feature_target_and_account_required(self):
        sender, _, _, report = self.spawn(bind=False)
        with self.assertRaisesRegex(LinuxSendError, '绑定'):
            sender.send_emoticon('123@chatroom', self.gif)
        with self.assertRaisesRegex(LinuxSendError, 'bind'):
            self.raw(sender, self.gif)
        sender.bind_account('wxid_self_fixture')
        with self.assertRaisesRegex(LinuxSendError, '授权'):
            sender.send_emoticon('456@chatroom', self.gif)
        sender.features = frozenset({'generic_messages', 'local_media'})
        with self.assertRaisesRegex(LinuxSendError, '重新编译'):
            sender.send_emoticon('123@chatroom', self.gif)
        self.assertFalse(report.exists())

    def test_account_switch_rejected_by_native_after_snapshot(self):
        sender, _, profile, report = self.spawn()
        profile.write_text('wxid_other')
        with self.assertRaisesRegex(LinuxSendError, 'account changed'):
            sender.send_emoticon('123@chatroom', self.gif)
        self.assertFalse(report.exists())
        self.assertEqual(list(sender._directory.glob('*.gif')), [])

    def test_invalid_gif_symlinks_and_hardlinks_rejected(self):
        sender, _, _, report = self.spawn()
        png = self.folder / 'fake.gif'
        Image.new('RGB', (1, 1)).save(png, format='PNG')
        short = self.folder / 'short.gif'
        short.write_bytes(self.gif.read_bytes()[:-1])
        link = self.folder / 'link.gif'
        link.symlink_to(self.gif)
        hard = self.folder / 'hard.gif'
        os.link(self.gif, hard)
        for path in (png, short, link, hard):
            with self.subTest(path=path.name), self.assertRaises((ValueError, OSError)):
                sender.send_emoticon('123@chatroom', path)
        self.assertFalse(report.exists())

    def test_native_raw_frames_reject_non_gif_symlink_and_shared_permissions(self):
        sender, _, _, report = self.spawn()
        bad = self.folder / 'bad'
        bad.write_bytes(b'not a GIF;')
        bad.chmod(0o600)
        symlink = self.folder / 'linked.gif'
        symlink.symlink_to(self.gif)
        for path in (bad, symlink):
            with self.assertRaisesRegex(LinuxSendError, 'bounded GIF'):
                self.raw(sender, path)
        self.gif.chmod(0o644)
        with self.assertRaisesRegex(LinuxSendError, 'bounded GIF'):
            self.raw(sender, self.gif)
        self.assertFalse(report.exists())

    def test_native_rejection_is_final_and_cleans_snapshot(self):
        sender, _, _, report = self.spawn(mode='reject')
        with self.assertRaisesRegex(LinuxSendError, 'rejected'):
            sender.send_emoticon('123@chatroom', self.gif)
        self.assertTrue(report.exists())
        self.assertEqual(list(sender._directory.glob('*.gif')), [])
        self.assertIsNone(sender._pending_emoticon)

    def test_lost_reply_retains_one_snapshot_and_prevents_retry(self):
        sender, _, _, report = self.spawn(mode='delay', timeout=.03)
        with self.assertRaises(LinuxSendIndeterminate):
            sender.send_emoticon('123@chatroom', self.gif)
        snapshot = sender._pending_emoticon
        self.assertTrue(snapshot.is_file())
        with self.assertRaises(LinuxSendIndeterminate):
            sender.send_emoticon('123@chatroom', self.gif)
        time.sleep(.25)
        self.assertEqual(report.read_text().splitlines(), ['O', '123@chatroom', str(snapshot)])
        sender.close()
        self.assertTrue(snapshot.is_file())

    def test_transport_eof_does_not_retry_or_delete_uncertain_snapshot(self):
        sender, _, _, _ = self.spawn(mode='eof')
        with self.assertRaises(LinuxSendIndeterminate):
            sender.send_emoticon('123@chatroom', self.gif)
        self.assertTrue(sender._pending_emoticon.is_file())
        with self.assertRaises(LinuxSendIndeterminate):
            sender.send_emoticon('123@chatroom', self.gif)

    def test_source_mutation_between_validation_and_copy_is_revalidated(self):
        sender, _, _, report = self.spawn()
        from wxbot_framework import emoticons
        read = emoticons._read_bounded
        calls = []
        def replace_after_validation(path, limit, is_current=None):
            result = read(path, limit, is_current)
            calls.append(path)
            if len(calls) == 1:
                self.gif.write_bytes(b'not gif')
            return result
        with patch.object(emoticons, '_read_bounded', replace_after_validation):
            with self.assertRaises(ValueError):
                sender.send_emoticon('123@chatroom', self.gif)
        self.assertFalse(report.exists())
        self.assertEqual(list(sender._directory.glob('*.gif')), [])
