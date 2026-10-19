"""文件附件的会话隔离、路由和控制台集成；不操作真实微信。"""
from contextlib import contextmanager
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wxbot_framework.backend import TEXT_CAPABILITIES
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import Message, ServiceFacade, PluginContext, LegacyService, CapabilityError
from wxbot_framework.text import TextRouter

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "wxid_file_test_robot"
FRIEND = "wxid_file_test_friend"
ROOM = "1234567890@chatroom"
SOURCE = ROOT / "tests/not-created-source.txt"
PREPARED = ROOT / ".runtime/files" / ACCOUNT / "send-test/not-created-source.txt"
XML = '<msg><appmsg><title>测试.txt</title><type>6</type><appattach><totallen>4</totallen></appattach></appmsg></msg>'


class FileSDKTests(unittest.TestCase):
    def setUp(self):
        self.inspect = self.enterContext(patch("wxbot_framework.files.inspect_file",
                                               return_value=SimpleNamespace(path=SOURCE)))
        self.backend = Mock(session_id="original")
        self.backend.send_file.return_value = True
        self.service = ServiceFacade(text_delay={"enabled": False})
        self.service.bind(self.backend)
        self.addCleanup(self.service.close)
        self.context = PluginContext("test-file", {}, self.service, {"file.send"})

    def test_plugins_and_legacy_forward_session_without_text_delay(self):
        self.service.text_delay = Mock()
        self.assertTrue(self.context.send_file(FRIEND, SOURCE))
        self.assertTrue(self.context.reply_file(Message("", ROOM, session_id="original"), SOURCE))
        self.assertTrue(LegacyService(self.service, "original").send_file("filehelper", SOURCE))
        self.assertEqual(self.backend.send_file.call_count, 3)
        self.service.text_delay.seconds_for.assert_not_called()
        self.assertIn("file.send", TEXT_CAPABILITIES)
        self.assertIn("file.receive", TEXT_CAPABILITIES)

    def test_old_and_empty_sessions_are_rejected_before_file_access(self):
        for session in ("", "ended"):
            with self.subTest(session=session), self.assertRaises(RuntimeError):
                self.context.reply_file(Message("", FRIEND, session_id=session), SOURCE)
        self.inspect.assert_not_called()
        self.backend.send_file.assert_not_called()

    def test_reconnect_during_validation_cannot_switch_account(self):
        replacement = Mock(session_id="replacement")

        def inspect(path):
            self.service.close()
            self.service.bind(replacement)
            return SimpleNamespace(path=SOURCE)

        self.inspect.side_effect = inspect
        with self.assertRaises(RuntimeError):
            self.service.send_file(FRIEND, SOURCE)
        self.backend.send_file.assert_not_called()
        replacement.send_file.assert_not_called()

    def test_delivery_guard_cannot_reconnect_then_send_on_previous_account(self):
        @contextmanager
        def guard():
            self.service.close()
            self.service.bind(Mock(session_id="replacement"))
            yield

        with self.assertRaises(RuntimeError):
            self.context.reply_file(Message("", FRIEND, session_id="original"), SOURCE,
                                    delivery_guard=guard)
        self.backend.send_file.assert_not_called()

    def test_missing_capability_or_invalid_file_never_sends(self):
        self.backend.send_file = None
        with self.assertRaises(CapabilityError):
            self.service.send_file(FRIEND, SOURCE)
        self.inspect.assert_not_called()

    def test_file_events_are_not_model_text(self):
        router = TextRouter()
        router.set_account(ACCOUNT)
        data = router.convert(ROOM, ACCOUNT, FRIEND + ":\n" + XML, msg_type=49)
        message = Message.from_native(11046, data, "original")
        self.assertEqual(message.sender_id, FRIEND)
        self.assertEqual(message.chat_id, ROOM)
        self.assertEqual(message.text, "")
        self.assertEqual(message.data["raw_content"], XML)

    def test_console_preserves_quoted_windows_path(self):
        app = BotApplication.__new__(BotApplication)
        app.service = Mock()
        app.execute('send-file filehelper "C:\\测试 文件\\example.txt"')
        app.service.send_file.assert_called_once_with("filehelper", "C:\\测试 文件\\example.txt")

    def test_group_file_log_excludes_file_name_path_xml_and_content(self):
        app = BotApplication.__new__(BotApplication)
        app.config = {"chat_allowlist": [FRIEND]}
        app.members = None
        app.plugins = Mock()
        message = Message("", ROOM, sender_id=FRIEND, type=49, is_group=True,
                          data={"file_name": "secret.txt", "file_path": "sensitive/path",
                                "raw_content": XML, "file_status": "ready"})
        with self.assertLogs("Bot", level="INFO") as logs:
            app._emit(message)
        text = "\n".join(logs.output)
        self.assertIn(ROOM, text)
        for secret in ("secret.txt", "sensitive/path", XML):
            self.assertNotIn(secret, text)


