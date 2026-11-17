"""Friend removal contract exercised with fake contacts and transport only."""
import http.client
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from text_service import NativeTextService
from wxbot_framework.panel_http import PanelServer
from wxbot_framework.web_panel import PanelController, PanelError


class FriendDeleteTests(unittest.TestCase):
    def test_runner_classifies_only_a_confirmed_native_reply(self):
        from run_selftest import _friend_delete_status

        def classify(native_code, *, replied=True, code=0, cleanup_code=0):
            return _friend_delete_status({"code": code, "cleanup_code": cleanup_code,
                "report": {"operation": "friend_delete", "replied": replied, "native_code": native_code}})

        self.assertEqual(classify(0), "deleted")
        self.assertEqual(classify(-30), "rejected")
        self.assertEqual(classify(-0x1b1), "unknown")
        self.assertEqual(classify(0, replied=False), "unknown")
        self.assertEqual(classify(0, code=1), "unknown")
        self.assertEqual(classify(0, cleanup_code=1), "unknown")
        self.assertEqual(classify(False), "unknown")
        self.assertEqual(_friend_delete_status({"code": 0, "cleanup_code": 0}), "unknown")
        self.assertEqual(_friend_delete_status({"code": 0, "cleanup_code": 0, "report": None}), "unknown")

    def test_native_service_uses_current_friend_and_bound_account(self):
        account, target, session = "wxid_fake_self", "wxid_fake_peer", "fake-session"
        roster = {"account": account, "session_id": session, "contacts": [{"wxid": target}]}
        fake = SimpleNamespace(
            wechat_version="4.2.0.12", _calls=threading.RLock(),
            contacts=Mock(return_value=roster), _check_account=Mock(),
            pid=7, preserve_reports=False, self_wxid=account, _friend_session_id=session,
            _run=Mock(return_value={"delete_status": "deleted"}))
        self.assertEqual(NativeTextService.delete_friend(fake, target), "deleted")
        fake._run.assert_called_once_with("--delete-friend", pid=7, keep_loaded=True,
                                          preserve_report=False, account=account, target=target)
        fake._run.reset_mock()
        with self.assertRaises(ValueError):
            NativeTextService.delete_friend(fake, "wxid_absent")
        fake._friend_session_id = "different-session"
        with self.assertRaises(RuntimeError):
            NativeTextService.delete_friend(fake, target)
        fake.wechat_version = "4.2.0.11"
        with self.assertRaises(RuntimeError):
            NativeTextService.delete_friend(fake, target)
        fake._run.assert_not_called()

    def test_panel_requires_confirmation_and_deduplicates_each_outcome(self):
        for native, panel in (("deleted", "deleted"), ("rejected", "failed"), ("unknown", "unknown")):
            with self.subTest(native=native):
                controller = PanelController.__new__(PanelController)
                controller.lock = threading.RLock()
                controller.send_jobs = {}
                controller.state = "running"
                service = SimpleNamespace(session_is_current=Mock(return_value=True),
                                          delete_friend=Mock(return_value=native))
                backend = SimpleNamespace(capabilities={"friend.delete"})
                controller.app = SimpleNamespace(_connection_ready=True, backend=backend, service=service,
                                                 config={"wechat_account": "wxid_fake_self"})
                data = {"request_id": "fixture-friend-delete-01", "session_id": "fake-session",
                        "account": "wxid_fake_self", "wxid": "wxid_fake_peer", "keep_history": True}
                service.session_is_current.return_value = False
                with self.assertRaises(PanelError):
                    controller.delete_friend(data)
                service.session_is_current.return_value = True
                with self.assertRaises(PanelError):
                    controller.delete_friend(dict(data, keep_history=False))
                backend.capabilities = set()
                with self.assertRaises(PanelError):
                    controller.delete_friend(data)
                backend.capabilities = {"friend.delete"}
                controller.delete_friend(data)
                for _ in range(100):
                    if controller.send_status(data["request_id"])["status"] != "sending":
                        break
                    time.sleep(0.01)
                self.assertEqual(controller.delete_friend(data)["status"], panel)
                service.delete_friend.assert_called_once_with("wxid_fake_peer", expected_session="fake-session")
                if native == "unknown":
                    with self.assertRaises(PanelError):
                        controller.delete_friend(dict(data, request_id="fixture-friend-delete-02"))

    def test_runner_rejects_invalid_operation_before_opening_process(self):
        import run_selftest
        with patch.object(run_selftest, "find_wechat") as find, patch.object(run_selftest, "check") as check:
            with self.assertRaises(ValueError):
                run_selftest.run("--delete-friend", bridge_path=run_selftest.TEXT_DLL,
                                 account="wxid_fake_self", target="wxid_fake_peer")
            with self.assertRaises(ValueError):
                run_selftest.run("--delete-friend", bridge_path=run_selftest.TEXT_DLL, keep_loaded=True,
                                 account="wxid_fake_self", target="wxid_fake_self")
            find.assert_not_called()
            check.assert_not_called()

    def test_http_mutation_requires_panel_token(self):
        callback = Mock(return_value={"status": "sending"})
        server = PanelServer(SimpleNamespace(delete_friend=callback), 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        for token, status in ((None, 403), (server.token, 200)):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
            try:
                headers = {"Content-Type": "application/json"}
                if token:
                    headers["X-Helper-Token"] = token
                connection.request("POST", "/api/friend-delete", json.dumps({"wxid": "wxid_fake_peer"}), headers)
                response = connection.getresponse()
                self.assertEqual(response.status, status)
                response.read()
            finally:
                connection.close()
        callback.assert_called_once_with({"wxid": "wxid_fake_peer"})
