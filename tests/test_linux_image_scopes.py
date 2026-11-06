"""Scoped incoming DTO capture, account changes and stale lease cancellation."""
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

from wxbot_framework.linux_sender import LinuxNativeSender, LinuxSendError, LinuxSendIndeterminate

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == 'posix' and hasattr(socket, 'SO_PEERCRED') and shutil.which('gcc'), 'requires Linux/GCC')
class LinuxImageScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='helper-image-scopes-')
        cls.folder = Path(cls.build.name)
        cls.fixture, cls.bridge, cls.fast = [cls.folder / name for name in ('fixture', 'send.so', 'fast.so')]
        flags = ['gcc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread']
        subprocess.run(flags + ['-rdynamic', str(ROOT / 'tests/fixtures/linux_send_fixture.c'), '-o', str(cls.fixture)], check=True)
        for target, extra in ((cls.bridge, []), (cls.fast, ['-DHELPER_FIXTURE_WAIT_MS=20'])):
            subprocess.run(flags + ['-fPIC', '-shared', '-DSDK_FIXTURE', '-Wno-unused-function',
                *extra, str(ROOT / 'native/linux/send_bridge.c'), '-ldl', '-o', str(target)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def spawn(self, *, delay=False, fast=False):
        tmp = tempfile.TemporaryDirectory(dir=self.folder)
        self.addCleanup(tmp.cleanup)
        profile = Path(tmp.name) / 'profile'
        profile.write_text('wxid_self_fixture')
        sender = LinuxNativeSender(directory=tmp.name, timeout=2)
        env = dict(os.environ, **sender.environment(), LD_PRELOAD=str(self.fast if fast else self.bridge),
            HELPER_FIXTURE_PROFILE_FILE=str(profile), HELPER_FIXTURE_IMAGE_DELAY='1' if delay else '0')
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
        return sender, profile

    def test_exact_multiple_peers_and_room_receiver_are_filtered(self):
        sender, _ = self.spawn()
        self.assertIn('image_scope_groups', sender.features)
        generation = sender.bind_image_scopes(['wxid_friend_fixture', '123@chatroom'], 'wxid_self_fixture')
        self.assertGreater(generation, 0)
        self.assertTrue(sender.image_context_available('a'*32, scope_generation=generation))
        self.assertTrue(sender.image_context_available('d'*32, scope_generation=generation))
        self.assertFalse(sender.image_context_available('b'*32, scope_generation=generation))
        self.assertFalse(sender.image_context_available('c'*32, scope_generation=generation))
        self.assertTrue(sender.download_message_image('d'*32, scope_generation=generation))

    def test_unrestricted_scope_accepts_regular_peers_only_for_current_account(self):
        sender, profile = self.spawn()
        generation = sender.bind_all_image_scopes('wxid_self_fixture')
        for md5 in ('a' * 32, 'd' * 32):
            self.assertTrue(sender.image_context_available(md5, scope_generation=generation))
        for md5 in ('b' * 32, 'c' * 32, 'e' * 32):
            self.assertFalse(sender.image_context_available(md5, scope_generation=generation))
        profile.write_text('wxid_other')
        with self.assertRaises(LinuxSendError):
            sender.image_context_available('a' * 32, scope_generation=generation)

    def test_same_scope_is_idempotent_including_target_order(self):
        sender, _ = self.spawn()
        generation = sender.bind_image_scopes(['123@chatroom', 'wxid_friend_fixture'], 'wxid_self_fixture')
        sender.image_context_available('a'*32)
        status = dict(sender.image_context_status)
        again = sender.bind_image_scopes(['wxid_friend_fixture', '123@chatroom'], 'wxid_self_fixture')
        self.assertEqual(generation, again)
        sender.image_context_available('a'*32)
        self.assertEqual(status, sender.image_context_status)

    def test_rebind_releases_previous_room_context_and_old_stop_cannot_clear_new_scope(self):
        sender, _ = self.spawn()
        old = sender.bind_image_scopes(['123@chatroom'], 'wxid_self_fixture')
        current = sender.bind_image_scopes(['wxid_friend_fixture'], 'wxid_self_fixture')
        self.assertNotEqual(old, current)
        self.assertFalse(sender.clear_image_scope(old))
        self.assertEqual(sender.image_scope_generation, current)
        self.assertFalse(sender.image_context_available('d'*32, scope_generation=current))
        self.assertTrue(sender.image_context_available('a'*32, scope_generation=current))
        with self.assertRaisesRegex(LinuxSendError, '范围已变化'):
            sender.download_message_image('a'*32, scope_generation=old)
        for command in (b'K', b'D'):
            with self.assertRaisesRegex(LinuxSendError, 'scope'):
                sender._result(sender._request(command, (str(old)+'\n'+'a'*32).encode()))

    def test_clear_releases_contexts_and_disarms(self):
        sender, _ = self.spawn()
        generation = sender.bind_image_scopes(['123@chatroom'], 'wxid_self_fixture')
        self.assertTrue(sender.clear_image_scope(generation))
        self.assertIsNone(sender.image_account)
        self.assertIsNone(sender.image_scope_generation)
        self.assertFalse(sender.clear_image_scope(generation))
        self.assertFalse(sender.image_context_available('d'*32))
        with self.assertRaises(LinuxSendError):
            sender.download_message_image('d'*32, scope_generation=generation)

    def test_no_wildcards_empty_duplicate_or_excessive_targets(self):
        sender, _ = self.spawn()
        for targets in ([], ['*'], ['123@chatroom']*2, ['@chatroom'], ['x@y'], ['x\ny'],
                [f'{i}@chatroom' for i in range(65)]):
            with self.subTest(targets=targets[:2]), self.assertRaises(ValueError):
                sender.bind_image_scopes(targets, 'wxid_self_fixture')
        sender.ensure_context()
        for value in ('wxid_self_fixture\n', 'wxid_self_fixture\n*\n123@chatroom', 'wxid_self_fixture\n123@chatroom\n*', 'wxid_self_fixture\n123@chatroom\n123@chatroom',
                'wxid_self_fixture\n'+'\n'.join(f'{i}@chatroom' for i in range(65))):
            with self.assertRaises(LinuxSendError):
                sender._result(sender._request(b'C', value.encode()))

    def test_new_scope_rejects_legacy_queries_without_generation(self):
        sender, _ = self.spawn()
        sender.bind_image_scopes(['123@chatroom'], 'wxid_self_fixture')
        for command in (b'K', b'D'):
            with self.assertRaisesRegex(LinuxSendError, 'scope'):
                sender._result(sender._request(command, b'a'*32))

    def test_account_switch_prevents_bind_lookup_and_download(self):
        sender, profile = self.spawn()
        with self.assertRaisesRegex(LinuxSendError, 'account'):
            sender.bind_image_scopes(['123@chatroom'], 'wxid_other')
        generation = sender.bind_image_scopes(['123@chatroom'], 'wxid_self_fixture')
        profile.write_text('wxid_other')
        with self.assertRaisesRegex(LinuxSendError, 'account'):
            sender.image_context_available('d'*32, scope_generation=generation)
        with self.assertRaisesRegex(LinuxSendError, 'account'):
            sender.download_message_image('d'*32, scope_generation=generation)
        profile.write_text('wxid_self_fixture')
        with self.assertRaisesRegex(LinuxSendError, 'scope'):
            sender.image_context_available('d'*32, scope_generation=generation)

    def test_account_switch_during_download_cannot_publish_success(self):
        sender, profile = self.spawn(delay=True)
        generation = sender.bind_image_scopes(['123@chatroom'], 'wxid_self_fixture')
        timer = threading.Timer(.05, lambda: profile.write_text('wxid_other'))
        timer.start()
        try:
            with self.assertRaisesRegex(LinuxSendError, 'account changed during download'):
                sender.download_message_image('d'*32, scope_generation=generation)
        finally:
            timer.join()

    def test_stop_after_native_timeout_invalidates_pending_download_generation(self):
        sender, _ = self.spawn(delay=True, fast=True)
        generation = sender.bind_image_scopes(['123@chatroom'], 'wxid_self_fixture')
        with self.assertRaises(LinuxSendIndeterminate):
            sender.download_message_image('d'*32, scope_generation=generation)
        self.assertTrue(sender.clear_image_scope(generation))
        time.sleep(.25)
        current = sender.bind_image_scopes(['wxid_friend_fixture'], 'wxid_self_fixture')
        self.assertGreater(current, generation)
        self.assertFalse(sender.clear_image_scope(generation))
        self.assertTrue(sender.image_context_available('a'*32, scope_generation=current))
