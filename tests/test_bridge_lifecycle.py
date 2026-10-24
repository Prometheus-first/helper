"""桥接生命周期回归：模拟原生接口，并用独立测试进程检查 Windows 占用锁。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from wxbot_framework.bridge_lifecycle import BridgeGuard, kernel
from wxbot_framework.config import ConfigStore
from wxbot_framework.runtime import BotApplication
from wxbot_framework.shutdown import ShutdownHandlers

ROOT = Path(__file__).resolve().parent.parent
NATIVE = ROOT / "native"
SCRATCH = ROOT / ".runtime/tests"
CLEAN = {"pid": 123, "code": 0, "dll_unloaded": True}


@unittest.skipUnless(sys.platform == "win32", "Windows 原生服务生命周期")
class NativeLifecycleTests(unittest.TestCase):
    def setUp(self):
        if str(NATIVE) not in sys.path:
            sys.path.insert(0, str(NATIVE))
        import filehelper_service
        self.native = filehelper_service
        self.real_loaded = filehelper_service.loaded_project_bridges
        self.real_cleanup = filehelper_service.cleanup_loaded_bridges
        self.guard = Mock()
        self.guard.target_exited.return_value = False
        self.channel = Mock()
        self.channel.process_exited.return_value = False
        self.thread = Mock()
        self.thread.is_alive.return_value = False
        self.patches = [patch.object(self.native, "BridgeGuard", return_value=self.guard),
                        patch.object(self.native, "loaded_project_bridges", return_value=[]),
                        patch.object(self.native, "cleanup_loaded_bridges", return_value=[dict(CLEAN)]),
                        patch.object(self.native, "ReceiveChannel", return_value=self.channel),
                        patch.object(self.native.threading, "Thread", return_value=self.thread)]
        self.mocks = [item.start() for item in self.patches]
        self.service = self.native.NativeFileHelperService(pid=123)
        self.service._run = Mock(return_value={"pid": 123, "code": 0})

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def test_guard_is_armed_before_loading_and_normal_stop_is_idempotent(self):
        order = []
        self.guard.start_watchdog.side_effect = lambda *_: order.append("watchdog")
        self.service._run.side_effect = lambda *_args, **_kw: order.append("load") or {"pid": 123, "code": 0}
        self.assertTrue(self.service.start())
        self.assertEqual(order, ["watchdog", "load"])
        self.assertEqual(self.service.stop(), CLEAN)
        self.assertEqual(self.service.stop(), CLEAN)
        self.native.cleanup_loaded_bridges.assert_called_once_with(123)
        self.guard.close.assert_called_once()
        self.assertFalse(self.service._owns_bridge)
        self.assertIsNone(self.service._guard)
        self.channel.close.assert_called_once()

    def test_stale_bridge_is_cleaned_before_new_listener(self):
        self.native.loaded_project_bridges.return_value = [self.native.TEXT_DLL]
        order = []
        self.native.cleanup_loaded_bridges.side_effect = lambda _pid: order.append("cleanup") or [dict(CLEAN)]
        self.service._run.side_effect = lambda *_args, **_kw: order.append("load") or {"pid": 123, "code": 0}
        self.service.start()
        self.assertEqual(order, ["cleanup", "load"])
        self.service.stop()

    def test_keyboard_interrupt_during_initialization_keeps_cleanup_responsibility(self):
        self.service._run.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.service.start()
        self.native.cleanup_loaded_bridges.assert_called_once_with(123)
        self.guard.close.assert_called_once()
        self.assertFalse(self.service._owns_bridge)

    def test_channel_preparation_failure_releases_bridge_and_channel(self):
        self.service._prepare_channel = Mock(side_effect=OSError("模拟读取失败"))
        with self.assertRaisesRegex(OSError, "模拟读取失败"):
            self.service.start()
        self.native.cleanup_loaded_bridges.assert_called_once()
        self.channel.close.assert_called_once()
        self.guard.close.assert_called_once()

    def test_busy_native_tasks_keep_ownership_until_cleanup_can_be_retried(self):
        self.service.start()
        self.native.cleanup_loaded_bridges.side_effect = [RuntimeError("原生任务尚未结束"), [dict(CLEAN)]]
        with self.assertRaisesRegex(RuntimeError, "尚未结束"):
            self.service.stop()
        self.assertTrue(self.service._owns_bridge)
        self.guard.close.assert_not_called()
        self.assertEqual(self.service.stop(), CLEAN)
        self.guard.close.assert_called_once()

    def test_guard_shutdown_failure_does_not_lose_successful_unload_result(self):
        self.service.start()
        self.guard.close.side_effect = [TimeoutError("守护暂未退出"), None]
        with self.assertRaises(TimeoutError):
            self.service.stop()
        self.assertFalse(self.service._owns_bridge)
        self.assertEqual(self.service.stop(), CLEAN)
        self.native.cleanup_loaded_bridges.assert_called_once()

    def test_client_exit_skips_calls_into_dead_process_and_releases_guard(self):
        self.service.start()
        self.guard.target_exited.return_value = True
        result = self.service.stop()
        self.assertTrue(result["process_exited"])
        self.assertTrue(result["dll_unloaded"])
        self.native.cleanup_loaded_bridges.assert_not_called()
        self.guard.close.assert_called_once()

    def test_foreign_project_bridge_is_not_adopted_or_unloaded(self):
        self.native.loaded_project_bridges.side_effect = RuntimeError("其他项目的桥接")
        with self.assertRaisesRegex(RuntimeError, "其他项目"):
            self.service.start()
        self.native.cleanup_loaded_bridges.assert_not_called()
        self.service._run.assert_not_called()
        self.guard.close.assert_called_once()

    def test_all_loaded_paths_are_checked_before_removing_any_bridge(self):
        foreign = str(ROOT / "different-project/text_bridge.dll")
        loaded = {self.native.DLL.name.lower(): (1, str(self.native.DLL)),
                  self.native.TEXT_DLL.name.lower(): (2, foreign)}
        with patch.object(self.native, "loaded_project_bridges", self.real_loaded), \
                patch.object(self.native, "modules", return_value=loaded), patch.object(self.native, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "其他项目"):
                self.real_cleanup(123)
            run.assert_not_called()


@unittest.skipUnless(sys.platform == "win32", "Windows 进程间互斥")
class BridgeMutexTests(unittest.TestCase):
    def test_live_owner_blocks_other_process_and_release_allows_restart(self):
        guard = BridgeGuard(os.getpid())
        code = ("import sys;sys.path.insert(0,sys.argv[1]);"
                "from wxbot_framework.bridge_lifecycle import BridgeGuard\n"
                "try:g=BridgeGuard(int(sys.argv[2]))\n"
                "except RuntimeError:sys.exit(0)\n"
                "else:g.close();sys.exit(1)\n")
        try:
            with self.assertRaises(RuntimeError):
                BridgeGuard(os.getpid())
            result = subprocess.run([sys.executable, "-B", "-c", code, str(ROOT), str(os.getpid())],
                                    capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
        finally:
            guard.close()
        replacement = BridgeGuard(os.getpid())
        replacement.close()

    def test_abandoned_mutex_can_be_recovered_after_test_owner_is_terminated(self):
        code = ("import sys;sys.path.insert(0,sys.argv[1]);"
                "from wxbot_framework.bridge_lifecycle import BridgeGuard;"
                "g=BridgeGuard(int(sys.argv[2]));print('READY',flush=True);sys.stdin.readline()")
        child = subprocess.Popen([sys.executable, "-B", "-c", code, str(ROOT), str(os.getpid())],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self.assertEqual(child.stdout.readline().strip(), b"READY")
            # 只结束本测试创建、未连接微信的子进程，模拟强制退出后的占用锁释放。
            child.terminate()
            child.wait(timeout=5)
            guard = BridgeGuard(os.getpid())
            guard.close()
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()


class RuntimeCleanupTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        (self.root / "plugins").mkdir()
        path = self.root / "bot_config.json"
        path.write_text(json.dumps({"backend": "native_text", "members_config_file": None,
                                    "console": False, "plugins": {}, "text_send_delay": {"enabled": False}}), encoding="utf-8")
        self.backend = Mock(pid=123, session_id="test-session", last_error=None)
        self.backend.stop.return_value = dict(CLEAN)
        self.app = BotApplication(self.root, ConfigStore(path), backend_factory=lambda: self.backend)
        self.app._connect()

    def tearDown(self):
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        self.temporary.cleanup()

    def test_shutdown_retries_busy_cleanup_and_keeps_last_result(self):
        self.backend.stop.side_effect = [RuntimeError("模拟原生任务忙"), dict(CLEAN)]
        with patch("wxbot_framework.runtime.time.sleep"):
            self.app.stop()
        self.assertEqual(self.backend.stop.call_count, 2)
        self.assertEqual(self.app.last_cleanup, CLEAN)
        self.app.stop()
        self.assertEqual(self.backend.stop.call_count, 2)
        self.assertEqual(self.app.last_cleanup, CLEAN)

    def test_plugin_shutdown_failure_does_not_prevent_native_cleanup(self):
        self.app.plugins.shutdown = Mock(side_effect=RuntimeError("模拟插件退出异常"))
        with self.assertRaisesRegex(RuntimeError, "插件退出异常"):
            self.app.stop()
        self.backend.stop.assert_called_once()
        self.assertFalse(self.app.service.connected)
        self.assertTrue(self.app.stopped_event.is_set())

    def test_failed_final_cleanup_can_be_retried_without_new_connection(self):
        self.backend.stop.side_effect = [RuntimeError("忙")] * 3 + [dict(CLEAN)]
        with patch("wxbot_framework.runtime.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "原生连接未能清理"):
                self.app.stop()
            self.app.stop()
        self.assertEqual(self.backend.stop.call_count, 4)
        self.assertFalse(self.app.service.connected)

    def test_signal_requests_orderly_stop_instead_of_interrupting_native_call(self):
        previous = signal.getsignal(signal.SIGINT)
        with ShutdownHandlers(self.app):
            signal.raise_signal(signal.SIGINT)
            self.assertTrue(self.app.stop_event.is_set())
            self.assertFalse(self.app.stopped_event.is_set())
            self.app.stop()
        self.assertEqual(signal.getsignal(signal.SIGINT), previous)
        self.backend.stop.assert_called_once()


@unittest.skipUnless(sys.platform == "win32", "Windows 隐藏守护进程")
class WatchdogProcessTests(unittest.TestCase):
    def check_owner_exit(self, forced):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=SCRATCH) as temporary:
            folder = Path(temporary).resolve()
            self.assertTrue(folder.is_relative_to(SCRATCH.resolve()))
            marker = folder / "cleaned.txt"
            helper = folder / "watchdog.py"
            # 目标是测试进程自身，cleanup 只写测试标记，完全不访问微信。
            helper.write_text(
                "import argparse,sys\nfrom pathlib import Path\n"
                f"sys.path.insert(0,{str(ROOT)!r})\n"
                "from wxbot_framework.bridge_lifecycle import watch_owner\n"
                "p=argparse.ArgumentParser()\n"
                "for name in ('owner-pid','wechat-pid','stop-event','ready-event'):p.add_argument('--'+name,required=True)\n"
                "a=p.parse_args()\n"
                f"def cleanup(pid):Path({str(marker)!r}).write_text('cleaned',encoding='utf-8')\n"
                "watch_owner(int(a.owner_pid),int(a.wechat_pid),a.stop_event,a.ready_event,cleanup)\n", encoding="utf-8")
            code = ("import json,sys;sys.path.insert(0,sys.argv[1]);"
                    "from wxbot_framework.bridge_lifecycle import BridgeGuard;"
                    "g=BridgeGuard(int(sys.argv[2]));g.start_watchdog(sys.argv[3]);"
                    "print(json.dumps({'watchdog':g.child.pid}),flush=True);sys.stdin.readline();g.close()")
            owner = subprocess.Popen([sys.executable, "-B", "-c", code, str(ROOT), str(os.getpid()), str(helper)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     creationflags=subprocess.CREATE_NO_WINDOW)
            watchdog_handle = None
            try:
                row = json.loads(owner.stdout.readline())
                watchdog_handle = kernel().OpenProcess(0x100000, False, row["watchdog"])
                self.assertTrue(watchdog_handle)
                if forced:
                    owner.terminate()
                else:
                    owner.stdin.write(b"quit\n")
                    owner.stdin.flush()
                owner.wait(timeout=10)
                self.assertEqual(kernel().WaitForSingleObject(watchdog_handle, 10000), 0)
                self.assertEqual(marker.exists(), forced)
                if not forced:
                    self.assertEqual(owner.returncode, 0, owner.stderr.read().decode(errors="replace"))
            finally:
                if watchdog_handle:
                    kernel().CloseHandle(watchdog_handle)
                if owner.poll() is None:
                    owner.terminate()
                    owner.wait(timeout=5)
                for stream in (owner.stdin, owner.stdout, owner.stderr):
                    stream.close()

    def test_normal_exit_disarms_guardian_without_running_cleanup_twice(self):
        self.check_owner_exit(False)

    def test_force_ending_test_owner_triggers_guardian_and_guardian_exits(self):
        self.check_owner_exit(True)
