import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from wxbot_framework.linux_backend import LinuxNativeTextBackend
from wxbot_framework.linux_sender import LinuxNativeSender, LinuxSendError, LinuxSendIndeterminate

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix" and hasattr(socket, "SO_PEERCRED") and shutil.which("gcc"), "requires Linux/GCC")
class LinuxSenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="helper-sender-tests-")
        cls.dir = Path(cls.tmp.name)
        cls.fixture = cls.dir / "fixture"
        cls.bridge = cls.dir / "send.so"
        cls.production = cls.dir / "production.so"
        cls.recv = cls.dir / "receive.so"
        cls.combined = cls.dir / "combined"
        source = ROOT / "native/linux/send_bridge.c"
        fixture = ROOT / "tests/fixtures/linux_send_fixture.c"
        flags = ["gcc", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror"]
        subprocess.run(flags + ["-rdynamic", "-pthread", str(fixture), "-o", str(cls.fixture)], check=True)
        shared = flags + ["-fPIC", "-shared", "-pthread"]
        subprocess.run(shared + ["-DSDK_FIXTURE", "-Wno-unused-function", str(source), "-ldl", "-o", str(cls.bridge)], check=True)
        subprocess.run(shared + [str(source), "-ldl", "-o", str(cls.production)], check=True)
        subprocess.run(["gcc", "-O2", "-fPIC", "-shared", "-pthread", "-DHELPER_FIXTURE_TEST",
                        str(ROOT / "native/linux/text_bridge.c"), "-ldl", "-o", str(cls.recv)], check=True)
        obj = cls.dir / "send_fixture.o"
        subprocess.run(flags + ["-Dmain=send_fixture_main", "-c", str(fixture), "-o", str(obj)], check=True)
        subprocess.run(["gcc", "-O2", "-rdynamic", "-fno-builtin", "-pthread",
                        str(ROOT / "tests/fixtures/linux_bridge_fixture.c"), str(obj), "-o", str(cls.combined)], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def spawn(self, *, bridge=None, timeout=2, controller=None, allowed_targets=("filehelper",), extra_env=None):
        sender = LinuxNativeSender(timeout=timeout, allowed_targets=allowed_targets)
        env = os.environ.copy()
        env.update(sender.environment(), LD_PRELOAD=str(bridge or self.bridge))
        env.update(extra_env or {})
        if controller is not None:
            env["HELPER_LINUX_CONTROLLER_PID"] = str(controller)
        child = subprocess.Popen([str(self.fixture)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        sender.bind_pid(child.pid)
        def stop():
            sender.close()
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)
            child.stderr.close()
        self.addCleanup(stop)
        return sender, child

    def test_native_scheduler_and_repeated_unicode_send(self):
        sender, child = self.spawn()
        self.assertTrue(sender.wait_ready(process=child))
        self.assertTrue(sender.ensure_context())
        for text in ('你好，换行\n带"引号"和\\反斜杠 😺', '长文本' * 10000 + '_END'):
            self.assertTrue(sender.send_text("filehelper", text))
        self.assertIsNone(child.poll())
        self.assertEqual(sender.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sender.path.parent.stat().st_mode & 0o777, 0o700)

    def test_native_failure_is_not_success(self):
        sender, child = self.spawn()
        sender.wait_ready(process=child)
        with self.assertRaisesRegex(LinuxSendError, "fixture rejected"):
            sender.send_text("filehelper", "NATIVE_REJECT")
        self.assertIsNone(child.poll())

    def mention_sender(self, timeout=2):
        folder = Path(tempfile.mkdtemp(dir=self.dir))
        profile, report = folder / 'profile', folder / 'mentions'
        profile.write_text('wxid_self_fixture')
        sender, child = self.spawn(timeout=timeout, allowed_targets=('123@chatroom', 'filehelper'),
            extra_env={'HELPER_FIXTURE_PROFILE_FILE':str(profile), 'HELPER_FIXTURE_MENTION_REPORT':str(report)})
        sender.wait_ready(process=child)
        sender.bind_account('wxid_self_fixture')
        return sender, child, profile, report

    def test_native_mentions_preserve_ids_and_unicode_separately_from_display(self):
        sender, child, _, report = self.mention_sender()
        self.assertIn('group_mentions', sender.features)
        text = '@显示名字\u2005\n你好，第二行 😺'
        self.assertTrue(sender.send_at_text('123@chatroom', text, ['wxid_first', 'wxid_second']))
        self.assertEqual(report.read_text(), '123@chatroom\nwxid_first,wxid_second\n' + text + '\n')
        self.assertIsNone(child.poll())

    def test_plugin_reply_at_uses_linux_backend_and_mentions_only_first_chunk(self):
        from wxbot_framework.linux_messages import LinuxTextAdapter
        from wxbot_framework.sdk import Message, PluginContext, ServiceFacade
        sender, child, _, report = self.mention_sender()
        backend = LinuxNativeTextBackend(self.fixture, self.recv, 'wxid_self_fixture',
            send_bridge_path=self.bridge, send_targets=('123@chatroom',))
        backend._sender, backend.process = sender, child
        backend.adapter = LinuxTextAdapter('wxid_self_fixture', backend.session_id)
        service = ServiceFacade(text_delay={'enabled':False})
        self.addCleanup(service.close)
        service.bind(backend)
        calls = []
        original = sender.send_text
        def plain(target, text):
            calls.append(text)
            return original(target, text)
        sender.send_text = plain
        context = PluginContext('fixture', {}, service, [])
        message = Message('test', '123@chatroom', 'wxid_peer', is_group=True,
                          session_id=backend.session_id)
        self.assertIn('group.text.mention', backend.capabilities)
        context.reply_at(message, '文' * 800, display_name='测试成员')
        lines = report.read_text().splitlines()
        self.assertEqual(lines[:2], ['123@chatroom', 'wxid_peer'])
        first = '\n'.join(lines[2:])
        self.assertEqual(first + ''.join(calls), '@测试成员\u2005\n' + '文' * 800)
        self.assertLessEqual(len(first.encode('utf-8')), 1023)
        self.assertEqual(report.read_text().count('123@chatroom'), 1)

    def test_mentions_reject_invalid_members_and_scope_without_native_call(self):
        sender, _, _, report = self.mention_sender()
        for room, members in [('filehelper',['wxid_a']), ('123@chatroom',[]),
                ('123@chatroom',['wxid_a']*2), ('123@chatroom',['filehelper']),
                ('123@chatroom',['gh_account']), ('123@chatroom',['notify@all']), ('123@chatroom',['a\nb']),
                ('123@chatroom',[f'wxid_{i}' for i in range(9)])]:
            with self.assertRaises(ValueError):sender.send_at_text(room, 'test', members)
        with self.assertRaises(LinuxSendError):sender.send_at_text('456@chatroom', 'test', ['wxid_a'])
        for text in ('', 'nul\0text', '字'*342):
            with self.assertRaises(ValueError):sender.send_at_text('123@chatroom', text, ['wxid_a'])
        sender.features = frozenset()
        with self.assertRaisesRegex(LinuxSendError, '重新编译'):sender.send_at_text('123@chatroom','test',['wxid_a'])
        self.assertFalse(report.exists())

    def test_native_mention_validation_cannot_be_bypassed_by_raw_frame(self):
        sender, _, _, report = self.mention_sender()
        sender.ensure_context()
        for payload in (b'filehelper\nwxid_a\ntext', b'123@chatroom\nwxid_a,wxid_a\ntext',
                b'123@chatroom\nnotify@all\ntext', b'123@chatroom\nfilehelper\ntext',
                b'123@chatroom\nwxid_a,\ntext', b'123@chatroom\nwxid_a\n'+b'x'*1024):
            with self.assertRaises(LinuxSendError):sender._result(sender._request(b'M', payload), sending=True)
        self.assertFalse(report.exists())

    def test_native_mention_rechecks_bound_account(self):
        sender, _, profile, report = self.mention_sender()
        sender.ensure_context()
        profile.write_text('wxid_other')
        with self.assertRaisesRegex(LinuxSendError, 'account changed'):
            sender.send_at_text('123@chatroom', 'test', ['wxid_a'])
        self.assertFalse(report.exists())

    def test_mention_timeout_does_not_retry(self):
        sender, _, _, report = self.mention_sender(timeout=.02)
        sender.ensure_context()
        with self.assertRaises(LinuxSendIndeterminate):
            sender.send_at_text('123@chatroom', 'CALLBACK_DELAY', ['wxid_a'])
        time.sleep(.25)
        self.assertEqual(report.read_text().count('CALLBACK_DELAY'),1)

    def test_native_mention_rejection_and_lost_reply(self):
        sender, child, _, report = self.mention_sender()
        with self.assertRaisesRegex(LinuxSendError, 'fixture mention rejected'):
            sender.send_at_text('123@chatroom', 'NATIVE_REJECT', ['wxid_a'])
        with self.assertRaises(LinuxSendIndeterminate):
            sender.send_at_text('123@chatroom', 'FORCE_EOF', ['wxid_a'])
        self.assertEqual(child.wait(timeout=3),0)
        self.assertEqual(report.read_text().count('FORCE_EOF'),1)

    def test_reply_lost_has_indeterminate_result_without_retry(self):
        sender, child = self.spawn()
        sender.wait_ready(process=child)
        with self.assertRaises(LinuxSendIndeterminate):
            sender.send_text("filehelper", "FORCE_EOF")
        self.assertEqual(child.wait(timeout=3), 0)

    def test_timeout_does_not_report_success_or_retry(self):
        sender, child = self.spawn(timeout=0.02)
        sender.wait_ready(process=child)
        sender.ensure_context()
        with self.assertRaises(LinuxSendIndeterminate):
            sender.send_text("filehelper", "CALLBACK_DELAY")
        time.sleep(0.15)
        self.assertIsNone(child.poll())

    def test_close_disables_sender_and_keeps_client_running(self):
        sender, child = self.spawn()
        sender.wait_ready(process=child)
        sender.close()
        sender.close()
        self.assertIsNone(child.poll())
        with self.assertRaisesRegex(LinuxSendError, "未运行"):
            sender.send_text("filehelper", "AFTER_STOP")

    def test_unverified_target_and_invalid_text_are_rejected(self):
        sender, child = self.spawn()
        sender.wait_ready(process=child)
        for chat in ("wxid_unverified_fixture", "123456@chatroom", None, []):
            with self.assertRaises(LinuxSendError):
                sender.send_text(chat, "no send")
        for text in ("", "a\0b", 12):
            with self.assertRaises(ValueError):
                sender.send_text("filehelper", text)
        self.assertTrue(sender.send_text("filehelper", "VALID_AFTER_REJECTION"))

    def test_unknown_executable_is_rejected_without_killing_it(self):
        sender, child = self.spawn(bridge=self.production)
        with self.assertRaisesRegex(LinuxSendError, "未就绪"):
            sender.wait_ready(timeout=0.3, process=child)
        self.assertIsNone(child.poll())

    def test_wrong_controller_is_rejected(self):
        sender, child = self.spawn(controller=os.getpid() + 100000)
        with self.assertRaisesRegex(LinuxSendError, "未就绪"):
            sender.wait_ready(timeout=0.2, process=child)
        self.assertIsNone(child.poll())

    def test_client_checks_native_process_identity(self):
        sender, child = self.spawn()
        sender.wait_ready(process=child)
        sender.expected_pid += 100000
        with self.assertRaises(PermissionError):
            sender.ensure_context()
        sender.expected_pid = child.pid

    def test_native_server_rejects_other_process(self):
        sender, child = self.spawn()
        sender.wait_ready(process=child)
        script = ("import socket,sys\ns=socket.socket(socket.AF_UNIX);s.settimeout(2);s.connect(sys.argv[1])\n"
                  "try:\n s.sendall(b'H');assert s.recv(256)==b''\nexcept ConnectionResetError:\n pass\n")
        result = subprocess.run([sys.executable, "-c", script, str(sender.path)], capture_output=True, timeout=4)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertTrue(sender.send_text("filehelper", "AFTER_REJECTED_CLIENT"))

    def test_backend_combines_receive_and_send_without_extra_process(self):
        backend = LinuxNativeTextBackend(self.combined, self.recv, "wxid_self_fixture",
            send_bridge_path=self.bridge, test_markers_only=False,
            _expected_executable_sha256=hashlib.sha256(self.combined.read_bytes()).hexdigest())
        messages = []
        try:
            backend.start(messages.append)
            self.assertIn("text.send", backend.capabilities)
            self.assertNotIn("group.text.send", backend.capabilities)
            self.assertTrue(backend.send_text("filehelper", "COMBINED_NATIVE_SEND"))
            deadline = time.monotonic() + 3
            while not messages and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(messages)
            self.assertEqual(messages[0].sender_id, "wxid_friend_fixture")
            self.assertTrue(backend.stop()["wechat_left_running"])
            self.assertEqual(backend.process.wait(timeout=5), 0)
        finally:
            backend.stop()
            if backend.process and backend.process.poll() is None:
                backend.process.kill()
                backend.process.wait()

    def test_backend_explicit_private_and_group_send_targets(self):
        backend = LinuxNativeTextBackend(self.combined, self.recv, "wxid_self_fixture",
            send_bridge_path=self.bridge, send_targets=("wxid_friend_fixture", "123456789@chatroom"),
            test_markers_only=False,
            _expected_executable_sha256=hashlib.sha256(self.combined.read_bytes()).hexdigest())
        try:
            self.assertIn("friend.text.send", backend.capabilities)
            self.assertIn("group.text.send", backend.capabilities)
            backend.start(lambda message: None)
            self.assertTrue(backend.send_text("wxid_friend_fixture", "FRIEND_SEND"))
            self.assertTrue(backend.send_text("123456789@chatroom", "GROUP_SEND"))
            with self.assertRaises(LinuxSendError):
                backend.send_text("wxid_unlisted_fixture", "FORBIDDEN_SEND")
        finally:
            backend.stop()
            if backend.process and backend.process.poll() is None:
                backend.process.terminate()
                backend.process.wait(timeout=3)

    def test_framework_loads_group_sender_plugin_and_replies_through_native_backend(self):
        from wxbot_framework.config import ConfigStore
        from wxbot_framework.runtime import BotApplication
        backend = LinuxNativeTextBackend(self.combined, self.recv, "wxid_self_fixture",
            send_bridge_path=self.bridge, send_targets=("123456789@chatroom",), test_markers_only=False,
            _expected_executable_sha256=hashlib.sha256(self.combined.read_bytes()).hexdigest())
        with tempfile.TemporaryDirectory(prefix="helper-linux-plugin-") as temporary:
            root = Path(temporary)
            plugin = root / "plugins/echo"
            plugin.mkdir(parents=True)
            (plugin / "plugin.json").write_text(json.dumps({"api": "v1", "requires": ["group.text.receive", "group.text.send"]}))
            (plugin / "__init__.py").write_text(
                "class Plugin:\n def __init__(self): self.result=None\n def on_message(self,m,c): self.result=c.reply(m,'FIXTURE_GROUP_REPLY')\n")
            config = json.loads((ROOT / "bot_config.example.json").read_text())
            config.update(console=False, chat_allowlist=["123456789@chatroom"], members_config_file=None,
                          text_send_delay={"enabled": False}, plugins={"echo": {"enabled": True}})
            path = root / "bot_config.json"
            path.write_text(json.dumps(config))
            app = BotApplication(root, ConfigStore(path), backend_factory=lambda: backend,
                                 initial_capabilities=backend.capabilities)
            try:
                app.plugins.reload()
                self.assertIn("echo", app.plugins.records)
                app._connect()
                deadline = time.monotonic() + 4
                while app.plugins.records["echo"].instance.result is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(app.plugins.records["echo"].instance.result)
                self.assertEqual(app.plugins.records["echo"].errors, 0)
            finally:
                app.stop()
                if backend.process and backend.process.poll() is None:
                    backend.process.terminate()
                    backend.process.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
