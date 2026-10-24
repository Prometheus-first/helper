"""联系人只读接口、会话隔离及本机 HTTP；全部使用合成数据。"""
from copy import deepcopy
from datetime import datetime
import http.client
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_web_panel import PanelFixture
from wxbot_framework.backend import NativeTextBackend, TEXT_CAPABILITIES
from wxbot_framework.robot_hub import RobotProxy
from wxbot_framework.sdk import CapabilityError, PluginContext, ServiceFacade
from wxbot_framework.web_panel import PanelError, PanelServer

ACCOUNT = "wxid_contact_robot"
SESSION = "synthetic-contact-session"


def directory():
    return {"account": ACCOUNT, "session_id": SESSION,
            "contacts": [{"wxid": "wxid_contact_peer", "nickname": "测试好友",
                          "remark": "合成备注", "alias": "demo_account", "sex": "female",
                          "country": "CN", "province": "测试省", "city": "测试市",
                          "signature": "测试签名", "tag_ids": ["7"], "tags": ["测试标签"],
                          "tags_status": "ready"}],
            "tags": [{"id": "7", "name": "测试标签"}],
            "source": "wechat_cache", "complete": False,
            "scanned_at": "2026-01-01T12:00:00+08:00"}


class ContactBackend:
    session_id = SESSION
    pid = 1234

    def __init__(self):
        self.snapshot = directory()
        self.calls = 0
        self.during_read = None

    def contacts(self):
        self.calls += 1
        if self.during_read is not None:
            self.during_read()
        return deepcopy(self.snapshot)

    def stop(self):
        return {}


class ContactServiceTests(unittest.TestCase):
    def setUp(self):
        self.backend = ContactBackend()
        self.service = ServiceFacade(text_delay={"enabled": False})
        self.service.bind(self.backend)
        self.addCleanup(self.service.close)

    def test_capability_and_native_backend_session_translation(self):
        self.assertIn("contact.read", TEXT_CAPABILITIES)
        backend = NativeTextBackend.__new__(NativeTextBackend)
        backend.session_id = SESSION
        snapshot = directory()
        snapshot["session_id"] = "raw-native-session"
        backend.raw = SimpleNamespace(contacts=lambda: snapshot)
        result = backend.contacts()
        self.assertEqual(result["session_id"], SESSION)
        self.assertEqual(result["contacts"], snapshot["contacts"])
        self.assertEqual(snapshot["session_id"], "raw-native-session")

    def test_service_preserves_directory_fields_and_unknown_tags(self):
        self.assertEqual(self.service.contacts(expected_session=SESSION), directory())
        self.backend.snapshot["contacts"][0].update(tags=[], tags_status="unavailable")
        self.assertEqual(self.service.contacts()["contacts"][0]["tags_status"], "unavailable")

    def test_disconnected_and_unimplemented_backends_cannot_read(self):
        self.service.close()
        with self.assertRaisesRegex(RuntimeError, "尚未连接"):
            self.service.contacts()
        self.service.bind(SimpleNamespace(session_id=SESSION, stop=lambda: {}))
        with self.assertRaises(CapabilityError):
            self.service.contacts()

    def test_stale_or_empty_expected_session_never_reads(self):
        for session in ("expired", ""):
            with self.subTest(session=session), self.assertRaisesRegex(RuntimeError, "连接已结束"):
                self.service.contacts(expected_session=session)
        self.assertEqual(self.backend.calls, 0)

    def test_read_discards_results_after_connection_change(self):
        for action in (lambda: setattr(self.backend, "session_id", "new-session"),
                       self.service.cancel_pending):
            self.backend.session_id = SESSION
            self.backend.during_read = action
            with self.subTest(action=action), self.assertRaisesRegex(RuntimeError, "连接已结束"):
                self.service.contacts()

    def test_foreign_snapshot_session_is_never_returned(self):
        self.backend.snapshot["session_id"] = "other-account-session"
        with self.assertRaisesRegex(RuntimeError, "连接已结束"):
            self.service.contacts()

    def test_plugin_requires_declared_contact_capability(self):
        context = PluginContext("example", {}, self.service, [])
        with self.assertRaisesRegex(CapabilityError, "缺少联系人读取能力"):
            context.contacts()
        context = PluginContext("example", {}, self.service, ["contact.read"])
        self.assertEqual(context.contacts(expected_session=SESSION), directory())


