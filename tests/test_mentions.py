"""真正艾特必须同时携带原生成员 ID 与可见标签，并保留会话限制。"""
import unittest
from unittest.mock import patch

from wxbot_framework.sdk import CapabilityError, Message, PluginContext, ServiceFacade
from wxbot_framework.text import validate_mentions

ROOM = "12345678@chatroom"
MEMBER = "wxid_test_member"


class Backend:
    session_id = "one"

    def send_at_text(self, room, text, members):
        self.sent = (room, text, members)
        return True

    def send_text(self, room, text):
        self.continuation = (room, text)
        return True

    def stop(self):
        return {"dll_unloaded": True}


class MentionTests(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.service = ServiceFacade(text_delay={"enabled": False})
        self.service.bind(self.backend)

    def tearDown(self):
        self.service.close()

    def test_nickname_and_actual_member_are_separate(self):
        self.service.send_at_text(ROOM, "测试", [MEMBER], display_names=["群昵称"])
        self.assertEqual(self.backend.sent, (ROOM, "@群昵称\u2005\n测试", (MEMBER,)))

    def test_reply_at_keeps_session_and_uses_sender(self):
        ctx = PluginContext("example", {}, self.service, [])
        message = Message("测试", ROOM, MEMBER, is_group=True, session_id="one")
        ctx.reply_at(message, "成功", display_name="小明")
        self.assertEqual(self.backend.sent[2], (MEMBER,))
        with self.assertRaises(RuntimeError):
            ctx.reply_at(Message("旧消息", ROOM, MEMBER, is_group=True, session_id="old"), "跳过")
        with self.assertRaises(ValueError):
            ctx.reply_at(Message("私聊", MEMBER, MEMBER), "跳过")

    def test_missing_native_ability_is_not_plain_text_at(self):
        self.backend.send_at_text = None
        with self.assertRaises(CapabilityError):
            self.service.send_at_text(ROOM, "测试", [MEMBER])

    def test_invalid_members_and_labels(self):
        for room, members in ((MEMBER, [MEMBER]), (ROOM, []), (ROOM, [MEMBER, MEMBER]),
                              (ROOM, ["filehelper"]), (ROOM, ["其他@chatroom"]), (ROOM, ["中文"]),
                              (ROOM, ["wxid_" + str(index) for index in range(9)])):
            with self.subTest(members=members), self.assertRaises(ValueError):
                validate_mentions(room, members)
        for names in ([], [""], ["换\n行"], ["x" * 65]):
            with self.subTest(names=names), self.assertRaises(ValueError):
                self.service.send_at_text(ROOM, "测试", [MEMBER], display_names=names)

    def test_long_mentions_keep_labels_in_first_segment(self):
        self.service.send_at_text(ROOM, "文" * 341, [MEMBER], display_names=["小明"])
        self.assertEqual(self.backend.sent[1] + self.backend.continuation[1], '@小明\u2005\n' + '文' * 341)
        self.assertLessEqual(len(self.backend.sent[1].encode('utf-8')), 1023)

    def test_pacing_applies_to_mention_text(self):
        with patch.object(type(self.service.text_delay), "seconds_for", return_value=0) as delay:
            self.service.send_at_text(ROOM, "成功", [MEMBER], display_names=["小明"])
        self.assertEqual(delay.call_args.args[0], "@小明\u2005\n成功")
