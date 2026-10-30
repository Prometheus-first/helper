"""不连接微信的框架行为测试；发送结果由内存后端记录。"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from wxbot_framework.backend import CAPABILITIES
from wxbot_framework.config import ConfigStore
from wxbot_framework.locking import InstanceLock
from wxbot_framework.plugins import PluginManager
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import Message, PluginContext, ServiceFacade

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".runtime/tests"


def wait_for(condition, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("等待异步结果超时")


class MemoryBackend:
    def __init__(self, fail_start=False):
        self.session_id = uuid.uuid4().hex
        self.pid = 123
        self.last_error = None
        self.sent = []
        self.fail_start = fail_start
        self.started = False
        self.stops = 0

    def start(self, emit):
        if self.fail_start:
            raise RuntimeError("模拟微信尚未启动")
        self.emit = emit
        self.started = True
        return True

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True

    def stop(self):
        self.stops += 1
        self.started = False
        return {"dll_unloaded": True}


class FrameworkTests(unittest.TestCase):
    def setUp(self):
        self.previous_logging = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        shutil.copytree(ROOT / "plugins", self.root / "plugins")
        self.path = self.root / "bot_config.json"
        self.config = json.loads((ROOT / "bot_config.example.json").read_text(encoding="utf-8"))
        self.config.update(console=False, reconnect_seconds=0.1, text_send_delay={"enabled": False})
        # 框架测试不调用外部大模型；玩法单独用假客户端验证。
        self.config["plugins"]["idiom_chain"]["enabled"] = False
        self.config["plugins"]["member_identity"]["enabled"] = False
        self.config["plugins"]["chat_summary"]["enabled"] = False
        self.config["plugins"]["chat_records"]["enabled"] = False
        self.config["plugins"]["personality"]["enabled"] = False
        self.config["plugins"]["mention_chat"]["enabled"] = False
        self.config["plugins"].setdefault("voice_reply", {})["enabled"] = False
        self.config["plugins"].setdefault("picture_idiom", {})["enabled"] = False
        self.config["members_config_file"] = None
        self.write_config()
        self.store = ConfigStore(self.path)
        self.backend = MemoryBackend()
        self.service = ServiceFacade(text_delay=self.config["text_send_delay"])
        self.service.bind(self.backend)
        self.manager = PluginManager(self.root / "plugins", self.store, self.service, CAPABILITIES)

    def tearDown(self):
        self.manager.shutdown()
        self.service.close()
        # 递归清理只针对本测试创建、已核对位于工作区的临时目录。
        self.assertTrue(self.root.resolve().is_relative_to(SCRATCH.resolve()))
        self.temporary.cleanup()
        logging.disable(self.previous_logging)

    def write_config(self):
        self.path.write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")

    def create_plugin(self, name, source, *, manifest=None):
        directory = self.root / "plugins" / name
        directory.mkdir(exist_ok=True)
        (directory / "__init__.py").write_text(source, encoding="utf-8")
        if manifest:
            (directory / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        return directory / "__init__.py"

    def message(self, text, sequence=1):
        return Message.from_native(11046, {"msg": text, "from_wxid": "filehelper", "room_wxid": "",
                                           "native_sender": "self_test", "native_receiver": "filehelper",
                                           "sequence": sequence}, self.backend.session_id)

    def test_scheduled_plugin_action_session_allowlist_and_disable(self):
        manifest = {'api':'v1', 'requires':['text.send'], 'scheduled_actions':[
            {'id':'greet', 'label':'定时问候', 'method':'greet', 'fields':[
                {'name':'chat_id', 'label':'会话', 'type':'chat', 'required':True},
                {'name':'text', 'label':'内容', 'type':'text', 'required':True}]}]}
        self.create_plugin('scheduled_test',
            'class Plugin:\n def on_message(self, m, c): pass\n'
            ' def greet(self, p, c): return c.send_text(p["chat_id"], p["text"])\n', manifest=manifest)
        self.manager.load_one('scheduled_test')
        action = 'plugin.scheduled_test.greet'
        params = {'chat_id':'filehelper', 'text':'定时测试'}
        self.manager.run_scheduled(action, params, self.backend.session_id, ['filehelper'])
        self.assertEqual(self.backend.sent, [('filehelper', '定时测试')])
        with self.assertRaises(RuntimeError):
            self.manager.run_scheduled(action, params, 'old_session', ['filehelper'])
        with self.assertRaises(RuntimeError):
            self.manager.run_scheduled(action, params, self.backend.session_id, ['different_chat'])
        with self.assertRaises(RuntimeError):
            self.manager.run_scheduled(action, params, self.backend.session_id, [], lambda: False)
        with self.assertRaises(ValueError):
            self.manager.run_scheduled('plugin.scheduled_test.on_message', params, self.backend.session_id, [])
        with self.assertRaises(ValueError):
            self.manager.run_scheduled(action, {**params, 'extra':'invalid'}, self.backend.session_id, [])
        self.manager.disable('scheduled_test')
        with self.assertRaises(RuntimeError):
            self.manager.run_scheduled(action, params, self.backend.session_id, [])
        self.assertEqual(len(self.backend.sent), 1)

    def test_new_and_legacy_plugins_receive_unicode_snapshot(self):
        self.manager.reload()
        source = {"msg": "echo 中文 🌸\n第二行", "from_wxid": "filehelper"}
        message = Message.from_native(11046, source, self.backend.session_id)
        source["msg"] = "已被上游修改"
        self.manager.dispatch(message)
        self.manager.dispatch(self.message("test", 2))
        wait_for(lambda: len(self.backend.sent) == 2)
        self.assertCountEqual(self.backend.sent, [("filehelper", "中文 🌸\n第二行"), ("filehelper", "filehelper")])

    def test_plugin_failure_does_not_stop_other_plugins_or_next_message(self):
        self.create_plugin("broken", "class Plugin:\n def on_message(self,m,c):\n  raise SystemExit('模拟插件错误')\n")
        self.manager.reload()
        self.manager.dispatch(self.message("test"))
        self.manager.dispatch(self.message("echo 好的", 2))
        wait_for(lambda: len(self.backend.sent) == 2 and self.manager.records["broken"].errors == 2)
        self.assertTrue(self.manager.records["broken"].thread.is_alive())
        self.assertIn(("filehelper", "好的"), self.backend.sent)

    def test_image_plugins_opt_in_without_changing_existing_text_plugins(self):
        self.create_plugin("image_handler", "class Plugin:\n def __init__(self):\n  self.seen=[]\n"
                           " def on_message(self,m,c):\n  self.seen.append((m.type,m.image_path))\n",
                           manifest={"api": "v1", "message_types": [3], "requires": ["image.receive"]})
        self.manager.capabilities = CAPABILITIES | {"image.receive"}
        self.manager.reload()
        self.manager.dispatch(Message.from_native(11046, {"wx_type": 3, "msg": "<msg><img /></msg>",
                               "chat_id": "filehelper", "image_path": "saved.png"}, self.backend.session_id))
        self.manager.dispatch(self.message("echo 文字照常处理"))
        wait_for(lambda: self.manager.records["image_handler"].handled == 1 and
                 self.manager.records["echo"].handled == 1 and self.manager.records["id_responder"].handled == 1)
        self.assertEqual(self.manager.records["image_handler"].instance.seen, [(3, "saved.png")])
        self.assertEqual(self.backend.sent, [("filehelper", "文字照常处理")])

    def test_invalid_plugin_message_types_are_rejected(self):
        for index, types in enumerate(([], [True], [0], ["3"], None)):
            name = f"invalid_images_{index}"
            self.create_plugin(name, "class Plugin: pass\n", manifest={"message_types": types})
            self.assertNotIn(name, self.manager.discover())
            self.assertIn("message_types", self.manager.load_errors[name])

    def test_console_sends_image_with_spaces_in_its_windows_path(self):
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        app.service.bind(self.backend)
        image = self.root / "图片 sample.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
        self.backend.send_image = Mock(return_value=True)
        app.execute(f'send-image filehelper "{image}"')
        self.backend.send_image.assert_called_once_with("filehelper", str(image))
        self.backend.send_image.return_value = False
        with self.assertRaisesRegex(RuntimeError, "未接受图片"):
            app.execute(f'send-image filehelper "{image}"')

    def test_invalid_image_save_directory_is_rejected_during_config_check(self):
        for value in (None, 123, "", " ", "bad\0path"):
            self.config["image_directory"] = value
            self.write_config()
            with self.subTest(directory=value), self.assertRaisesRegex(ValueError, "image_directory"):
                self.store.load()

    def test_emoticon_console_commands_preserve_paths_and_report_rejection(self):
        from dataclasses import make_dataclass
        from PIL import Image
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        app.service.bind(self.backend)
        path = self.root / "动态 sticker.gif"
        Image.new("RGB", (4, 4), "green").save(path)
        self.backend.send_emoticon = Mock(return_value=True)
        app.execute(f'send-emoticon filehelper "{path}"')
        self.backend.send_emoticon.assert_called_once_with("filehelper", str(path))
        self.backend.send_emoticon.return_value = False
        with self.assertRaisesRegex(RuntimeError, "未接受表情"):
            app.execute(f'send-emoticon filehelper "{path}"')
        Row = make_dataclass("Row", ["gif_path", "sendable"])
        self.backend.export_emoticons = Mock(return_value=(Row(path, True),))
        app.execute("export-emoticons")
        app.execute("export-emoticons raw")
        self.assertEqual(self.backend.export_emoticons.call_args_list,
                         [unittest.mock.call(convert=True), unittest.mock.call(convert=False)])

    def test_invalid_emoticon_directories_are_rejected_during_config_check(self):
        for field, values in (("emoticon_directory", (None, 123, "", " ", "bad\0path")),
                              ("wechat_data_directory", (123, "", " ", "bad\0path"))):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.config[field] = value
                    self.write_config()
                    with self.assertRaisesRegex(ValueError, field):
                        self.store.load()
            self.config.pop(field, None)

    def test_disabled_and_unsupported_plugins_are_not_imported(self):
        source = "raise RuntimeError('此插件不应被导入')\n"
        self.create_plugin("disabled", source)
        self.create_plugin("groups", source, manifest={"api": "v1", "requires": ["group.at"]})
        self.config["plugins"]["disabled"] = {"enabled": False}
        self.write_config()
        self.manager.reload()
        self.assertNotIn("disabled", self.manager.records)
        self.assertNotIn("groups", self.manager.records)
        groups = next(row for row in self.manager.status() if row["name"] == "groups")
        self.assertEqual(groups["missing_capabilities"], ["group.at"])

    def test_enable_disable_persist_and_keep_other_plugins_running(self):
        self.manager.reload()
        self.manager.disable("echo")
        saved = self.store.load()["plugins"]["echo"]
        self.assertFalse(saved["enabled"])
        self.assertEqual(saved["prefix"], "echo ")
        self.manager.dispatch(self.message("echo 不应回复"))
        self.manager.dispatch(self.message("test", 2))
        wait_for(lambda: self.manager.records["id_responder"].handled == 2)
        self.assertEqual(self.backend.sent, [("filehelper", "filehelper")])
        self.manager.enable("echo")
        self.assertTrue(self.store.load()["plugins"]["echo"]["enabled"])
        self.manager.dispatch(self.message("echo 恢复了", 3))
        wait_for(lambda: ("filehelper", "恢复了") in self.backend.sent)

    def test_running_plugin_keeps_description_when_manifest_cannot_be_read(self):
        self.manager.load_one("echo")
        expected = self.manager.records["echo"].spec.description
        (self.root / "plugins/echo/plugin.json").write_text("{broken", encoding="utf-8")

        row = next(row for row in self.manager.status() if row["name"] == "echo")

        self.assertTrue(row["active"])
        self.assertEqual(row.get("description"), expected)
        self.assertTrue(row["error"])

    def test_reload_uses_fresh_source_and_failed_reload_keeps_previous(self):
        source = "class Plugin:\n def on_message(self,m,c):\n  c.reply(m,'v1')\n"
        path = self.create_plugin("reloadable", source)
        self.manager.reload()
        self.manager.dispatch(self.message("one"))
        wait_for(lambda: ("filehelper", "v1") in self.backend.sent)
        previous_module = self.manager.records["reloadable"].module_name
        stat = path.stat()
        path.write_text(source.replace("v1", "v2"), encoding="utf-8")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.manager.reload("reloadable")
        self.assertNotIn(previous_module, sys.modules)
        self.manager.dispatch(self.message("two", 2))
        wait_for(lambda: ("filehelper", "v2") in self.backend.sent)
        current = self.manager.records["reloadable"]
        path.write_text("class Plugin(:\n", encoding="utf-8")
        self.manager.reload("reloadable")
        self.assertIs(self.manager.records["reloadable"], current)
        self.assertTrue(current.active)
        self.manager.dispatch(self.message("three", 3))
        wait_for(lambda: self.backend.sent.count(("filehelper", "v2")) == 2)

    def test_unload_releases_plugin_resources(self):
        marker = self.root / "lifecycle.txt"
        source = ("class Plugin:\n"
                  " def on_load(self,c):\n  self.file=open(c.config['marker'],'w',encoding='utf-8')\n"
                  " def on_message(self,m,c):\n  pass\n"
                  " def on_unload(self,c):\n  self.file.write('released');self.file.close()\n")
        self.create_plugin("resource", source)
        self.config["plugins"]["resource"] = {"enabled": True, "marker": str(marker)}
        self.write_config()
        self.manager.reload()
        self.manager.disable("resource")
        self.assertEqual(marker.read_text(encoding="utf-8"), "released")

    def test_busy_reload_does_not_replace_active_callback(self):
        source = ("import threading\nclass Plugin:\n"
                  " def on_load(self,c):\n  self.entered=threading.Event();self.release=threading.Event()\n"
                  " def on_message(self,m,c):\n  self.entered.set();self.release.wait(4)\n")
        self.create_plugin("busy", source)
        self.manager.reload()
        original = self.manager.records["busy"]
        self.manager.dispatch(self.message("work"))
        self.assertTrue(original.instance.entered.wait(1))
        try:
            with self.assertRaises(RuntimeError):
                self.manager.load_one("busy")
            self.assertIs(self.manager.records["busy"], original)
            self.assertTrue(original.active)
        finally:
            original.instance.release.set()

    def test_old_session_reply_is_rejected_after_reconnect(self):
        old = self.message("old")
        self.service.close()
        replacement = MemoryBackend()
        self.service.bind(replacement)
        context = PluginContext("test", {}, self.service, CAPABILITIES)
        with self.assertRaises(RuntimeError):
            context.reply(old, "不应发送")
        self.assertEqual(replacement.sent, [])

    def test_runtime_waits_reconnects_and_stops_cleanly(self):
        created = []

        def factory():
            backend = MemoryBackend(fail_start=not created)
            created.append(backend)
            return backend

        app = BotApplication(self.root, self.store, backend_factory=factory)
        thread = threading.Thread(target=app.run)
        thread.start()
        try:
            wait_for(lambda: len(created) >= 2 and app._connection_ready)
            created[1].last_error = RuntimeError("模拟断线")
            wait_for(lambda: len(created) >= 3 and app._connection_ready)
            created[2].emit(Message.from_native(11046, {"msg": "test", "from_wxid": "filehelper"}, created[2].session_id))
            wait_for(lambda: created[2].sent == [("filehelper", "filehelper")])
            app.commands.put("quit")
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(all(backend.stops == 1 for backend in created))
            self.assertFalse(app.service.connected)
        finally:
            app.stop_event.set()
            thread.join(3)

    def test_single_instance_lock_is_released(self):
        path = self.root / "bot.lock"
        with InstanceLock(path):
            with self.assertRaises(RuntimeError):
                InstanceLock(path).acquire()
        with InstanceLock(path):
            pass

    def test_plugins_reply_to_friend_and_group_conversations(self):
        from wxbot_framework.text import TextRouter
        router = TextRouter()
        router.convert("wxid_self", "filehelper", "init")
        self.manager.reload()
        for sender, text in (("wxid_friend", "echo 好友"),
                             ("12345@chatroom", "wxid_member:\necho 群聊"),
                             ("12345@chatroom", "wxid_member:\ntest")):
            data = router.convert(sender, "wxid_self", text)
            self.manager.dispatch(Message.from_native(11046, data, self.backend.session_id))
        wait_for(lambda: len(self.backend.sent) == 3)
        self.assertCountEqual(self.backend.sent, [("wxid_friend", "好友"),
                                                ("12345@chatroom", "群聊"),
                                                ("12345@chatroom", "12345@chatroom")])

    def test_runtime_allowlist_dispatches_only_selected_chat(self):
        self.config["backend"] = "native_text"
        self.config["chat_allowlist"] = ["filehelper"]
        self.write_config()
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        app.service.bind(self.backend)
        app.plugins.reload()
        try:
            app._emit(Message(text="test", chat_id="wxid_other", session_id=self.backend.session_id,
                              data={"msg": "test", "from_wxid": "wxid_other"}))
            app._emit(self.message("echo 允许"))
            wait_for(lambda: app.plugins.records["id_responder"].handled == 1 and
                     app.plugins.records["echo"].handled == 1)
            self.assertEqual(self.backend.sent, [("filehelper", "允许")])
        finally:
            app.plugins.shutdown()

    def test_new_group_is_logged_before_allowlist_without_body_or_member(self):
        self.config["chat_allowlist"] = ["filehelper"]
        self.write_config()
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        app.members = Mock()
        room = "87654321@chatroom"
        sender = "wxid_example_member"
        body = "本条群正文不应出现在日志中"
        # 同时覆盖标准消息和仅有会话 ID、缺少群标记的消息，避免误当私聊泄露正文。
        logging.disable(logging.NOTSET)
        with patch.object(app.plugins, "dispatch") as dispatch, self.assertLogs("Bot", "INFO") as captured:
            for is_group in (True, False):
                app._emit(Message(body, room, sender, is_group=is_group))
        self.assertEqual(len(captured.records), 2)
        for record in captured.records:
            self.assertIn(room, record.getMessage())
            self.assertNotIn(body, record.getMessage())
            self.assertNotIn(sender, record.getMessage())
        app.members.record.assert_not_called()
        dispatch.assert_not_called()

    def test_private_log_keeps_sender_and_unicode_but_escapes_line_breaks(self):
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        sender = "wxid_example_sender"
        body = "你好 🌸\r\n第二行\t100%"
        # 文件传输助手的会话 ID 与实际作者不同，必须显示作者。
        message = Message.from_native(11046, {"msg": body, "chat_id": "filehelper",
                                             "sender_id": sender, "dispatch_to_plugins": False}, "logging")
        logging.disable(logging.NOTSET)
        with self.assertLogs("Bot", "INFO") as captured:
            app._emit(message)
        self.assertEqual(len(captured.records), 1)
        rendered = captured.records[0].getMessage()
        self.assertIn(sender, rendered)
        self.assertIn("你好 🌸", rendered)
        self.assertNotIn("\n", rendered)
        self.assertNotIn("\r", rendered)
        self.assertEqual(json.loads(rendered.split("内容=", 1)[1]), body)

    @unittest.skipUnless(sys.platform == "win32", "本机原生服务检查")
    def test_native_test_messages_do_not_bypass_group_log_privacy(self):
        native = str(ROOT / "native")
        if native not in sys.path:
            sys.path.insert(0, native)
        from text_service import NativeTextService
        from filehelper_service import ReceivedTextMessage
        service = NativeTextService(pid=123)
        account, sender, room = "wxid_example_self", "wxid_example_member", "87654321@chatroom"
        service.router.set_account(account)
        service._sender_wechat_name = Mock(return_value=None)
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        markers = ("WXBOT_AT_CHAT_PROBE", "WXBOT_GROUP_TEST", "echo WXBOT_GROUP_TEST")
        logging.disable(logging.NOTSET)
        with self.assertLogs(level="INFO") as captured:
            for body in markers:
                raw = ReceivedTextMessage(type=1, sender=room.encode(), receiver=account.encode(),
                                          text=(sender + ":\n" + body).encode())
                app._emit(Message.from_native(11046, service._map_message(raw), "logging"))
        self.assertEqual(len(captured.records), len(markers))
        rendered = "\n".join(captured.output)
        self.assertIn(room, rendered)
        self.assertNotIn(sender, rendered)
        for body in markers:
            self.assertNotIn(body, rendered)

        service._owns_bridge = True
        service._check_account = Mock()
        service._run = Mock(return_value={"success": True})
        with self.assertLogs("NativeText", "INFO") as sent_logs:
            self.assertTrue(service.send_text(room, "WXBOT_GROUP_SEND_TEST"))
        self.assertIn(room, "\n".join(sent_logs.output))
        self.assertNotIn("WXBOT_GROUP_SEND_TEST", "\n".join(sent_logs.output))


if __name__ == "__main__":
    unittest.main()