@unittest.skipUnless(sys.platform == "win32", "Windows 原生服务")
class FileServiceTests(unittest.TestCase):
    def setUp(self):
        if str(ROOT / "native") not in sys.path:
            sys.path.insert(0, str(ROOT / "native"))
        from text_service import NativeTextService
        self.service = NativeTextService(pid=123, account=ACCOUNT, preserve_reports=False)
        self.service.router.set_account(ACCOUNT)
        self.service._owns_bridge = True
        self.service._read_account = Mock(return_value=ACCOUNT)
        self.service._run = Mock(return_value={"success": True})
        self.events = []

        @contextmanager
        def prepare(path, directory):
            self.events.append("prepare")
            try:
                yield SimpleNamespace(path=PREPARED, size=4, name="test.txt")
            finally:
                self.events.append("release")

        self.prepare = self.enterContext(patch("wxbot_framework.files.prepare_file", side_effect=prepare))

    def test_snapshot_remains_alive_through_native_send(self):
        def dispatch(*args, **options):
            self.assertEqual(self.events, ["prepare"])
            self.assertEqual(options["account"], ACCOUNT)
            self.assertEqual(options["file_path"], str(PREPARED))
            return {"success": True}

        self.service._run.side_effect = dispatch
        self.assertTrue(self.service.send_file("filehelper", SOURCE))
        self.assertEqual(self.events, ["prepare", "release"])
        self.prepare.assert_called_once_with(SOURCE, ROOT / ".runtime/files" / ACCOUNT)

    def test_account_change_while_preparing_cancels_dispatch_and_releases_snapshot(self):
        self.service._read_account.side_effect = [ACCOUNT, "wxid_replacement"]
        with self.assertRaises(RuntimeError):
            self.service.send_file(FRIEND, SOURCE)
        self.service._run.assert_not_called()
        self.assertEqual(self.events, ["prepare", "release"])

    def test_file_xml_uses_v3_buffer_and_respects_allowed_chats(self):
        from filehelper_service import ReceivedMediaMessage
        self.service._file_receiver = Mock()
        self.service._sender_wechat_name = Mock(return_value=None)
        self.service.image_allowlist = frozenset([FRIEND])
        event = ReceivedMediaMessage(type=49, sender=FRIEND.encode(), receiver=ACCOUNT.encode(),
                                     image_xml=XML.encode("utf-8"))
        self.assertIsNone(self.service._map_message(event))
        data = self.service._file_receiver.submit.call_args.args[0]
        self.assertEqual(data["raw_content"], XML)
        self.assertEqual(data["wx_type"], 49)
        self.service._file_receiver.reset_mock()
        event.sender = b"wxid_unlisted"
        self.assertIsNone(self.service._map_message(event))
        self.service._file_receiver.submit.assert_not_called()

    def test_busy_download_prevents_dll_unload(self):
        self.service._file_receiver = Mock(is_alive=True)
        with patch("filehelper_service.NativeFileHelperService.stop") as stop:
            with self.assertRaisesRegex(RuntimeError, "保留桥接"):
                self.service.stop()
            stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
