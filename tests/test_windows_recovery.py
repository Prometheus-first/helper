"""Windows recovery fixtures. No WeChat process is opened or launched."""
from pathlib import Path
import struct
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_web_panel import PanelFixture
from wxbot_framework import windows_recovery as recovery

ACCOUNT = "wxid_test_robot"


class WindowsRecoveryPanelTests(PanelFixture):
    def setUp(self):
        super().setUp()
        self.controller.bind_account(self.accounts[0])

    def test_default_enabled_and_account_guard_for_opt_out(self):
        panel = self.controller
        self.assertTrue(panel.configuration()["windows_auto_recovery"])
        with self.assertRaisesRegex(RuntimeError, "账号已变化"):
            panel.save_configuration({"windows_auto_recovery": False, "expected_account": "wxid_other"})
        panel.save_configuration({"windows_auto_recovery": False, "expected_account": ACCOUNT})
        self.assertFalse(panel.configuration()["windows_auto_recovery"])
        replacement = {"pid": 1235, "wxid": "wxid_other", "name": "合成账号"}
        self.accounts.append(replacement)
        panel.bind_account(replacement)
        self.assertTrue(panel.configuration()["windows_auto_recovery"])

    def test_hung_request_stays_unknown_and_only_original_process_is_terminated(self):
        panel = self.controller
        panel.start()
        app, token = panel.app, panel.recovery.cancel_event
        worker = panel.worker
        backend = app.backend
        backend.raw = SimpleNamespace(_owns_bridge=True, _channel=None, pid=1234)
        job_id = "fixture-request-12345"
        job = panel.send_jobs[job_id] = {
            "request_id": job_id, "session_id": "old-session", "status": "sending", "error": "",
        }
        target = Mock(executable=Path("C:/fixture/Weixin.exe"))
        target.account.return_value = ACCOUNT
        target.active.return_value = {70: recovery.NATIVE_STALL_MS}
        next_process = Mock(pid=2468)
        next_process.poll.return_value = None
        effects = []
        target.terminate.side_effect = lambda: effects.append("old exited")

        def start_client(executable):
            self.assertEqual(executable, target.executable)
            self.assertEqual(effects, ["old exited"])
            return next_process

        with (patch.object(recovery, "launch_replacement", side_effect=start_client) as launch,
              patch.object(recovery, "replacement_account", side_effect=[None, ACCOUNT, ACCOUNT]),
              patch.object(token, "wait", side_effect=lambda _: worker.join(2) or token.is_set())):
            self.assertTrue(panel.recovery._recover(app, backend, token, target, 70))
        target.terminate.assert_called_once_with()
        launch.assert_called_once_with(target.executable)
        self.assertTrue(app.stopped_event.is_set())
        self.assertIsNot(panel.app, app)
        self.assertEqual(panel.app.config["wechat_pid"], 2468)
        self.assertEqual(panel.send_status(job_id)["status"], "unknown")
        panel._finish_send_job(job, "sent")
        self.assertEqual(panel.send_status(job_id)["status"], "unknown")
        with patch.object(recovery, "replacement_account", return_value="wxid_other"):
            self.assertFalse(panel.app._replacement_guard())

    def test_wrong_account_changed_slot_and_cooldown_cannot_terminate(self):
        panel = self.controller
        panel.start()
        app, token = panel.app, panel.recovery.cancel_event
        app.backend.raw = SimpleNamespace(_owns_bridge=True, _channel=None, pid=1234)
        target = Mock()
        target.account.return_value = "wxid_other"
        target.active.return_value = {70: recovery.NATIVE_STALL_MS}
        run = lambda: panel.recovery._recover(app, app.backend, token, target, 70)
        self.assertFalse(run())
        target.account.return_value = ACCOUNT
        target.active.return_value = {71: recovery.NATIVE_STALL_MS}
        self.assertFalse(run())
        target.active.return_value = {70: recovery.NATIVE_STALL_MS - 1}
        self.assertFalse(run())
        target.active.return_value = {70: recovery.NATIVE_STALL_MS}
        panel.recovery.attempts[ACCOUNT] = [recovery.time.monotonic()]
        self.assertFalse(run())
        self.assertEqual(panel.recovery.status, "limited")
        panel.recovery.attempts.clear()
        token.set()
        self.assertFalse(run())
        target.terminate.assert_not_called()

    def test_verified_replacement_waits_for_old_worker_cleanup_before_resuming(self):
        panel = self.controller
        new_process = Mock()
        new_process.poll.return_value = None
        worker = Mock()
        worker.is_alive.side_effect = [True, False]
        token = Mock()
        token.wait.return_value = False
        token.is_set.return_value = False
        with (patch.object(recovery, "replacement_account", return_value=ACCOUNT),
              patch.object(panel, "start") as start):
            panel.recovery._wait_for_account(new_process, ACCOUNT, token, old_worker=worker)
        self.assertEqual(worker.is_alive.call_count, 2)
        start.assert_called_once_with(_replacement=(new_process, ACCOUNT, token))

    def test_replacement_with_another_login_does_not_resume_robot(self):
        panel = self.controller
        new_process = Mock()
        new_process.poll.return_value = None
        token = Mock()
        token.wait.return_value = False
        token.is_set.return_value = False
        with (patch.object(recovery, "replacement_account", return_value="wxid_other"),
              patch.object(panel, "start") as start):
            panel.recovery._wait_for_account(new_process, ACCOUNT, token)
        start.assert_not_called()
        self.assertEqual(panel.recovery.status, "wrong_account")


class WindowsRecoveryProbeTests(unittest.TestCase):
    def test_probe_ignores_idle_reused_and_partially_published_slots(self):
        def slot_dump(*slots):
            return b"".join(struct.pack("<qq", *slot) for slot in slots).ljust(recovery.SLOTS.size, b"\0")

        first = slot_dump((20, 100), (-1, 120), (21, 140), (23, 90_100), (24, 50))
        second = slot_dump((20, 100), (22, 120), (0, 140), (23, 90_100), (24, 55))
        self.assertEqual(recovery._active_slots(first, second, 90_000), {20: 89_900})

    @unittest.skipUnless(sys.platform == "win32", "Windows process handles")
    def test_terminate_pinned_synthetic_process_leaves_another_child_running(self):
        children = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                    creationflags=subprocess.CREATE_NO_WINDOW) for _ in range(2)]
        try:
            target = object.__new__(recovery.RecoveryTarget)
            target.api = recovery._kernel()
            target.handle = recovery.checked(target.api.OpenProcess(0x101011, False, children[0].pid), "fixture")
            try:
                target.terminate()
                self.assertIsNotNone(children[0].poll())
                self.assertIsNone(children[1].poll())
            finally:
                target.close()
        finally:
            for child in children:
                if child.poll() is None:
                    child.terminate()
                child.wait(5)
