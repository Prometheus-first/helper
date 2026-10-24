"""验证独立接龙、合并首答和发送失败时的状态，使用内存上下文。"""
import logging
import time
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from plugins.idiom_chain import Plugin, HELP
from wxbot_framework.llm import LLMError
from wxbot_framework.sdk import Message
from wxbot_framework.personality import PersonalityState

ROOM = "12345678@chatroom"
A, B = "wxid_player_a", "wxid_player_b"


class JSONClient:
    def __init__(self, result):
        self.result = result

    def complete_json(self, system, user):
        return self.result


class Context:
    def __init__(self):
        self.config = {"member_names": {ROOM: {A: "小明"}}}
        self.llm = JSONClient({"idiom": "一心一意"})
        self.logger = logging.getLogger("IdiomTests")
        self.sent = []
        self.succeeds = True

    def personality(self, message):
        return PersonalityState(message.chat_id)

    def llm_for(self, message, *, personality=None):
        return self.llm

    def personality_guard(self, state):
        return nullcontext()

    def reply(self, message, text, *, delivery_guard=None):
        self.sent.append((message.chat_id, text, None, None))
        return self.succeeds

    def reply_at(self, message, text, *, display_name=None, delivery_guard=None):
        self.sent.append((message.chat_id, text, message.sender_id, display_name))
        return self.succeeds


