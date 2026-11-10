"""视频封装解析、会话隔离与能力范围的离线验证。"""
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wxbot_framework.backend import TEXT_42011_CAPABILITIES, TEXT_42012_CAPABILITIES
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import CapabilityError, LegacyService, Message, PluginContext, ServiceFacade
from wxbot_framework.videos import inspect_video


def box(kind, payload):
    return struct.pack(">I4s", len(payload) + 8, kind) + payload


def mp4(duration=2300, *, handler=b"vide"):
    mvhd = b"\0" * 12 + struct.pack(">II", 1000, duration)
    hdlr = b"\0" * 8 + handler
    return (box(b"ftyp", b"isom\0\0\0\0") +
            box(b"moov", box(b"mvhd", mvhd) + box(b"trak", box(b"mdia", box(b"hdlr", hdlr)))))


class VideoValidationTests(unittest.TestCase):
    def test_reads_duration_from_mp4_and_rounds_up(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            path.write_bytes(mp4())
            self.assertEqual(inspect_video(path).duration_seconds, 3)

    def test_rejects_truncated_or_audio_only_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "clip.mp4"
            for data in (mp4()[:-3], mp4(handler=b"soun"), b"not video", mp4(duration=0)):
                path.write_bytes(data)
                with self.subTest(data=data[:12]), self.assertRaises(ValueError):
                    inspect_video(path)


class VideoServiceTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock(session_id="first")
        self.backend.send_video.return_value = True
        self.service = ServiceFacade(text_delay={"enabled": False})
        self.service.bind(self.backend)
        self.addCleanup(self.service.close)
        self.context = PluginContext("test", {}, self.service, {"video.send"})
        self.path = Path("sample.mp4")
        self.inspect = self.enterContext(patch("wxbot_framework.videos.inspect_video",
                                               return_value=SimpleNamespace(path=self.path)))

    def test_plugin_and_legacy_send_as_video(self):
        self.assertTrue(self.context.send_video("filehelper", self.path))
        self.assertTrue(self.context.reply_video(Message("", "filehelper", session_id="first"), self.path))
        self.assertTrue(LegacyService(self.service, "first").send_video("filehelper", self.path))
        self.assertEqual(self.backend.send_video.call_count, 3)

    def test_stale_session_and_missing_backend_are_rejected(self):
        with self.assertRaises(RuntimeError):
            self.context.reply_video(Message("", "filehelper", session_id="old"), self.path)
        self.backend.send_video = None
        with self.assertRaises(CapabilityError):
            self.context.send_video("filehelper", self.path)
        self.inspect.assert_not_called()

    def test_account_change_during_validation_never_sends(self):
        replacement = Mock(session_id="second")
        def switch(_):
            self.service.close()
            self.service.bind(replacement)
            return SimpleNamespace(path=self.path)
        self.inspect.side_effect = switch
        with self.assertRaises(RuntimeError):
            self.service.send_video("filehelper", self.path)
        self.backend.send_video.assert_not_called()
        replacement.send_video.assert_not_called()

    def test_only_verified_version_advertises_video(self):
        self.assertIn("video.send", TEXT_42012_CAPABILITIES)
        self.assertIn("group.video.send", TEXT_42012_CAPABILITIES)
        self.assertNotIn("video.send", TEXT_42011_CAPABILITIES)

    def test_console_preserves_quoted_windows_path(self):
        app = BotApplication.__new__(BotApplication)
        app.service = Mock()
        app.execute('send-video filehelper "C:\\测试 视频\\example.mp4"')
        app.service.send_video.assert_called_once_with("filehelper", "C:\\测试 视频\\example.mp4")


@unittest.skipUnless(sys.platform == "win32", "Windows 原生服务")
class NativeVideoTests(unittest.TestCase):
    def test_snapshot_and_duration_are_bound_to_current_account(self):
        root = Path(__file__).resolve().parents[1]
        if str(root / "native") not in sys.path:
            sys.path.insert(0, str(root / "native"))
        from text_service import NativeTextService
        account = "wxid_video_test"
        service = NativeTextService(pid=123, account=account, preserve_reports=False)
        service.router.set_account(account)
        service.wechat_version = "4.2.0.12"
        service._owns_bridge = True
        service._read_account = Mock(return_value=account)
        service._run = Mock(return_value={"success": True})
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "clip.mp4"
            source.write_bytes(mp4())
            service.file_directory = Path(directory) / "media"
            self.assertTrue(service.send_video("filehelper", source))
            options = service._run.call_args.kwargs
            self.assertEqual(options["account"], account)
            self.assertEqual(options["video_duration_seconds"], 3)
            self.assertFalse(Path(options["video_path"]).exists())

    def test_unsupported_version_never_reads_video(self):
        root = Path(__file__).resolve().parents[1]
        if str(root / "native") not in sys.path:
            sys.path.insert(0, str(root / "native"))
        from text_service import NativeTextService
        service = NativeTextService(pid=123, account="wxid_video_test", preserve_reports=False)
        service._owns_bridge = True
        service.wechat_version = "4.2.0.11"
        with patch("wxbot_framework.files.prepare_file") as prepare, self.assertRaises(RuntimeError):
            service.send_video("filehelper", "missing.mp4")
        prepare.assert_not_called()
