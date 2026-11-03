import hashlib
import json
import os
import sys
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

import wxbot_framework.linux_backend as linux_backend
from wxbot_framework.config import ConfigStore
from wxbot_framework.runtime import BotApplication
from wxbot_framework.linux_backend import LinuxNativeTextBackend
from wxbot_framework.linux_messages import LinuxTextAdapter
from wxbot_framework.linux_wire import LocalEventListener, acknowledge_text_event, read_bridge_ready, read_text_event

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "native" / "linux" / "text_bridge.c"
FIXTURE = ROOT / "tests" / "fixtures" / "linux_bridge_fixture.c"


@unittest.skipUnless(os.name == "posix" and hasattr(socket, "SO_PEERCRED") and shutil.which("gcc"),
                     "requires Linux and GCC")
class LinuxNativeBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="helper-bridge-tests-")
        cls.directory = Path(cls.tmp.name)
        cls.fixture = cls.directory / "fixture"
        cls.marker_fixture = cls.directory / "marker-fixture"
        cls.two_fixture = cls.directory / "two-fixture"
        cls.cli_fixture = cls.directory / "cli-fixture"
        cls.test_bridge = cls.directory / "fixture-bridge.so"
        cls.production_bridge = cls.directory / "production-bridge.so"
        cls.failed_bridge = cls.directory / "failed-bridge.so"
        subprocess.run(["gcc", "-O2", "-rdynamic", "-fno-builtin", "-o", str(cls.fixture), str(FIXTURE)], check=True)
        subprocess.run(["gcc", "-O2", "-rdynamic", "-fno-builtin", "-DHELPER_FIXTURE_MARKER", "-o", str(cls.marker_fixture), str(FIXTURE)], check=True)
        subprocess.run(["gcc", "-O2", "-rdynamic", "-fno-builtin", "-DHELPER_FIXTURE_TWO", "-o", str(cls.two_fixture), str(FIXTURE)], check=True)
        subprocess.run(["gcc", "-O2", "-rdynamic", "-fno-builtin", "-o", str(cls.cli_fixture), str(ROOT / "tests" / "fixtures" / "linux_bridge_cli_fixture.c")], check=True)
        common = ["gcc", "-O2", "-fPIC", "-shared", "-pthread"]
        subprocess.run(common + ["-DHELPER_FIXTURE_TEST", "-o", str(cls.test_bridge), str(SOURCE), "-ldl"], check=True)
        subprocess.run(common + ["-o", str(cls.production_bridge), str(SOURCE), "-ldl"], check=True)
        subprocess.run(common + ["-DHELPER_FIXTURE_TEST", "-DHELPER_FIXTURE_FAIL_RESTORE", "-o", str(cls.failed_bridge), str(SOURCE), "-ldl"], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def _spawn(self, listener, bridge, *, controller=None, marker_mode=False, fixture=None):
        env = os.environ.copy()
        env.update(LD_PRELOAD=str(bridge), HELPER_LINUX_EVENT_SOCKET=os.fspath(listener.path),
                   HELPER_LINUX_CONTROLLER_PID=str(os.getpid() if controller is None else controller))
        if marker_mode:
            env["HELPER_LINUX_TEST_MARKERS"] = "1"
        child = subprocess.Popen([str(fixture or self.fixture)], env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        listener.bind_pid(child.pid)
        self.addCleanup(lambda: child.poll() is None and (child.kill(), child.wait()))
        return child

    def test_native_frame_long_group_message_and_graceful_shutdown(self):
        with LocalEventListener() as listener:
            child = self._spawn(listener, self.test_bridge)
            with listener.accept(timeout=4) as conn:
                conn.settimeout(4)
                read_bridge_ready(conn)
                event = read_text_event(conn)
                msg = LinuxTextAdapter("wxid_self_fixture", "fixture").convert(event)
                self.assertEqual(msg.chat_id, "123456789@chatroom")
                self.assertEqual(msg.sender_id, "wxid_friend_fixture")
                self.assertEqual(msg.text, "x" * 200000 + "_FIXTURE_END")
                self.assertEqual(event["sequence"], 1)
                acknowledge_text_event(conn, event["sequence"])
            out, err = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, err)
            self.assertIn("fixture_ok", out)

    def test_unacknowledged_event_is_retried_with_same_sequence(self):
        with LocalEventListener() as listener:
            child = self._spawn(listener, self.test_bridge)
            with listener.accept(timeout=4) as conn:
                conn.settimeout(4)
                read_bridge_ready(conn)
                first = read_text_event(conn)
                # Drop the connection without sending an ACK.
            with listener.accept(timeout=4) as conn:
                conn.settimeout(4)
                read_bridge_ready(conn)
                second = read_text_event(conn)
                self.assertEqual(first, second)
                acknowledge_text_event(conn, second["sequence"])
            out, err = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, err)
            self.assertIn("fixture_ok", out)
    def test_stop_ack_disables_further_capture(self):
        with LocalEventListener() as listener:
            child = self._spawn(listener, self.test_bridge, fixture=self.two_fixture)
            with listener.accept(timeout=4) as conn:
                conn.settimeout(1)
                read_bridge_ready(conn)
                first = read_text_event(conn)
                acknowledge_text_event(conn, first["sequence"], stop=True)
                with self.assertRaises((socket.timeout, EOFError, ConnectionResetError)):
                    read_text_event(conn)
            out, err = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, err)
            self.assertIn("fixture_ok", out)
    def test_closing_stop_pipe_disables_capture_without_stopping_wechat(self):
        read_fd, write_fd = os.pipe()
        child = None
        try:
            with LocalEventListener() as listener:
                env = os.environ.copy()
                env.update(LD_PRELOAD=str(self.test_bridge), HELPER_LINUX_EVENT_SOCKET=os.fspath(listener.path),
                           HELPER_LINUX_CONTROLLER_PID=str(os.getpid()), HELPER_LINUX_STOP_FD=str(read_fd))
                child = subprocess.Popen([str(self.two_fixture)], env=env, pass_fds=(read_fd,),
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                os.close(read_fd)
                read_fd = -1
                listener.bind_pid(child.pid)
                with listener.accept(timeout=4) as conn:
                    conn.settimeout(1)
                    read_bridge_ready(conn)
                    event = read_text_event(conn)
                    acknowledge_text_event(conn, event["sequence"])
                    os.close(write_fd)
                    write_fd = -1
                    with self.assertRaises((socket.timeout, EOFError, ConnectionResetError)):
                        read_text_event(conn)
                out, err = child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0, err)
                self.assertIn("fixture_ok", out)
        finally:
            if read_fd >= 0:
                os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)
            if child is not None and child.poll() is None:
                child.kill()
                child.wait()
    def test_failed_code_protection_restores_entry_without_killing_client(self):
        with LocalEventListener() as listener:
            child = self._spawn(listener, self.failed_bridge)
            with self.assertRaises(socket.timeout):
                listener.accept(timeout=2)
            out, err = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, err)
            self.assertIn("fixture_ok", out)
            self.assertIn("bridge initialization failed", err)

    def test_bridge_rejects_controller_with_wrong_pid(self):
        with LocalEventListener() as listener:
            child = self._spawn(listener, self.test_bridge, controller=os.getpid() + 100000)
            with self.assertRaises(socket.timeout):
                listener.accept(timeout=2)
            out, err = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, err)
            self.assertIn("fixture_ok", out)

    def test_test_mode_filters_other_messages_and_accepts_marker(self):
        with LocalEventListener() as listener:
            child = self._spawn(listener, self.test_bridge, marker_mode=True)
            with listener.accept(timeout=4) as conn:
                conn.settimeout(1)
                read_bridge_ready(conn)
                with self.assertRaises((socket.timeout, EOFError, ConnectionResetError)):
                    read_text_event(conn)
            out, err = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, err)
            self.assertIn("fixture_ok", out)
        with LocalEventListener() as listener:
            child = self._spawn(listener, self.test_bridge, marker_mode=True, fixture=self.marker_fixture)
            with listener.accept(timeout=4) as conn:
                conn.settimeout(4)
                read_bridge_ready(conn)
                event = read_text_event(conn)
                self.assertIn("WX_LINUX_BRIDGE_08", event["text"])
                acknowledge_text_event(conn, event["sequence"])
            out, err = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, err)
            self.assertIn("fixture_ok", out)
    def test_real_test_controller_receives_both_message_types(self):
        digest = hashlib.sha256(self.cli_fixture.read_bytes()).hexdigest()
        code = ("import sys; from tools import linux_text_receive_test as m; "
                "m.EXPECTED=sys.argv[1]; sys.argv=[\"probe\",\"--wechat\",sys.argv[2],"
                "\"--bridge\",sys.argv[3],\"--timeout\",\"5\"]; m.main()")
        result = subprocess.run([sys.executable, "-B", "-c", code, digest, str(self.cli_fixture),
                                 str(self.test_bridge)], cwd=ROOT, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("私聊测试消息已进入框架", result.stdout)
        self.assertIn("群聊测试消息已进入框架", result.stdout)
        self.assertIn("测试接收已停用", result.stdout)
    def _backend(self, fixture=None):
        path = fixture or self.fixture
        return LinuxNativeTextBackend(path, self.test_bridge, "wxid_self_fixture", test_markers_only=False,
                                      _expected_executable_sha256=hashlib.sha256(path.read_bytes()).hexdigest())

    def test_backend_requires_bridge_ready_before_reporting_success(self):
        digest = hashlib.sha256(self.fixture.read_bytes()).hexdigest()
        backend = LinuxNativeTextBackend(self.fixture, self.production_bridge, "wxid_self_fixture",
                                         _expected_executable_sha256=digest, _ready_timeout=0.7)
        try:
            with self.assertRaisesRegex(RuntimeError, "桥接未就绪"):
                backend.start(lambda message: self.fail("unexpected message"))
            self.assertIsInstance(backend.last_error, RuntimeError)
            self.assertIsNone(backend._listener)
            self.assertIsNone(backend._stop_fd)
            self.assertIsNotNone(backend.process)
            self.assertIsNone(backend.process.poll(), "failed bridge must not kill the client")
            self.assertEqual(backend.process.wait(timeout=5), 0)
        finally:
            backend.stop()
            if backend.process and backend.process.poll() is None:
                backend.process.kill()
                backend.process.wait()

    def test_experimental_backend_routes_group_and_stops_without_closing_client(self):
        backend = self._backend(self.two_fixture)
        arrived = threading.Event()
        seen = []
        def emit(message):
            seen.append(message)
            arrived.set()
        try:
            backend.start(emit)
            self.assertTrue(arrived.wait(4), "fixture message not delivered")
            self.assertEqual(seen[0].sender_id, "wxid_friend_fixture")
            self.assertEqual(seen[0].chat_id, "123456789@chatroom")
            self.assertEqual(seen[0].text, "x" * 200000 + "_FIXTURE_END")
            result = backend.stop()
            self.assertTrue(result["capture_stop_requested"])
            self.assertTrue(result["wechat_left_running"])
            self.assertEqual(backend.process.wait(timeout=5), 0)
            self.assertEqual(len(seen), 1)
            with self.assertRaises(RuntimeError):
                backend.start(emit)
        finally:
            backend.stop()
            if backend.process and backend.process.poll() is None:
                backend.process.kill()
                backend.process.wait()

    def test_backend_retries_lost_ack_without_duplicate_dispatch(self):
        backend = self._backend()
        original_ack = linux_backend.acknowledge_text_event
        acked = threading.Event()
        seen = []
        calls = []
        def flaky_ack(conn, sequence):
            calls.append(sequence)
            if len(calls) == 1:
                conn.close()
                raise ConnectionResetError("fixture lost ACK")
            original_ack(conn, sequence)
            acked.set()
        try:
            with mock.patch.object(linux_backend, "acknowledge_text_event", side_effect=flaky_ack):
                backend.start(seen.append)
                self.assertTrue(acked.wait(5), "event was not retried")
                self.assertEqual(len(seen), 1)
                self.assertEqual(calls[0], calls[1])
                self.assertIsNone(backend.last_error)
                backend.stop()
            self.assertEqual(backend.process.wait(timeout=5), 0)
        finally:
            backend.stop()
            if backend.process and backend.process.poll() is None:
                backend.process.kill()
                backend.process.wait()

    def test_backend_surfaces_callback_failure(self):
        backend = self._backend()
        failed = threading.Event()
        def fail(_message):
            failed.set()
            raise LookupError("fixture callback failed")
        try:
            backend.start(fail)
            self.assertTrue(failed.wait(4))
            deadline = time.monotonic() + 2
            while backend.last_error is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIsInstance(backend.last_error, LookupError)
        finally:
            backend.stop()
            if backend.process and backend.process.poll() is None:
                backend.process.wait(timeout=5)
    def test_framework_dispatches_linux_text_only_to_supported_plugin(self):
        backend = self._backend()
        with tempfile.TemporaryDirectory(prefix="helper-runtime-") as temp:
            root = Path(temp)
            plugin_root = root / "plugins"
            for name in ("reader", "sender"):
                (plugin_root / name).mkdir(parents=True)
            (plugin_root / "reader" / "__init__.py").write_text(
                "class Plugin:\n def __init__(self): self.seen=[]\n def on_message(self,m,c): self.seen.append(m)\n", encoding="utf8")
            (plugin_root / "reader" / "plugin.json").write_text(
                json.dumps({"api": "v1", "requires": ["group.text.receive"]}), encoding="utf8")
            (plugin_root / "sender" / "__init__.py").write_text(
                "raise RuntimeError('unsupported plugin was imported')\n", encoding="utf8")
            (plugin_root / "sender" / "plugin.json").write_text(
                json.dumps({"api": "v1", "requires": ["text.send"]}), encoding="utf8")
            config = json.loads((ROOT / "bot_config.example.json").read_text(encoding="utf8"))
            config.update(console=False, chat_allowlist=[], members_config_file=None,
                          plugins={"reader": {"enabled": True}, "sender": {"enabled": True}})
            config_path = root / "bot_config.json"
            config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf8")
            app = BotApplication(root, ConfigStore(config_path), backend_factory=lambda: backend,
                                 initial_capabilities=backend.capabilities)
            try:
                # Mirrors normal startup before the backend connects.
                app.plugins.reload()
                self.assertIn("reader", app.plugins.records)
                self.assertNotIn("sender", app.plugins.records)
                app._connect()
                deadline = time.monotonic() + 4
                while not app.plugins.records["reader"].instance.seen and time.monotonic() < deadline:
                    time.sleep(0.01)
                seen = app.plugins.records["reader"].instance.seen
                self.assertEqual(len(seen), 1)
                self.assertEqual(seen[0].sender_id, "wxid_friend_fixture")
                self.assertEqual(seen[0].chat_id, "123456789@chatroom")
                self.assertNotIn("sender", app.plugins.records)
            finally:
                app.stop()
                if backend.process and backend.process.poll() is None:
                    backend.process.wait(timeout=5)
    def test_experimental_backend_isolates_different_account(self):
        backend = LinuxNativeTextBackend(self.fixture, self.test_bridge, "wxid_other_fixture",
                                         test_markers_only=False,
                                         _expected_executable_sha256=hashlib.sha256(self.fixture.read_bytes()).hexdigest())
        seen = []
        try:
            backend.start(seen.append)
            self.assertEqual(backend.process.wait(timeout=5), 0)
            time.sleep(0.25)
            self.assertEqual(seen, [])
        finally:
            backend.stop()

    def test_experimental_backend_rejects_unknown_executable_before_launch(self):
        backend = LinuxNativeTextBackend(self.fixture, self.test_bridge, "wxid_self_fixture")
        with self.assertRaisesRegex(RuntimeError, "版本指纹不匹配"):
            backend.start(lambda message: None)
        self.assertIsNone(backend.process)
        backend.stop()
    def test_unknown_binary_is_not_patched(self):
        with LocalEventListener() as listener:
            child = self._spawn(listener, self.production_bridge)
            with self.assertRaises(socket.timeout):
                listener.accept(timeout=2)
            out, err = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, err)
            self.assertNotIn("receive bridge loaded", err)
            self.assertIn("fixture_ok", out)


if __name__ == "__main__":
    unittest.main()