class ContactNativeTests(unittest.TestCase):
    def setUp(self):
        from text_service import NativeTextService
        self.service = NativeTextService(pid=123, account=ACCOUNT)
        self.service.router.set_account(ACCOUNT)
        self.service._friend_session_id = "native-synthetic-session"
        self.service._owns_bridge = True
        self.service._channel = SimpleNamespace(read_memory=Mock(), core_base=0x180000000)
        self.service._check_account = Mock()

    def test_native_read_checks_account_before_and_after_and_adds_metadata(self):
        snapshot = directory()
        with patch("wxbot_framework.contacts.read_contact_directory", create=True, return_value=snapshot) as reader:
            result = self.service.contacts()
        reader.assert_called_once_with(self.service._channel.read_memory,
            self.service._channel.core_base, expected_account=ACCOUNT)
        self.assertEqual(self.service._check_account.call_count, 2)
        self.assertEqual(result["account"], ACCOUNT)
        self.assertEqual(result["session_id"], "native-synthetic-session")
        self.assertIsNotNone(datetime.fromisoformat(result["scanned_at"]).tzinfo)
        self.assertEqual(snapshot, directory())

    def test_native_not_connected_does_not_inspect_memory(self):
        for field, value in (("_owns_bridge", False), ("_channel", None)):
            previous = getattr(self.service, field)
            setattr(self.service, field, value)
            try:
                with patch("wxbot_framework.contacts.read_contact_directory", create=True) as reader:
                    with self.assertRaisesRegex(RuntimeError, "需要已连接"):
                        self.service.contacts()
                    reader.assert_not_called()
            finally:
                setattr(self.service, field, previous)

    def test_native_cache_not_ready_is_a_readable_error(self):
        with patch("wxbot_framework.contacts.read_contact_directory", create=True, return_value=None):
            with self.assertRaisesRegex(RuntimeError, "联系人缓存尚未就绪"):
                self.service.contacts()

    def test_native_changed_account_or_session_discards_snapshot(self):
        for change in (lambda: setattr(self.service.router, "self_wxid", "wxid_different_account"),
                       lambda: setattr(self.service, "_friend_session_id", "changed")):
            self.service.router.self_wxid = ACCOUNT
            self.service._friend_session_id = "native-synthetic-session"
            self.service._check_account.reset_mock()
            self.service._check_account.side_effect = lambda **_: change() if self.service._check_account.call_count == 2 else None
            with patch("wxbot_framework.contacts.read_contact_directory", create=True, return_value=directory()):
                with self.assertRaisesRegex(RuntimeError, "连接已结束"):
                    self.service.contacts()


class ContactPanelTests(PanelFixture):
    def connect(self):
        self.controller.start()
        self.backend = ContactBackend()
        self.service = ServiceFacade(text_delay={"enabled": False})
        self.service.bind(self.backend)
        self.addCleanup(self.service.close)
        self.controller.app.backend = self.backend
        self.controller.app.service = self.service
        for _ in range(100):
            if self.controller.app._connection_ready:
                break
            time.sleep(.005)

    def test_panel_requires_started_and_connected_robot(self):
        with self.assertRaisesRegex(PanelError, "启动机器人"):
            self.controller.contacts()
        self.connect()
        self.controller.app._connection_ready = False
        with self.assertRaisesRegex(PanelError, "尚未连接"):
            self.controller.contacts()

    def test_panel_returns_snapshot_without_contact_logs_or_state(self):
        self.connect()
        self.assertEqual(self.controller.contacts(), directory())
        self.assertNotIn("contacts", self.controller.snapshot())
        self.assertNotIn("合成备注", json.dumps(self.controller.logs.tail(), ensure_ascii=False))

    def test_panel_reports_unavailable_cache_and_missing_capability_as_409(self):
        self.connect()
        self.backend.snapshot = None
        with self.assertRaisesRegex(PanelError, "联系人缓存尚未就绪") as error:
            self.controller.contacts()
        self.assertEqual(error.exception.status, 409)
        self.backend.contacts = None
        with self.assertRaises(PanelError) as error:
            self.controller.contacts()
        self.assertEqual(error.exception.status, 409)

    def test_panel_discards_read_when_robot_stops_or_switches(self):
        self.connect()
        self.backend.during_read = lambda: setattr(self.controller.app, "_connection_ready", False)
        with self.assertRaisesRegex(PanelError, "连接已结束"):
            self.controller.contacts()

    def test_http_is_read_only_authenticated_and_does_not_cache_contacts(self):
        self.connect()
        server = PanelServer(self.controller, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        def request(*, token=True, method="GET"):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
            headers = {"X-Helper-Token": server.token} if token else {}
            body = None
            if method == "POST":
                headers["Content-Type"] = "application/json"
                body = b"{}"
            connection.request(method, "/api/contacts", body=body, headers=headers)
            response = connection.getresponse()
            result = response.status, dict(response.getheaders()), json.loads(response.read())
            connection.close()
            return result

        self.assertEqual(request(token=False)[0], 403)
        status, headers, body = request()
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(body, directory())
        self.assertEqual(request(method="POST")[0], 404)
        self.controller.app._connection_ready = False
        self.assertEqual(request()[0], 409)

    def test_proxy_routes_contacts_only_to_selected_robot(self):
        calls = []
        hub = SimpleNamespace(request=lambda *args: calls.append(args) or directory())
        self.assertEqual(RobotProxy(hub, "profile-second").contacts(), directory())
        self.assertEqual(calls, [("profile-second", "contacts", None)])


if __name__ == "__main__":
    unittest.main()
