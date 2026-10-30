"""今日记录、午夜清理、完整分批总结与清空竞态；不连接微信或外部 API。"""
import json
import logging
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

from plugins.chat_records import Plugin as ClearPlugin
from plugins.chat_summary import Plugin as SummaryPlugin
from wxbot_framework.config import ConfigStore
from wxbot_framework.llm import LLMError
from wxbot_framework.members import MemberStore, SnapshotExpired
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import Message, PluginContext, ServiceFacade
from wxbot_framework.summary import ChatSummarizer, conversation_rows, split_text
from wxbot_framework.text import TextRouter

ROOT = Path(__file__).resolve().parent.parent
ROOM, OTHER_ROOM = "123@chatroom", "456@chatroom"
ROBOT, CAT, OTHER = "wxid_robot", "amy123", "wxid_other"


class Backend:
    session_id = "test"

    def __init__(self):
        self.sent = []

    def send_text(self, room, text):
        self.sent.append((room, text))
        return True

    def stop(self):
        return True


class DailySummaryTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".runtime/tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temporary.name)
        self.now = datetime(2026, 10, 10, 15, 59, 59, tzinfo=timezone.utc)  # 北京 23:59:59
        options = {"database_file": "data.sqlite3", "history_enabled": True, "daily_clear": True,
                   "groups": {ROOM: [], OTHER_ROOM: []}}
        self.path = self.root / "members.json"
        self.path.write_text(json.dumps(options), encoding="utf-8")
        self.stores = []
        self.store = self.new_store()
        self.store.register("测试成员", ROOM, CAT)
        self.store.register("别人", ROOM, OTHER)
        self.store.register("测试成员", OTHER_ROOM, CAT)
        self.backend = Backend()
        self.service = ServiceFacade(text_delay={"enabled": False})
        self.service.bind(self.backend)
        self.ctx = PluginContext("summary", {}, self.service, [], member_store=self.store)
        self.calls = []
        self.ctx._llm = Mock(complete_json=self.model)
        self.plugin = SummaryPlugin()
        self.plugin.on_load(self.ctx)
        self.clear_plugin = ClearPlugin()
        self.clear_plugin.on_load(self.ctx)

    def tearDown(self):
        self.service.close()
        for store in self.stores:
            store.close()
        self.assertTrue(self.root.resolve().is_relative_to((ROOT / ".runtime/tests").resolve()))
        self.temporary.cleanup()

    def new_store(self, account=ROBOT):
        store = MemberStore(self.path, account, now=lambda: self.now)
        self.stores.append(store)
        store.initialize()
        return store

    def message(self, text, sender=CAT, room=ROOM, sequence=0, **kwargs):
        return Message(text, room, sender_id=sender, is_group=True, session_id="test", sequence=sequence, **kwargs)

    def model(self, system, user):
        self.calls.append(json.loads(user))
        return {"summary": "讨论了晚餐和明天的安排。"}

    def snapshot(self):
        return self.store.today_snapshot("测试成员", ROOM)

    def test_midnight_clears_idle_buffer_preserves_identity_and_invalidates_snapshot(self):
        self.store.record(self.message("今晚聊聊天"))
        snapshot = self.snapshot()
        self.assertEqual(snapshot.day, "2026-10-10")
        self.now += timedelta(seconds=1)
        self.assertTrue(self.store.rollover())
        self.assertEqual(self.store.history("测试成员", ROOM), [])
        self.assertFalse(self.store.snapshot_is_current(snapshot))
        self.assertEqual(self.store.resolve("测试成员", ROOM).wxid, CAT)
        self.store.record(self.message("新的一天"))
        self.assertEqual([r["text"] for r in self.snapshot().messages], ["新的一天"])
        self.assertEqual(self.snapshot().day, "2026-10-11")

    def test_restart_after_missed_midnight_cleans_previous_day(self):
        self.store.record(self.message("旧记录"))
        self.store.close()
        self.now += timedelta(days=2)
        reopened = self.new_store()
        self.assertEqual(reopened.today_snapshot("测试成员", ROOM).messages, ())
        self.assertEqual(reopened.history("测试成员", ROOM), [])

    def test_restart_same_day_retains_all_text_and_aliases(self):
        self.store.record(self.message("今天的话"))
        self.store.register("测试成员新名", ROOM, CAT)
        reopened = self.new_store()
        self.assertEqual(reopened.resolve("测试成员", ROOM).name, "测试成员新名")
        self.assertEqual(len(reopened.today_snapshot("测试成员新名", ROOM).messages), 1)

    def test_clear_isolates_groups_and_accounts_without_deleting_bindings(self):
        self.store.record(self.message("这个群"))
        self.store.record(self.message("另一个群", room=OTHER_ROOM))
        other = self.new_store("wxid_second_robot")
        other.register("测试成员", ROOM, CAT)
        other.record(self.message("另一个账号"))
        snapshot = self.snapshot()
        self.assertEqual(self.store.clear(ROOM), 1)
        self.assertFalse(self.store.snapshot_is_current(snapshot))
        self.assertEqual(len(self.store.today_snapshot("测试成员", OTHER_ROOM).messages), 1)
        self.assertEqual(len(other.today_snapshot("测试成员", ROOM).messages), 1)
        self.assertEqual(self.store.resolve("测试成员", ROOM).wxid, CAT)

    def test_today_snapshot_does_not_truncate_at_console_history_limit(self):
        for index in range(1005):
            self.store.record(self.message(str(index), sequence=index + 1))
        self.assertEqual(len(self.snapshot().messages), 1005)
        self.assertEqual(self.snapshot().messages[0]["text"], "0")
        self.assertEqual(len(self.store.today_group_snapshot(ROOM).messages), 1005)

    def test_bot_output_stored_but_excluded_from_member_summary(self):
        self.store.record(self.message("机器人输出", data={"bot_generated": True}))
        self.store.record(self.message("自己手机的话"))
        self.assertEqual(len(self.store.history("测试成员", ROOM)), 2)
        self.assertEqual([r["text"] for r in self.snapshot().messages], ["自己手机的话"])

    def test_summary_uses_current_room_and_actual_id_and_filters_commands(self):
        for text in ("晚饭吃面", "/绑定 测试成员", "/总结 测试成员", "/总结群聊", "/help"):
            self.store.record(self.message(text))
        self.store.record(self.message("别人的话", sender=OTHER))
        self.store.record(self.message("另一个群的话", room=OTHER_ROOM))
        self.plugin.on_message(self.message("/总结 测试成员", sender=OTHER), self.ctx)
        self.assertEqual(len(self.calls), 1)
        data = self.calls[0]
        self.assertEqual([r["text"] for r in data["data"]], ["晚饭吃面"])
        self.assertNotIn(CAT, json.dumps(data))
        self.assertIn("1条", self.backend.sent[-1][1])

    def test_scheduled_group_summary_reuses_current_group_records(self):
        self.store.record(self.message('今天聊了晚饭'))
        self.ctx.session_id = 'test'
        self.plugin.scheduled_group_summary({'chat_id':ROOM}, self.ctx)
        self.assertEqual(self.backend.sent[-1][0], ROOM)
        self.assertIn('今日群聊总结', self.backend.sent[-1][1])
        with self.assertRaises(ValueError):
            self.plugin.scheduled_group_summary({'chat_id':'unknown@chatroom'}, self.ctx)

    def test_unknown_member_and_no_chat_do_not_call_model(self):
        self.plugin.on_message(self.message("/总结 未绑定"), self.ctx)
        self.assertIn("请本人先发送 /绑定", self.backend.sent[-1][1])
        self.plugin.on_message(self.message("/总结 测试成员"), self.ctx)
        self.assertIn("没有可总结", self.backend.sent[-1][1])
        self.assertEqual(self.calls, [])

    def test_summary_and_clear_validate_commands_and_ignore_private_chats(self):
        self.plugin.on_message(self.message("/总结"), self.ctx)
        self.assertEqual(self.backend.sent[-1][1], "用法：/总结 昵称")
        self.clear_plugin.on_message(self.message("/清空 别人"), self.ctx)
        self.assertEqual(self.backend.sent[-1][1], "用法：/清空")
        count = len(self.backend.sent)
        self.plugin.on_message(Message("/总结 测试成员", CAT), self.ctx)
        self.clear_plugin.on_message(Message("/清空", CAT), self.ctx)
        self.assertEqual(len(self.backend.sent), count)

    def test_manual_clear_command_keeps_member_registration(self):
        self.store.record(self.message("可清空的记录"))
        self.clear_plugin.on_message(self.message("/清空"), self.ctx)
        self.assertEqual(self.snapshot().messages, ())
        self.assertEqual(self.backend.sent[-1][1], "已清空本群聊天记录。")
        self.assertEqual(self.store.resolve("测试成员", ROOM).wxid, CAT)

    def test_all_long_records_are_covered_and_partials_are_merged(self):
        texts = [f"第{i}条" + "内容" * 950 for i in range(9)]
        for text in texts:
            self.store.record(self.message(text))
        summary = ChatSummarizer(self.ctx.llm, chunk_chars=4000)
        snapshot = self.snapshot()
        summary.summarize(snapshot, conversation_rows(snapshot), current=lambda: True)
        received = [item for call in self.calls for item in call["data"] if "text" in item]
        for index, original in enumerate(texts, 1):
            self.assertEqual("".join(item["text"] for item in received if item["record"] == index), original)
        self.assertTrue(any(call["task"] == "合并为最终总结" for call in self.calls))
        self.assertTrue(all(len(json.dumps(call, ensure_ascii=False)) <= 4000 for call in self.calls))

    def test_clear_during_llm_call_cancels_result(self):
        self.store.record(self.message("不能清空后再发"))
        def clear_during_request(system, user):
            self.clear_plugin.on_message(self.message("/清空"), self.ctx)
            return {"summary": "旧总结"}
        self.ctx._llm = Mock(complete_json=clear_during_request)
        self.plugin.on_load(self.ctx)
        self.plugin.on_message(self.message("/总结 测试成员"), self.ctx)
        self.assertEqual(self.backend.sent, [(ROOM, "已清空本群聊天记录。")])

    def test_json_escaping_does_not_overflow_small_batches(self):
        text = "\x01" * 2000
        self.store.record(self.message(text))
        snapshot = self.snapshot()
        ChatSummarizer(self.ctx.llm, chunk_chars=4000).summarize(
            snapshot, conversation_rows(snapshot), current=lambda: True)
        self.assertTrue(all(len(json.dumps(call, ensure_ascii=False)) <= 4000 for call in self.calls))
        units = [unit for call in self.calls for unit in call["data"] if "text" in unit]
        self.assertEqual("".join(unit["text"] for unit in units), text)

    def test_midnight_during_request_cancels_result(self):
        self.store.record(self.message("旧天聊天"))
        def midnight(system, user):
            self.now += timedelta(seconds=1)
            return {"summary": "昨天的总结"}
        self.ctx._llm = Mock(complete_json=midnight)
        self.plugin.on_load(self.ctx)
        self.plugin.on_message(self.message("/总结 测试成员"), self.ctx)
        self.assertEqual(self.backend.sent, [])

    def test_clear_during_pacing_prevents_native_send(self):
        self.store.record(self.message("旧记录"))
        snapshot = self.snapshot()
        cancel = Mock(is_set=lambda: False)
        def wait(seconds):
            self.store.clear(ROOM)
            return False
        cancel.wait = wait
        self.service._cancel = cancel
        with self.assertRaises(SnapshotExpired):
            self.ctx.reply(self.message("/总结 测试成员"), "待发送的旧总结",
                           delivery_guard=lambda: self.store.delivery_guard(snapshot))
        self.assertEqual(self.backend.sent, [])

    def test_new_messages_do_not_cancel_existing_snapshot(self):
        snapshot = self.snapshot()
        self.store.record(self.message("新消息"))
        self.assertTrue(self.store.snapshot_is_current(snapshot))

    def test_llm_error_returns_short_message_and_preserves_records(self):
        self.store.record(self.message("原始聊天"))
        self.ctx._llm = Mock(complete_json=Mock(side_effect=LLMError("模拟超时")))
        self.plugin.on_load(self.ctx)
        self.plugin.on_message(self.message("/总结 测试成员"), self.ctx)
        self.assertIn("暂时无法生成", self.backend.sent[-1][1])
        self.assertEqual(len(self.snapshot().messages), 1)

    def test_malformed_summary_is_rejected(self):
        self.store.record(self.message("聊天"))
        snapshot = self.snapshot()
        for result in ({}, {"summary": ""}, {"summary": 1}, {"summary": "a\0b"}, {"summary": "a\x01b"}, {"summary": "字" * 601}, []):
            client = Mock(complete_json=Mock(return_value=result))
            with self.subTest(result_type=type(result)), self.assertRaises(LLMError):
                ChatSummarizer(client).summarize(snapshot, conversation_rows(snapshot), current=lambda: True)

    def test_utf8_chunks_keep_all_characters_and_fit_native_limit(self):
        text = "中文🌸abc\n" * 300
        parts = split_text(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(0 < len(part.encode("utf-8")) <= 1023 for part in parts))

    def test_owner_group_messages_are_archived_without_reply_loops(self):
        router = TextRouter(include_self_groups=True)
        router.set_account(ROBOT)
        data = router.convert(ROOM, ROBOT, ROBOT + ":\n普通手机聊天")
        self.assertFalse(data["dispatch_to_plugins"])
        self.store.register("主人", ROOM, ROBOT)
        # 验证 runtime 确实先记下文本，再跳过插件。
        app = object.__new__(BotApplication)
        app.config = {"chat_allowlist": []}
        app.members, app.plugins = self.store, Mock()
        app._emit(Message.from_native(11046, data, "test"))
        app.plugins.dispatch.assert_not_called()
        self.assertEqual(self.store.today_snapshot("主人", ROOM).messages[0]["text"], "普通手机聊天")
        for command in ("/绑定 主人", "/总结 测试成员", "/总结群聊", "/清空"):
            self.assertTrue(router.convert(ROOM, ROBOT, ROBOT + ":\n" + command)["dispatch_to_plugins"])

    def test_group_snapshot_includes_unbound_and_owner_but_isolates_room_and_account(self):
        self.store.record(self.message("测试成员先说"))
        self.store.record(self.message("未绑定者接话", sender="wxid_unbound"))
        self.store.record(self.message("主人手机说话", sender=ROBOT, is_self=True))
        self.store.record(self.message("测试成员再说"))
        self.store.record(self.message("机器人输出", sender=ROBOT, is_self=True, data={"bot_generated": True}))
        self.store.record(self.message("另一个群", room=OTHER_ROOM))
        other = self.new_store("wxid_second_robot")
        other.record(self.message("另一个账号的记录"))
        snapshot = self.store.today_group_snapshot(ROOM)
        self.assertEqual([row["text"] for row in snapshot.messages],
                         ["测试成员先说", "未绑定者接话", "主人手机说话", "测试成员再说"])
        self.assertEqual([row["speaker"] for row in snapshot.messages], ["P1", "P2", "P3", "P1"])
        self.assertEqual(snapshot.messages[0]["speaker_name"], "测试成员")
        self.assertEqual(snapshot.messages[1]["speaker_name"], "未绑定成员2")
        self.assertEqual(snapshot.room, ROOM)

    def test_group_command_sends_all_people_without_raw_ids_and_filters_commands(self):
        self.store.record(self.message("测试成员想吃面"))
        self.store.record(self.message("别人想吃饭", sender=OTHER))
        self.store.record(self.message("未绑定者订餐", sender="wxid_unbound"))
        self.store.record(self.message("/总结群聊", sender=ROBOT, is_self=True))
        self.store.record(self.message("/总结 测试成员"))
        client = Mock(complete_json=Mock(side_effect=self.model))
        self.ctx._llm = client
        self.plugin.on_load(self.ctx)
        self.plugin.on_message(self.message("/总结群聊", sender=ROBOT, is_self=True), self.ctx)
        self.assertEqual(self.calls[0]["scope"], "group")
        self.assertEqual([unit["text"] for unit in self.calls[0]["data"]],
                         ["测试成员想吃面", "别人想吃饭", "未绑定者订餐"])
        self.assertEqual([unit["speaker_name"] for unit in self.calls[0]["data"]],
                         ["测试成员", "别人", "未绑定成员3"])
        payload = json.dumps(self.calls, ensure_ascii=False)
        for wxid in (ROBOT, CAT, OTHER, "wxid_unbound", ROOM):
            self.assertNotIn(wxid, payload)
        self.assertIn("当前微信群所有人的聊天", client.complete_json.call_args.args[0])
        self.assertIn("今日群聊总结", self.backend.sent[-1][1])
        self.assertIn("3条，3人", self.backend.sent[-1][1])

    def test_group_command_empty_or_only_commands_does_not_call_model(self):
        for text in ("/绑定 测试成员", "/help", "/总结群聊"):
            self.store.record(self.message(text))
        self.plugin.on_message(self.message("/总结群聊"), self.ctx)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.backend.sent[-1][1], "本群今天还没有可总结的聊天记录。")

    def test_group_command_requires_exact_command_no_argument_and_group_context(self):
        self.plugin.on_message(self.message("/总结群聊 测试成员"), self.ctx)
        self.assertEqual(self.backend.sent[-1][1], "用法：/总结群聊")
        count = len(self.backend.sent)
        self.plugin.on_message(self.message("/总结群聊测试"), self.ctx)
        self.plugin.on_message(Message("/总结群聊", CAT), self.ctx)
        self.plugin.on_message(self.message("/总结群聊", room="999@chatroom"), self.ctx)
        self.assertEqual(len(self.backend.sent), count)
        self.assertEqual(self.calls, [])

    def test_group_clear_during_request_cancels_result(self):
        self.store.record(self.message("今天一起吃饭"))
        def clear_during_request(system, user):
            self.clear_plugin.on_message(self.message("/清空"), self.ctx)
            return {"summary": "已被清空的全群聊天"}
        self.ctx._llm = Mock(complete_json=clear_during_request)
        self.plugin.on_load(self.ctx)
        self.plugin.on_message(self.message("/总结群聊"), self.ctx)
        self.assertEqual(self.backend.sent, [(ROOM, "已清空本群聊天记录。")])

    def test_group_midnight_during_request_cancels_result(self):
        self.store.record(self.message("旧日全群聊天"))
        def midnight(system, user):
            self.now += timedelta(seconds=1)
            return {"summary": "昨天的全群总结"}
        self.ctx._llm = Mock(complete_json=midnight)
        self.plugin.on_load(self.ctx)
        self.plugin.on_message(self.message("/总结群聊"), self.ctx)
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(self.store.today_group_snapshot(ROOM).messages, ())

    def test_group_guard_after_send_delay_detects_clear(self):
        self.store.record(self.message("全群快照"))
        snapshot = self.store.today_group_snapshot(ROOM)
        cancel = Mock(is_set=lambda: False)
        def wait(seconds):
            self.store.clear(ROOM)
            return False
        cancel.wait = wait
        self.service._cancel = cancel
        with self.assertRaises(SnapshotExpired):
            self.ctx.reply(self.message("/总结群聊"), "待发送全群总结",
                           delivery_guard=lambda: self.store.delivery_guard(snapshot))
        self.assertEqual(self.backend.sent, [])

    def test_long_group_batches_preserve_speakers_all_text_and_json_size(self):
        self.store.register("\x01" * 64, ROOM, CAT)
        originals = ["\x02" * 2000, "其他人" * 1000, "最后" * 2000]
        for index, text in enumerate(originals):
            self.store.record(self.message(text, sender=CAT if index != 1 else OTHER))
        snapshot = self.store.today_group_snapshot(ROOM)
        ChatSummarizer(self.ctx.llm, chunk_chars=4000).summarize(
            snapshot, conversation_rows(snapshot), current=lambda: True)
        self.assertTrue(all(len(json.dumps(call, ensure_ascii=False)) <= 4000 for call in self.calls))
        units = [unit for call in self.calls for unit in call["data"] if "text" in unit]
        for index, text in enumerate(originals, 1):
            parts = [unit for unit in units if unit["record"] == index]
            self.assertEqual("".join(unit["text"] for unit in parts), text)
            self.assertTrue(all(unit["speaker"] == ("P2" if index == 2 else "P1") for unit in parts))
        self.assertTrue(any(call["task"] == "合并为最终总结" for call in self.calls))

    def test_group_llm_error_preserves_all_people_records(self):
        self.store.record(self.message("测试成员今天的话"))
        self.store.record(self.message("别人今天的话", sender=OTHER))
        self.ctx._llm = Mock(complete_json=Mock(side_effect=LLMError("模拟群聊总结失败")))
        self.plugin.on_load(self.ctx)
        self.plugin.on_message(self.message("/总结群聊"), self.ctx)
        self.assertIn("暂时无法生成", self.backend.sent[-1][1])
        self.assertEqual(len(self.store.today_group_snapshot(ROOM).messages), 2)

    def test_runtime_rollover_runs_without_new_messages(self):
        # 禁用连接和所有插件，只验证主循环的定时清理调用。
        config = json.loads((ROOT / "bot_config.example.json").read_text(encoding="utf-8"))
        config["members_config_file"] = "members.json"
        config["wechat_account"] = ROBOT
        config["console"] = False
        for settings in config["plugins"].values():
            settings["enabled"] = False
        config_path = self.root / "bot_config.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        (self.root / "plugins").mkdir()
        app = BotApplication(self.root, ConfigStore(config_path))
        app.members.close()
        app.members = Mock()
        app._connect = Mock()
        app.run(seconds=0.65)
        self.assertGreaterEqual(app.members.rollover.call_count, 2)


if __name__ == "__main__":
    unittest.main()