class IdiomPluginTests(unittest.TestCase):
    def setUp(self):
        self.ctx = Context()
        self.plugin = Plugin()
        self.plugin.on_load(self.ctx)

    def send(self, text, sender=A, room=ROOM, *, session="one", received_at=None, is_self=False, is_group=True):
        message = Message(text, room, sender_id=sender, session_id=session, is_group=is_group,
                          is_self=is_self, received_at=time.monotonic() if received_at is None else received_at)
        self.plugin.on_message(message, self.ctx)
        return message

    def start(self):
        self.send("成语接龙")
        self.ctx.llm.result = {"is_idiom": True, "reason": "常见成语", "next_idiom": "发扬光大"}

    def test_two_players_are_independent_and_use_member_names(self):
        self.start()
        # 两个答案都在机器人第一次回复之前到达：B 仍应从开场词接。
        b_received = time.monotonic()
        self.send("意气风发", A)
        self.send("意气风发", B, received_at=b_received)
        game = self.plugin.games[ROOM]
        self.assertEqual(game.players[A].current, "发扬光大")
        self.assertEqual(game.players[B].current, "发扬光大")
        self.assertEqual(self.ctx.sent[-2][2:], (A, "小明"))
        self.assertEqual(self.ctx.sent[-1][2:], (B, None))
        self.ctx.llm.result["next_idiom"] = "方兴未艾"
        self.send("大大方方", A)
        self.assertEqual(game.players[A].current, "方兴未艾")
        self.assertEqual(game.players[B].current, "发扬光大")

    def test_groups_do_not_share_game(self):
        self.start()
        self.send("意气风发", room="87654321@chatroom")
        self.assertEqual(len(self.ctx.sent), 1)

    def test_starting_picture_game_clears_chain_only_when_plugin_is_active(self):
        self.start()
        self.ctx.plugin_is_active = lambda name: name == 'picture_idiom'
        self.send('看图猜成语')
        self.assertNotIn(ROOM, self.plugin.games)
        self.assertEqual(len(self.ctx.sent), 1)

    def test_bad_answer_keeps_current_question(self):
        self.start()
        self.send("异想天开")
        self.assertEqual(self.plugin.games[ROOM].players[A].current, "一心一意")
        self.assertIn("接龙失败", self.ctx.sent[-1][1])

    def test_merged_mode_uses_latest_word_and_only_first_received_answer(self):
        self.start()
        self.send("意气风发")
        self.send("合并接龙")
        game = self.plugin.games[ROOM]
        self.assertEqual(game.shared.current, "发扬光大")
        self.assertEqual(game.players, {})
        arrived = time.monotonic()
        self.ctx.llm.result["next_idiom"] = "方兴未艾"
        self.send("大大方方", A, received_at=arrived)
        count = len(self.ctx.sent)
        self.send("大大方方", B, received_at=arrived + 0.00000001)
        self.assertEqual(len(self.ctx.sent), count)
        self.assertIsNone(self.ctx.sent[-1][2])
        self.assertEqual(game.shared.current, "方兴未艾")

    def test_failed_merged_answer_still_consumes_round(self):
        self.start()
        self.send("合并接龙")
        arrived = time.monotonic()
        self.send("异想天开", received_at=arrived)
        count = len(self.ctx.sent)
        self.send("意气风发", B, received_at=arrived)
        self.assertEqual(len(self.ctx.sent), count)
        self.assertEqual(self.plugin.games[ROOM].shared.current, "一心一意")
        self.send("意气风发", B)
        self.assertEqual(self.plugin.games[ROOM].shared.current, "发扬光大")

    def test_exit_and_restart(self):
        self.start()
        self.send("退出接龙")
        self.assertNotIn(ROOM, self.plugin.games)
        count = len(self.ctx.sent)
        self.send("意气风发")
        self.assertEqual(len(self.ctx.sent), count)
        self.ctx.llm.result = {"idiom": "一心一意"}
        self.send("成语接龙")
        self.assertIn(ROOM, self.plugin.games)

    def test_help_and_primary_phone_commands(self):
        self.send("/help", is_self=True)
        self.assertEqual(self.ctx.sent[-1][1], HELP)
        self.assertIn("\n\n2. 看图猜成语", HELP)
        self.assertIn("\n\n8. /查看音色\n\n9. /音色 名称|关闭", HELP)
        self.assertIn("11. /来张图", HELP)
        self.assertTrue(HELP.endswith("12. /help"))
        self.assertLessEqual(len(HELP.encode("utf-8")), 1023)
        self.send("成语接龙", is_self=True)
        self.assertEqual(self.ctx.sent[-1][1], "一心一意")
        self.assertIn(ROOM, self.plugin.games)
        self.ctx.llm.result = {"is_idiom": True, "reason": "常见成语", "next_idiom": "发扬光大"}
        self.send("意气风发", is_self=True)
        self.assertEqual(self.plugin.games[ROOM].players[A].current, "发扬光大")

    def test_own_long_reply_does_not_create_loop_and_private_is_ignored(self):
        self.start()
        count = len(self.ctx.sent)
        self.send("接龙成功！一心一意 → 意气风发", is_self=True)
        self.send("/help", is_group=False)
        self.assertEqual(len(self.ctx.sent), count)

    def test_reconnect_resets_game(self):
        self.start()
        self.send("意气风发", session="two")
        self.assertEqual(self.plugin.games, {})

    def test_failed_send_does_not_advance_question(self):
        self.start()
        self.ctx.succeeds = False
        self.send("意气风发")
        self.assertEqual(self.plugin.games[ROOM].players, {})
        self.send("合并接龙")
        self.assertEqual(self.plugin.games[ROOM].mode, "independent")

    def test_model_failure_keeps_question(self):
        self.start()
        with patch("plugins.idiom_chain.IdiomJudge.evaluate", side_effect=LLMError("测试超时")), self.assertLogs("IdiomTests"):
            self.send("意气风发")
        self.assertEqual(self.plugin.games[ROOM].players[A].current, "一心一意")
        self.assertIn("重试", self.ctx.sent[-1][1])

    def test_model_opening_failure_does_not_create_game(self):
        with patch("plugins.idiom_chain.IdiomJudge.opening", side_effect=LLMError("测试超时")), self.assertLogs("IdiomTests"):
            self.send("成语接龙")
        self.assertEqual(self.plugin.games, {})

    def test_invalid_member_name_configuration(self):
        for options in ({"member_names": []}, {"member_names": {ROOM: {A: "\n"}}},
                        {"member_names": {ROOM: {A: "A" * 65}}}):
            self.ctx.config = options
            with self.subTest(options=options), self.assertRaises(ValueError):
                Plugin().on_load(self.ctx)
