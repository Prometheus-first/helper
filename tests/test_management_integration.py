"""好友与群管理的控制台、会话隔离和自动插件边界；不操作微信。"""
from pathlib import Path
import json
import shutil
import sys
import tempfile
import unittest
from unittest.mock import Mock

from wxbot_framework.group_management import validate_group_id, validate_group_members
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import Message, PluginContext, ServiceFacade
from wxbot_framework.friend_requests import PendingFriendRequests, parse_friend_request
from wxbot_framework.config import ConfigStore
from wxbot_framework.plugins import PluginManager
from plugins.friend_auto_accept import Plugin

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "wxid_management_robot"
FIRST = "wxid_management_first"
SECOND = "wxid_management_second"
ROOM = "123456789@chatroom"
XML = ('<msg fromusername="wxid_management_first" fromnickname="测试好友" '
       'content="测试验证词" scene="30" opcode="2" encryptusername="v3_SYNTHETIC_USER" '
       'ticket="v4_SYNTHETIC_TICKET"/>')


class ManagementIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock(session_id="original")
        self.backend.create_group.return_value = ROOM
        self.backend.invite_group_members.return_value = True
        self.service = ServiceFacade(text_delay={"enabled": False})
        self.service.bind(self.backend)
        self.addCleanup(self.service.close)

    def test_group_inputs_reject_non_groups_system_peers_duplicates_and_self(self):
        for room in (FIRST, "", "测试群"):
            with self.subTest(room=room), self.assertRaises(ValueError):
                validate_group_id(room)
        for members, creating in (([], False), ([FIRST], True), ([FIRST, FIRST], True),
                                  (["filehelper"], False), ([ROOM], False), ([".."], False),
                                  ([FIRST] * 9, False)):
            with self.subTest(members=members), self.assertRaises(ValueError):
                validate_group_members(members, creating=creating)
        with self.assertRaises(ValueError):
            validate_group_members([ACCOUNT], self_wxid=ACCOUNT)

    def test_management_is_not_subject_to_text_pacing_and_uses_original_connection(self):
        self.service.text_delay = Mock()
        self.assertTrue(self.service.invite_group_members(ROOM, [FIRST]))
        self.assertEqual(self.service.create_group([FIRST, SECOND]), ROOM)
        self.backend.invite_group_members.assert_called_once_with(ROOM, (FIRST,))
        self.backend.create_group.assert_called_once_with((FIRST, SECOND))
        self.service.text_delay.seconds_for.assert_not_called()
        for session in ("", "ended"):
            with self.subTest(session=session), self.assertRaises(RuntimeError):
                self.service.create_group([FIRST, SECOND], expected_session=session)

    def test_old_plugin_request_cannot_accept_from_replacement_account(self):
        context = PluginContext("friend_auto_accept", {}, self.service, {"friend.request.accept"})
        self.service.close()
        replacement = Mock(session_id="replacement")
        self.service.bind(replacement)
        with self.assertRaises(RuntimeError):
            context.accept_friend("fr_" + "a" * 32, expected_session="original")
        replacement.accept_friend.assert_not_called()

    def test_console_alone_exposes_group_actions(self):
        app = BotApplication.__new__(BotApplication)
        app.service = self.service
        app.execute("invite-members " + ROOM + " " + FIRST)
        app.execute("create-group " + FIRST + " " + SECOND)
        self.backend.invite_group_members.assert_called_once_with(ROOM, (FIRST,))
        self.backend.create_group.assert_called_once_with((FIRST, SECOND))
        context = PluginContext("test", {}, self.service, set())
        self.assertFalse(hasattr(context, "create_group"))
        self.assertFalse(hasattr(context, "invite_group_members"))

    def test_friend_events_bypass_chat_allowlist_without_recording_history_or_credentials(self):
        app = BotApplication.__new__(BotApplication)
        app.config = {"chat_allowlist": [ROOM]}
        app.members, app.plugins = Mock(), Mock()
        catalog = PendingFriendRequests(ACCOUNT, "original")
        public = catalog.add(parse_friend_request(XML))
        message = Message.from_native(11046, {"wx_type": 37, "chat_id": "fmessage",
            "sender_id": FIRST, "friend_request": public}, "original")
        with self.assertLogs("Bot", level="INFO") as logs:
            app._emit(message)
        app.plugins.dispatch.assert_called_once_with(message)
        app.members.record.assert_not_called()
        log = "\n".join(logs.output)
        self.assertIn(public["request_id"], log)
        for secret in ("v3_SYNTHETIC_USER", "v4_SYNTHETIC_TICKET", XML, "测试验证词"):
            self.assertNotIn(secret, log)


