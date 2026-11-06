"""Video request validation and isolated native transport fixture; no WeChat."""
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wxbot_framework.linux_sender import LinuxNativeSender, LinuxSendError
from wxbot_framework.videos import inspect_video
from wxbot_framework.sdk import Message, PluginContext, ServiceFacade

ROOT = Path(__file__).resolve().parents[1]


def box(kind, body):
    return struct.pack('>I4s', len(body) + 8, kind) + body


def video(handler=b'vide'):
    mvhd = b'\0' * 12 + struct.pack('>II', 1000, 2200)
    return box(b'ftyp', b'isom\0\0\0\0') + box(b'moov', box(b'mvhd', mvhd) +
        box(b'trak', box(b'mdia', box(b'hdlr', b'\0' * 8 + handler))))


@unittest.skipUnless(os.name == 'posix' and hasattr(socket, 'SO_PEERCRED') and shutil.which('gcc'), 'requires Linux/GCC')
class LinuxVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix='helper-video-fixture-')
        folder = Path(cls.build.name)
        cls.fixture, cls.bridge = folder / 'fixture', folder / 'send.so'
        flags = ['gcc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror', '-pthread']
        subprocess.run(flags + ['-rdynamic', str(ROOT / 'tests/fixtures/linux_send_fixture.c'), '-o', str(cls.fixture)], check=True)
        subprocess.run(flags + ['-fPIC', '-shared', '-DSDK_FIXTURE', '-Wno-unused-function',
            str(ROOT / 'native/linux/send_bridge.c'), '-ldl', '-o', str(cls.bridge)], check=True)
        subprocess.run(flags + ['-fPIC', '-shared', str(ROOT / 'native/linux/send_bridge.c'),
            '-ldl', '-o', str(folder / 'production.so')], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='helper-video-data-')
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.source = self.folder / '示例.mp4'
        self.source.write_bytes(video())

    def spawn(self, *, bind=True):
        sender = LinuxNativeSender(timeout=2, allowed_targets=('123@chatroom',), directory=self.folder)
        profile, report = self.folder / 'profile', self.folder / 'report'
        profile.write_text('wxid_self_fixture')
        env = dict(os.environ, **sender.environment(), LD_PRELOAD=str(self.bridge),
                   HELPER_FIXTURE_PROFILE_FILE=str(profile), HELPER_FIXTURE_VIDEO_REPORT=str(report))
        env.pop('HELPER_LINUX_VIDEO_DIAGNOSTIC', None)
        child = subprocess.Popen([str(self.fixture)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        sender.bind_pid(child.pid)
        def stop():
            sender.close()
            if child.poll() is None:
                child.terminate(); child.wait(timeout=3)
            sender.cleanup_retained_files()
            child.stderr.close()
        self.addCleanup(stop)
        sender.wait_ready(process=child)
        if bind:
            sender.bind_account('wxid_self_fixture')
        return sender, profile, report

    def test_video_snapshot_has_duration_and_is_retained_until_process_exit(self):
        sender, _, report = self.spawn()
        self.assertIn('local_video', sender.features)
        self.assertEqual(inspect_video(self.source).duration_seconds, 3)
        self.assertTrue(sender.send_video('123@chatroom', self.source))
        target, duration, path = report.read_text().splitlines()
        snapshot = Path(path)
        self.assertEqual((target, duration), ('123@chatroom', '3'))
        self.assertEqual(snapshot.suffix, '.mp4')
        self.assertEqual(snapshot.stat().st_mode & 0o777, 0o600)
        self.source.unlink()
        self.assertEqual(snapshot.read_bytes(), video())
        self.assertFalse(sender.cleanup_retained_files())

    def test_missing_capability_and_target_scope(self):
        sender, _, report = self.spawn()
        self.assertIn('local_video', sender.features)
        sender.features = frozenset()
        with self.assertRaises(LinuxSendError): sender.send_video('123@chatroom', self.source)
        self.assertFalse(report.exists())
        sender.features = frozenset({'local_video'})
        with self.assertRaises(LinuxSendError): sender.send_video('456@chatroom', self.source)

    def test_account_change_fails_before_send(self):
        sender, profile, report = self.spawn()
        profile.write_text('wxid_other_fixture')
        with self.assertRaises(LinuxSendError): sender.send_video('123@chatroom', self.source)
        self.assertFalse(report.exists())

    def test_bad_mp4_is_rejected_before_native_send(self):
        sender, _, report = self.spawn()
        for contents in (video()[:-2], video(b'soun'), b'not an mp4'):
            self.source.write_bytes(contents)
            with self.assertRaises(ValueError): sender.send_video('123@chatroom', self.source)
        self.assertFalse(report.exists())

    def test_native_boundary_rejects_unprivate_file(self):
        sender, _, report = self.spawn()
        self.source.chmod(0o644)
        sender.ensure_context()
        body = ('123@chatroom\n3\n' + os.fsencode(self.source).hex()).encode('ascii')
        with self.assertRaises(LinuxSendError): sender._result(sender._request(b'R', body), sending=True)
        self.assertFalse(report.exists())

    def test_extension_is_kept_for_video_without_source_suffix(self):
        sender, _, report = self.spawn()
        original = self.folder / 'clip.data'
        original.write_bytes(video())
        self.assertTrue(sender.send_video('123@chatroom', original))
        self.assertTrue(report.read_text().splitlines()[2].endswith('.mp4'))

    def test_missing_account_rejects_native_request(self):
        sender, _, report = self.spawn(bind=False)
        with self.assertRaises(LinuxSendError): sender.send_video('123@chatroom', self.source)
        self.assertFalse(report.exists())


class LinuxVideoFacadeTests(unittest.TestCase):
    def test_plugin_reply_keeps_the_original_connection(self):
        service = ServiceFacade(text_delay={'enabled': False})
        backend = Mock(session_id='first')
        backend.send_video.return_value = True
        service.bind(backend)
        self.addCleanup(service.close)
        context = PluginContext('test', {}, service, {'video.send'})
        with patch('wxbot_framework.videos.inspect_video', return_value=SimpleNamespace(path=Path('x.mp4'))):
            self.assertTrue(context.reply_video(Message('', '123@chatroom', session_id='first'), 'x.mp4'))
            with self.assertRaises(RuntimeError):
                context.reply_video(Message('', '123@chatroom', session_id='old'), 'x.mp4')
        backend.send_video.assert_called_once_with('123@chatroom', 'x.mp4')
