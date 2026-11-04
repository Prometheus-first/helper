"""群人格持久性、统一风格、真实艾特与十条上下文及取消竞态。"""
import json
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import test_chat_summary as daily
from plugins.mention_chat import Plugin as ChatPlugin
from plugins.personality import Plugin as PersonalityPlugin
from plugins.idiom_chain import Plugin as IdiomPlugin
from wxbot_framework.dialogue import Dialogue
from wxbot_framework.members import MemberError, SnapshotExpired
from wxbot_framework.mentions import parse_at_members, strip_at_labels
from wxbot_framework.personality import PROMPTS, PersonalityExpired, PersonalityState, prepare_switch, game_reply
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import Message
from wxbot_framework.text import TextRouter

ROOM, CAT, ROBOT, OTHER_ROOM, OTHER = daily.ROOM, daily.CAT, daily.ROBOT, daily.OTHER_ROOM, daily.OTHER


class PersonalityTests(unittest.TestCase):
    setUp = daily.DailySummaryTests.setUp
    tearDown = daily.DailySummaryTests.tearDown
    new_store = daily.DailySummaryTests.new_store
    message = daily.DailySummaryTests.message
    model = daily.DailySummaryTests.model
    snapshot = daily.DailySummaryTests.snapshot

    def chat(self, text="你怎么看？", *, sequence=100, sender=CAT, at_members=(ROBOT,), **kwargs):
        msg = self.message("@机器人\u2005" + text, sequence=sequence, sender=sender,
                           data={"at_members": at_members, "is_at_self": True, **kwargs})
        self.store.record(msg)
        return msg

    def setup_chat(self, provider=None):
        self.backend.send_at_text = lambda room, text, ids: self.backend.sent.append((room, text, ids)) or True
        self.ctx._llm = Mock(complete_json=provider or Mock(return_value={"reply": "这是回答。", "summary": "这是总结。"}))
        plugin = ChatPlugin()
        plugin.on_load(self.ctx)
        return plugin

    def test_personality_persists_and_isolated_from_other_groups_accounts_and_clear(self):
        state = self.store.set_personality(ROOM, "嘴臭")
        self.assertEqual(self.store.personality(OTHER_ROOM).name, "默认")
        self.assertEqual(self.new_store("wxid_second_robot").personality(ROOM).name, "默认")
        self.store.clear(ROOM)
        self.now += timedelta(seconds=1)
        self.store.rollover()
        self.assertEqual(self.store.personality(ROOM), state)
        self.assertEqual(self.new_store().personality(ROOM), state)

    def test_same_selection_keeps_generation_unknown_name_does_not_change_it(self):
        state = self.store.set_personality(ROOM, "卑微")
        self.assertEqual(self.store.set_personality(ROOM, "卑微"), state)
        with self.assertRaises(MemberError):
            self.store.set_personality(ROOM, "不存在")
        self.assertEqual(self.store.personality(ROOM), state)
        self.assertGreater(self.store.set_personality(ROOM, "夸夸").generation, state.generation)

    def test_cancel_removes_style_persists_and_keeps_records_and_other_groups(self):
        initial = self.store.personality(ROOM)
        repeated = prepare_switch(self.message("/切换人格 取消"), self.store)
        self.assertEqual(repeated.data["personality_switch"], initial)
        self.store.record(self.message("今天写了文档", sequence=1))
        old = self.store.set_personality(ROOM, "嘴臭")
        other = self.store.set_personality(OTHER_ROOM, "夸夸")
        prepared = prepare_switch(self.message("/切换人格 取消"), self.store)
        state = prepared.data["personality_switch"]
        self.assertEqual(state.name, "默认")
        self.assertGreater(state.generation, old.generation)
        plugin = PersonalityPlugin()
        plugin.on_load(self.ctx)
        plugin.on_message(prepared, self.ctx)
        self.assertIn("已取消人格", self.backend.sent[-1][1])
        self.assertEqual(len(self.store.today_snapshot("测试成员", ROOM).messages), 1)
        self.assertEqual(self.store.personality(OTHER_ROOM), other)
        self.assertEqual(self.new_store().personality(ROOM), state)
        self.assertEqual(prepare_switch(self.message("/切换人格 取消"), self.store).data["personality_switch"], state)
        provider = Mock(return_value={"reply": "正常回答"})
        self.ctx._llm = Mock(complete_json=provider)
        self.ctx.llm_for(self.message("问题")).complete_json("原功能提示", "输入")
        self.assertEqual(provider.call_args.args[0], "原功能提示")

    def test_cancel_during_model_request_discards_old_personality_reply(self):
        state = self.store.set_personality(ROOM, "嘴臭")
        def cancel(system, user):
            prepare_switch(self.message("/切换人格 取消"), self.store)
            return {"reply": "旧人格回答"}
        self.ctx._llm = Mock(complete_json=cancel)
        client = self.ctx.llm_for(self.message("问题"), personality=state)
        with self.assertRaises(PersonalityExpired):
            client.complete_json("功能", "输入")
        with self.assertRaises(PersonalityExpired):
            with self.store.personality_delivery_guard(state):
                self.fail("取消后不能发送旧人格回答")
        self.assertEqual(self.store.personality(ROOM).name, "默认")

    def test_runtime_switches_before_dispatch_and_ack_uses_same_state(self):
        app = object.__new__(BotApplication)
        app.config, app.members, app.plugins = {"chat_allowlist": []}, self.store, Mock()
        app._emit(self.message("/切换人格 嘴臭"))
        delivered = app.plugins.dispatch.call_args.args[0]
        self.assertEqual(self.store.personality(ROOM).name, "嘴臭")
        self.assertEqual(delivered.data["personality_switch"].name, "嘴臭")
        plugin = PersonalityPlugin()
        plugin.on_load(self.ctx)
        plugin.on_message(delivered, self.ctx)
        self.assertIn("已切换：嘴臭", self.backend.sent[-1][1])

    def test_missing_or_unknown_switch_returns_usage_without_changing_persona(self):
        plugin = PersonalityPlugin()
        plugin.on_load(self.ctx)
        for command in ("/切换人格", "/切换人格 未知", "/切换人格 嘴臭 卑微"):
            prepared = prepare_switch(self.message(command), self.store)
            plugin.on_message(prepared, self.ctx)
            self.assertIn("用法", self.backend.sent[-1][1])
        self.assertEqual(self.store.personality(ROOM).name, "默认")

    def test_disabled_personality_plugin_does_not_apply_switch(self):
        app = object.__new__(BotApplication)
        app.config, app.members, app.plugins = {"chat_allowlist": []}, self.store, Mock()
        app.plugins.is_active.return_value = False
        app._emit(self.message("/切换人格 嘴臭"))
        self.assertEqual(self.store.personality(ROOM).name, "默认")
        prepare_switch(self.message("/切换人格 嘴臭", data={"bot_generated": True}), self.store)
        self.assertEqual(self.store.personality(ROOM).name, "默认")

    def test_owner_phone_can_switch_and_default_style_does_not_modify_system(self):
        router = TextRouter(include_self_groups=True)
        router.set_account(ROBOT)
        data = router.convert(ROOM, ROBOT, ROBOT + ":\n/切换人格 夸夸")
        self.assertTrue(data["dispatch_to_plugins"])
        provider = Mock(return_value={"reply": "回答"})
        self.ctx._llm = Mock(complete_json=provider)
        self.ctx.llm_for(self.message("问题")).complete_json("原功能提示", "输入")
        self.assertEqual(provider.call_args.args[0], "原功能提示")

    def test_personality_prompt_applies_to_member_and_group_summaries(self):
        self.store.record(self.message("今天要认真工作"))
        for name in PROMPTS:
            self.store.set_personality(ROOM, name)
            provider = Mock(return_value={"summary": "总结内容"})
            self.ctx._llm = Mock(complete_json=provider)
            for command in ("/总结 测试成员", "/总结群聊"):
                self.plugin.on_message(self.message(command), self.ctx)
                system = provider.call_args.args[0]
                self.assertTrue(system.startswith(f"当前人格：{name}"))
                self.assertIn("最高优先级", system)
                self.assertIn("JSON", system)

    def test_personality_change_during_model_request_discards_summary(self):
        self.store.record(self.message("当天记录"))
        self.store.set_personality(ROOM, "嘴臭")
        def change(system, user):
            self.store.set_personality(ROOM, "卑微")
            return {"summary": "旧人格结果"}
        self.ctx._llm = Mock(complete_json=change)
        self.plugin.on_message(self.message("/总结群聊"), self.ctx)
        self.assertEqual(self.backend.sent, [])

    def test_old_llm_session_does_not_call_provider_after_switch(self):
        state = self.store.set_personality(ROOM, "嘴臭")
        provider = Mock(return_value={"reply": "旧结果"})
        self.ctx._llm = Mock(complete_json=provider)
        client = self.ctx.llm_for(self.message("问题"), personality=state)
        self.store.set_personality(ROOM, "卑微")
        with self.assertRaises(PersonalityExpired):
            client.complete_json("功能", "输入")
        provider.assert_not_called()

    def test_context_is_previous_ten_from_same_author_and_room_not_later_messages(self):
        for index in range(12):
            self.store.record(self.message(f"前文{index}", sequence=index + 1))
        self.store.record(self.message("其他人的话", sender=OTHER))
        self.store.record(self.message("其他群的话", room=OTHER_ROOM))
        self.store.record(self.message("/切换人格 嘴臭"))
        self.store.record(self.message("机器人输出", data={"bot_generated": True}))
        msg = self.chat("当前提问")
        self.store.record(self.message("当前提问之后才收到的消息", sequence=101))
        snapshot = self.store.conversation_snapshot(msg)
        self.assertEqual([row["text"] for row in snapshot.messages], [f"前文{i}" for i in range(2, 12)])
        self.assertEqual(snapshot.current, msg.text)

    def test_true_mention_dialogue_uses_persona_context_and_returns_real_at(self):
        self.store.set_personality(ROOM, "嘴臭")
        for index in range(3):
            self.store.record(self.message(f"我今天第{index}次摸鱼", sequence=index + 1))
        provider = Mock(return_value={"reply": "你这效率，进度条看了都想请假。"})
        plugin = self.setup_chat(provider)
        msg = self.chat("我今天又摸鱼了")
        plugin.on_message(msg, self.ctx)
        system, user = provider.call_args.args
        self.assertTrue(system.startswith("当前人格：嘴臭"))
        payload = json.loads(user)
        self.assertEqual(len(payload["previous"]), 3)
        self.assertEqual(payload["current"], "我今天又摸鱼了")
        self.assertNotIn(CAT, user)
        self.assertEqual(self.backend.sent[-1][2], (CAT,))
        self.assertTrue(self.backend.sent[-1][1].startswith("@测试成员\u2005"))

    def test_short_mouth_dialogue_is_plain_and_keeps_real_at_and_context(self):
        self.store.set_personality(ROOM, "嘴臭")
        self.store.record(self.message("今天写了三行代码", sequence=1))
        provider = Mock(return_value={"reply": "```text\n**三行代码就敢吹，先把手上的任务搞定再说。**\n```"})
        plugin = self.setup_chat(provider)
        plugin.on_message(self.chat("我已经很强了"), self.ctx)
        sent = self.backend.sent[-1]
        body = strip_at_labels(sent[1])
        self.assertEqual(sent[2], (CAT,))
        self.assertLessEqual(len(body), 20)
        self.assertTrue(body.startswith("三行代码"))
        self.assertFalse(any(char in body for char in "`#*-|"))
        self.assertEqual(len(json.loads(provider.call_args.args[1])["previous"]), 1)
        self.assertIn("长度与排版优先", provider.call_args.args[0])
        provider.return_value = {"reply": "#**```"}
        plugin.on_message(self.chat(sequence=101), self.ctx)
        self.assertIn("暂时无法回复", self.backend.sent[-1][1])

    def test_short_mouth_summaries_omit_titles_and_cancel_restores_them(self):
        self.store.record(self.message("今天写了三行代码，剩下的明天做"))
        self.store.set_personality(ROOM, "嘴臭")
        self.ctx._llm = Mock(complete_json=Mock(return_value={"summary": "**代码写三行，吹牛倒是写满了一整本。剩下的明天再说。**"}))
        for command in ("/总结 测试成员", "/总结群聊"):
            self.plugin.on_message(self.message(command), self.ctx)
            body = self.backend.sent[-1][1]
            self.assertLessEqual(len(body), 20)
            self.assertTrue(body.startswith("代码写三行"))
            self.assertNotIn("今日", body)
            self.assertNotIn("*", body)
        prepare_switch(self.message("/切换人格 取消"), self.store)
        self.plugin.on_message(self.message("/总结 测试成员"), self.ctx)
        self.assertTrue(self.backend.sent[-1][1].startswith("测试成员 今日聊天"))

    def test_short_mouth_idiom_reply_keeps_the_next_word_without_formatting(self):
        success = game_reply("嘴臭", True, "", "一心一意", "意气风发", "发愤图强")
        failure = game_reply("嘴臭", False, "**末字和首字不相同，请认真核对接字**", "一心一意", "乱写", "一心一意")
        self.assertIn("发愤图强", success)
        self.assertIn("一心一意", failure)
        for text in (success, failure):
            self.assertLessEqual(len(text), 20)
            self.assertFalse(any(char in text for char in "`#*-|"))

    def test_mention_of_other_person_and_bot_output_do_not_call_model(self):
        provider = Mock(return_value={"reply": "回答"})
        plugin = self.setup_chat(provider)
        for msg in (self.chat(at_members=(OTHER,)), self.chat(sequence=101, bot_generated=True),
                    self.message("@机器人 只是可见文字"), Message("私聊", CAT)):
            plugin.on_message(msg, self.ctx)
        provider.assert_not_called()
        self.assertEqual(self.backend.sent, [])

    def test_unbound_person_can_chat_and_bot_does_not_reply_to_id_text(self):
        plugin = self.setup_chat()
        plugin.on_message(self.chat(sender="wxid_unbound"), self.ctx)
        self.assertTrue(self.backend.sent[-1][1].startswith("@群友\u2005"))
        self.assertEqual(self.backend.sent[-1][2], ("wxid_unbound",))

    def test_cleared_current_message_or_day_change_does_not_reuse_history(self):
        plugin = self.setup_chat()
        msg = self.chat()
        self.store.clear(ROOM)
        plugin.on_message(msg, self.ctx)
        self.assertEqual(self.backend.sent, [])
        msg = self.chat(sequence=102)
        self.now += timedelta(seconds=1)
        plugin.on_message(msg, self.ctx)
        self.assertEqual(self.backend.sent, [])

    def test_clear_and_personality_change_during_chat_request_cancel_reply(self):
        for mutation in (lambda: self.store.clear(ROOM), lambda: self.store.set_personality(ROOM, "卑微")):
            self.store.set_personality(ROOM, "嘴臭")
            def change(system, user):
                mutation()
                return {"reply": "失效的结果"}
            plugin = self.setup_chat(change)
            plugin.on_message(self.chat(), self.ctx)
        self.assertEqual(self.backend.sent, [])

    def test_personality_change_during_at_send_pacing_cancels_native_send(self):
        self.setup_chat()
        msg = self.chat()
        snapshot = self.store.conversation_snapshot(msg)
        state = self.store.set_personality(ROOM, "嘴臭")
        cancel = Mock(is_set=lambda: False)
        def wait(seconds):
            self.store.set_personality(ROOM, "卑微")
            return False
        cancel.wait = wait
        self.service._cancel = cancel
        with self.assertRaises(PersonalityExpired):
            self.ctx.reply_at(msg, "旧回复", display_name="测试成员",
                              delivery_guard=lambda: self.ctx.response_guard(snapshot, state))
        self.assertEqual(self.backend.sent, [])

    def test_long_reply_respects_byte_limit_including_mention_nickname(self):
        self.store.register("🌸" * 64, ROOM, CAT)
        plugin = self.setup_chat(Mock(return_value={"reply": "中文🌸" * 190}))
        plugin.on_message(self.chat(), self.ctx)
        self.assertGreater(len(self.backend.sent), 1)
        self.assertTrue(all(len(row[1].encode("utf-8")) <= 1023 for row in self.backend.sent))
        self.assertTrue(all(row[2] == (CAT,) for row in self.backend.sent))

    def test_invalid_model_reply_is_reported_without_deleting_context(self):
        self.store.record(self.message("历史", sequence=1))
        plugin = self.setup_chat(Mock(return_value={"reply": ""}))
        plugin.on_message(self.chat(), self.ctx)
        self.assertIn("暂时无法回复", self.backend.sent[-1][1])
        self.assertEqual(len(self.store.conversation_snapshot(self.chat(sequence=101)).messages), 2)

    def test_idiom_game_skips_at_dialogue_and_style_keeps_correct_next_word(self):
        plugin = IdiomPlugin()
        plugin.on_load(self.ctx)
        plugin.games[ROOM] = Mock()
        plugin.on_message(self.chat(), self.ctx)
        self.assertEqual(self.backend.sent, [])
        for name in PROMPTS:
            reply = game_reply(name, True, "正确", "一心一意", "意气风发", "发扬光大")
            self.assertIn("发扬光大", reply)
            self.assertIn("以“大”", reply)


