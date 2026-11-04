import unittest

from wxbot_framework.linux_messages import LinuxTextAdapter


class LinuxTextAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = LinuxTextAdapter("wxid_self_fixture", "fixture-session")

    def test_private_message_preserves_account_session_and_long_text(self):
        text = "很长的消息🙂\n" * 3000 + "末尾"
        msg = self.adapter.convert({"sender": "wxid_friend_fixture", "receiver": "wxid_self_fixture", "text": text, "sequence": 19})
        self.assertEqual((msg.chat_id, msg.sender_id, msg.receiver_id),
                         ("wxid_friend_fixture", "wxid_friend_fixture", "wxid_self_fixture"))
        self.assertEqual(msg.text, text)
        self.assertEqual(msg.sequence, 19)
        self.assertEqual(msg.session_id, "fixture-session")
        self.assertFalse(msg.is_group)

    def test_group_strips_member_prefix_and_preserves_sender(self):
        msg = self.adapter.convert({"sender": "123456789@chatroom", "receiver": "wxid_self_fixture", "text": "wxid_friend_fixture:\n你好"})
        self.assertTrue(msg.is_group)
        self.assertEqual((msg.chat_id, msg.sender_id, msg.text),
                         ("123456789@chatroom", "wxid_friend_fixture", "你好"))

    def test_group_mentions_require_source_metadata(self):
        event = {"sender": "123456789@chatroom", "receiver": "wxid_self_fixture", "text": "wxid_friend_fixture:\n@昵称\u2005你好"}
        self.assertEqual(self.adapter.convert(event).data.get("at_members"), ())
        event["msgsource"] = "<msgsource><atuserlist>wxid_self_fixture,wxid_other_fixture</atuserlist></msgsource>"
        self.assertEqual(self.adapter.convert(event).data.get("at_members"), ("wxid_self_fixture", "wxid_other_fixture"))
        self.assertTrue(self.adapter.convert(event).data.get("is_at_self"))
        event["msgsource"] = "<!DOCTYPE x [<!ENTITY e 'wxid_self_fixture'>]><msgsource><atuserlist>&e;</atuserlist></msgsource>"
        self.assertEqual(self.adapter.convert(event).data.get("at_members"), ())

    def test_wrong_account_and_missing_group_prefix_are_ignored(self):
        self.assertIsNone(self.adapter.convert({"sender": "wxid_friend_fixture", "receiver": "wxid_other_fixture", "text": "你好"}))
        self.assertIsNone(self.adapter.convert({"sender": "123456789@chatroom", "receiver": "wxid_self_fixture", "text": "没有成员前缀"}))

    def test_self_private_message_and_nontext_not_dispatched(self):
        self.assertIsNone(self.adapter.convert({"sender": "wxid_self_fixture", "receiver": "wxid_friend_fixture", "text": "你好"}))
        with self.assertRaises(ValueError):
            self.adapter.convert({"sender": "wxid_friend_fixture", "receiver": "wxid_self_fixture", "text": "x", "type": 3})

    def test_invalid_event_data_is_rejected(self):
        with self.assertRaises(ValueError):
            self.adapter.convert({"sender": "wxid_friend_fixture", "receiver": "wxid_self_fixture", "text": b"x"})
        with self.assertRaises(ValueError):
            self.adapter.convert({"sender": "wxid_friend_fixture", "receiver": "wxid_self_fixture", "text": "x", "sequence": True})
        with self.assertRaises(ValueError):
            LinuxTextAdapter("wxid_self_fixture", "")


if __name__ == "__main__":
    unittest.main()