class AutoAcceptPluginTests(unittest.TestCase):
    def test_existing_config_without_new_plugin_entry_remains_disabled_until_explicit_enable(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        directory = root / "plugins/friend_auto_accept"
        directory.mkdir(parents=True)
        for name in ("__init__.py", "plugin.json"):
            shutil.copyfile(ROOT / "plugins/friend_auto_accept" / name, directory / name)
        path = root / "bot_config.json"
        path.write_text(json.dumps({"backend": "native_text", "plugins": {}}), encoding="utf-8")
        manager = PluginManager(root / "plugins", ConfigStore(path), Mock(),
                                {"friend.request.receive", "friend.request.accept"})
        self.addCleanup(manager.shutdown)
        manager.reload()
        self.assertFalse(manager.status()[0]["enabled"])
        self.assertFalse(manager.is_active("friend_auto_accept"))
        self.assertEqual(manager.load_errors, {})
        manager.enable("friend_auto_accept")
        self.assertTrue(manager.is_active("friend_auto_accept"))
        self.assertTrue(ConfigStore(path).load()["plugins"]["friend_auto_accept"]["enabled"])

    def test_matching_request_only_and_preserves_session(self):
        context = Mock(config={"verification_keywords": ["测试"]})
        plugin = Plugin()
        plugin.on_load(context)
        public = PendingFriendRequests(ACCOUNT, "original").add(parse_friend_request(XML))
        message = Message("", "fmessage", type=37, session_id="original", data={"friend_request": public})
        plugin.on_message(message)
        context.accept_friend.assert_called_once_with(public["request_id"], expected_session="original")
        context.accept_friend.reset_mock()
        plugin.on_message(Message("create-group a b", ROOM))
        plugin.on_message(Message("", "fmessage", type=37,
            data={"friend_request": dict(public, verification="其他内容")}))
        plugin.on_message(Message("", "fmessage", type=37,
            data={"friend_request": dict(public, status="accepted")}))
        context.accept_friend.assert_not_called()

    def test_exception_cannot_write_request_credentials_to_log(self):
        context = Mock(config={})
        plugin = Plugin()
        plugin.on_load(context)
        context.accept_friend.side_effect = RuntimeError("v4_SECRET_SHOULD_NOT_LOG")
        plugin.on_message(Message("", "fmessage", type=37, session_id="original",
            data={"friend_request": {"request_id": "fr_" + "a" * 32, "status": "pending"}}))
        self.assertNotIn("v4_SECRET_SHOULD_NOT_LOG", str(context.logger.warning.call_args))


@unittest.skipUnless(sys.platform == "win32", "Windows 原生服务")
class ManagementServiceTests(unittest.TestCase):
    def setUp(self):
        if str(ROOT / "native") not in sys.path:
            sys.path.insert(0, str(ROOT / "native"))
        from text_service import NativeTextService
        self.service = NativeTextService(pid=123, account=ACCOUNT)
        self.service.router.set_account(ACCOUNT)
        self.service._owns_bridge = True
        self.service._read_account = Mock(return_value=ACCOUNT)
        self.service._friend_session_id = "original"
        self.service._friend_requests = PendingFriendRequests(ACCOUNT, "original")
        self.service._run = Mock(return_value={"success": True, "report": {"room_id": ROOM}})

    def test_request_credentials_stay_inside_service_and_duplicates_do_not_accept_twice(self):
        from filehelper_service import ReceivedMediaMessage
        raw = ReceivedMediaMessage(type=37, sender=b"fmessage", receiver=ACCOUNT.encode(), image_xml=XML.encode())
        event = self.service._map_message(raw)
        self.assertEqual(event["wx_type"], 37)
        for secret in ("v3_SYNTHETIC_USER", "v4_SYNTHETIC_TICKET", XML):
            self.assertNotIn(secret, str(event))
        request_id = event["friend_request"]["request_id"]
        self.assertTrue(self.service.accept_friend(request_id))
        self.assertTrue(self.service.accept_friend(request_id))
        self.service._run.assert_called_once()
        self.assertFalse(self.service._map_message(raw)["dispatch_to_plugins"])

    def test_result_notification_and_other_receiver_are_not_pending_requests(self):
        from filehelper_service import ReceivedMediaMessage
        for body, receiver in ((XML.replace('opcode="2"', 'opcode="3"'), ACCOUNT), (XML, SECOND)):
            event = ReceivedMediaMessage(type=37, sender=b"fmessage", receiver=receiver.encode(), image_xml=body.encode())
            self.assertIsNone(self.service._map_message(event))
        self.assertEqual(self.service.friend_requests(), [])

    def test_account_change_never_invites_creates_or_accepts_from_other_login(self):
        self.service._read_account.return_value = SECOND
        with self.assertRaises(RuntimeError):
            self.service.invite_group_members(ROOM, [FIRST])
        with self.assertRaises(RuntimeError):
            self.service.create_group([FIRST, SECOND])
        with self.assertRaises(RuntimeError):
            self.service.accept_friend("fr_" + "a" * 32)
        self.service._run.assert_not_called()

    def test_creation_requires_confirmed_group_id(self):
        self.assertEqual(self.service.create_group([FIRST, SECOND]), ROOM)
        self.assertIn(ROOM, self.service.chats)
        self.service._run.return_value = {"success": True, "report": {"room_id": FIRST}}
        with self.assertRaises(ValueError):
            self.service.create_group([FIRST, SECOND])
        self.service._run.return_value = {"success": False}
        with self.assertRaises(RuntimeError):
            self.service.create_group([FIRST, SECOND])


if __name__ == "__main__":
    unittest.main()