@unittest.skipUnless(sys.platform == 'win32', 'Windows native service requires WinDLL')
class NativeAtReceiveTests(unittest.TestCase):
    def test_cdata_and_multiple_members_are_parsed_without_guessing_names(self):
        self.assertEqual(parse_at_members("<msgsource><atuserlist><![CDATA[wxid_a,amy123,wxid_a]]></atuserlist></msgsource>"),
                         ("wxid_a", "amy123"))
        self.assertEqual(parse_at_members("<msgsource><atuserlist>filehelper,坏名字,wxid_b</atuserlist></msgsource>"), ("wxid_b",))
        for source in ("", "@机器人", "<msgsource>", "<!DOCTYPE a [<!ENTITY x 'abc'>]><a>&x;</a>", "x" * 4096):
            self.assertEqual(parse_at_members(source), ())

    def test_visible_labels_are_removed_on_unicode_at_boundary(self):
        self.assertEqual(strip_at_labels("@机器 人\u2005@小明\u2005请看看这句话"), "请看看这句话")
        self.assertEqual(strip_at_labels("普通 @example.com 文本"), "普通 @example.com 文本")

    def test_native_metadata_marks_only_actual_robot_group_at(self):
        native = daily.ROOT / "native"
        if str(native) not in sys.path:
            sys.path.insert(0, str(native))
        from text_service import NativeTextService
        from filehelper_service import ReceivedTextMessage
        service = NativeTextService(pid=123)
        service.router.set_account(ROBOT)
        msg = ReceivedTextMessage(type=1, sender=ROOM.encode(), receiver=ROBOT.encode(),
                                  text=(CAT + ":\n@机器人\u2005你好").encode(),
                                  mention_source=f"<msgsource><atuserlist>{ROBOT}</atuserlist></msgsource>".encode())
        self.assertTrue(service._map_message(msg)["is_at_self"])
        msg.mention_source = f"<msgsource><atuserlist>{OTHER}</atuserlist></msgsource>".encode()
        self.assertFalse(service._map_message(msg)["is_at_self"])
        msg.mention_source = b""
        self.assertFalse(service._map_message(msg)["is_at_self"])
        msg.sender, msg.text = CAT.encode(), "普通私聊".encode()
        msg.mention_source = f"<msgsource><atuserlist>{ROBOT}</atuserlist></msgsource>".encode()
        self.assertFalse(service._map_message(msg)["is_at_self"])


if __name__ == "__main__":
    unittest.main()
