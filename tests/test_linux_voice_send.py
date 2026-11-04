"""Native voice boundary/transport fixtures; do not contact real WeChat."""
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from wxbot_framework.linux_sender import LinuxNativeSender, LinuxSendError, LinuxSendIndeterminate

ROOT = Path(__file__).resolve().parents[1]
HEADER = b'\x02#!SILK_V3'


@unittest.skipUnless(os.name == 'posix' and hasattr(socket, 'SO_PEERCRED') and shutil.which('gcc'), 'requires Linux/GCC')
class LinuxVoiceSendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='helper-voice-send-')
        folder = Path(cls.build.name)
        cls.fixture, cls.bridge = folder / 'fixture', folder / 'send.so'
        flags = ['gcc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread']
        subprocess.run(flags + ['-rdynamic', str(ROOT / 'tests/fixtures/linux_send_fixture.c'), '-o', str(cls.fixture)], check=True)
        subprocess.run(flags + ['-fPIC', '-shared', '-DSDK_FIXTURE', '-Wno-unused-function',
            str(ROOT / 'native/linux/send_bridge.c'), '-ldl', '-o', str(cls.bridge)], check=True)
        # Compile the real DTO construction branch too; never load it here.
        subprocess.run(flags + ['-fPIC', '-shared', str(ROOT / 'native/linux/send_bridge.c'),
            '-ldl', '-o', str(folder / 'production.so')], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='helper-voice-data-')
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.voice = self.folder / '语音.silk'
        # Framing-only sample: payload is synthetic, not claimed decodable.
        self.data = HEADER + b'\x03\x00\x11\x00\x22' + b'\x02\x00\x00\x33'
        self.voice.write_bytes(self.data)
        self.voice.chmod(0o600)

    def spawn(self, *, bind=True, mode='', timeout=2, remove=False):
        sender = LinuxNativeSender(timeout=timeout, allowed_targets=('123@chatroom',), directory=self.folder)
        profile, report = self.folder / 'profile', self.folder / 'report'
        profile.write_text('wxid_self_fixture')
        env = dict(os.environ, **sender.environment(), LD_PRELOAD=str(self.bridge),
            HELPER_FIXTURE_PROFILE_FILE=str(profile), HELPER_FIXTURE_VOICE_REPORT=str(report),
            HELPER_FIXTURE_VOICE_MODE=mode)
        if remove:
            env['HELPER_FIXTURE_VOICE_REMOVE'] = str(self.voice)
        child = subprocess.Popen([str(self.fixture)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        sender.bind_pid(child.pid)
        def stop():
            sender.close()
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)
            sender.cleanup_retained_files()
            child.stderr.close()
        self.addCleanup(stop)
        sender.wait_ready(process=child)
        if bind:
            sender.bind_account('wxid_self_fixture')
        return sender, profile, report

    def raw(self, sender, path=None, duration='40', target='123@chatroom'):
        sender.ensure_context()
        body = (target + '\n' + duration + '\n' + os.fsencode(path or self.voice).hex()).encode('ascii')
        return sender._result(sender._request(b'W', body), sending=True)

    def test_owned_binary_bytes_survive_source_deletion(self):
        sender, _, report = self.spawn(remove=True)
        self.assertIn('local_voice', sender.features)
        self.raw(sender)
        self.assertFalse(self.voice.exists())
        self.assertEqual(report.read_bytes(), b'123@chatroom\n40\n' + self.data)

    def test_public_sender_normalizes_standard_silk_and_cleans_snapshot(self):
        sender, _, report = self.spawn()
        self.voice.write_bytes(self.data[1:] + b'\xff\xff')
        seen = []
        request = sender._request
        def capture(command, body=b''):
            if command == b'W':
                p = Path(os.fsdecode(bytes.fromhex(body.split(b'\n')[2].decode())))
                self.assertEqual(p.stat().st_mode & 0o777, 0o600)
                self.assertEqual(p.read_bytes(), self.data)
                seen.append(p)
            return request(command, body)
        with patch.object(sender, '_request', capture):
            self.assertTrue(sender.send_voice('123@chatroom', self.voice))
        self.assertEqual(report.read_bytes(), b'123@chatroom\n40\n' + self.data)
        self.assertFalse(seen[0].exists())

    def test_native_requires_binding_and_current_account(self):
        sender, profile, report = self.spawn(bind=False)
        with self.assertRaisesRegex(LinuxSendError, 'bind'):
            self.raw(sender)
        sender.bind_account('wxid_self_fixture')
        profile.write_text('wxid_other_fixture')
        with self.assertRaisesRegex(LinuxSendError, 'account changed'):
            self.raw(sender)
        self.assertFalse(report.exists())

    def test_native_rejects_bad_framing_and_duration(self):
        sender, _, report = self.spawn()
        invalid = [b'not silk', self.data[:-1], HEADER, HEADER + b'\x00\x00',
            self.data + b'\xff\xff', self.data[1:], HEADER + b'\xff\x7f' + b'a',
            HEADER + (b'\x01\x00a' * 3001), b'x' * 262145]
        for data in invalid:
            self.voice.write_bytes(data)
            with self.subTest(size=len(data)), self.assertRaises(LinuxSendError):
                self.raw(sender)
        self.voice.write_bytes(self.data)
        for duration in ('0', '-20', '20', '41', '60020', '123456', '40x'):
            with self.subTest(duration=duration), self.assertRaises(LinuxSendError):
                self.raw(sender, duration=duration)
        self.assertFalse(report.exists())

    def test_native_rejects_unprivate_linked_and_nonregular_files(self):
        sender, _, report = self.spawn()
        link = self.folder / 'link.silk'
        link.symlink_to(self.voice)
        with self.assertRaises(LinuxSendError):
            self.raw(sender, link)
        hard = self.folder / 'hard.silk'
        os.link(self.voice, hard)
        with self.assertRaises(LinuxSendError):
            self.raw(sender)
        hard.unlink()
        self.voice.chmod(0o644)
        with self.assertRaises(LinuxSendError):
            self.raw(sender)
        fifo = self.folder / 'fifo'
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(LinuxSendError):
            self.raw(sender, fifo)
        self.assertFalse(report.exists())

    def test_public_sender_authorization_and_feature_guards(self):
        sender, _, report = self.spawn()
        with self.assertRaises(LinuxSendError):
            sender.send_voice('456@chatroom', self.voice)
        sender.features = frozenset()
        with self.assertRaises(LinuxSendError):
            sender.send_voice('123@chatroom', self.voice)
        self.assertFalse(report.exists())

    def test_indeterminate_retains_snapshot_and_blocks_retry(self):
        sender, _, report = self.spawn(mode='delay', timeout=.03)
        with self.assertRaises(LinuxSendIndeterminate):
            sender.send_voice('123@chatroom', self.voice)
        self.assertEqual(sender._pending_voice.read_bytes(), self.data)
        with self.assertRaises(LinuxSendIndeterminate):
            sender.send_voice('123@chatroom', self.voice)
        time.sleep(.25)
        self.assertEqual(report.read_bytes(), b'123@chatroom\n40\n' + self.data)

    def test_reject_does_not_report_success(self):
        sender, _, _ = self.spawn(mode='reject')
        with self.assertRaisesRegex(LinuxSendError, 'rejected'):
            sender.send_voice('123@chatroom', self.voice)
        self.assertIsNone(sender._pending_voice)
        self.assertEqual(list(sender._directory.glob('voice-*')), [])

    def test_native_dry_run_guard(self):
        sender, _, report = self.spawn()
        frame = b'123@chatroom\n40\n' + os.fsencode(self.voice).hex().encode()
        with self.assertRaisesRegex(LinuxSendError, 'dry run'):
            sender._result(sender._request(b'W', frame), sending=True)
        self.assertFalse(report.exists())


if __name__ == '__main__':
    unittest.main()
