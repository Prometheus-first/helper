"""Exercise the Windows relation scan with synthetic contacts and replies."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from run_selftest import _friend_relation_status
from wxbot_framework.web_panel import PanelController, PanelError


class FriendRelationTests(unittest.TestCase):
    def test_classifies_only_complete_verify_user_replies(self):
        report = dict(operation='friend_relation', complete=True, exception=0,
                      native_success=False, native_code=-44)
        result = dict(code=0, cleanup_code=0, report=report)
        self.assertEqual(_friend_relation_status(result), 'verification_required')
        self.assertEqual(_friend_relation_status(dict(result, report=dict(report, native_success=True,
                                                                          native_code=-1))), 'accepted')
        for override in (dict(native_code=-1), dict(complete=False), dict(exception=1),
                         dict(operation='friend_delete'), dict(native_success=None), dict(native_code=-44.0)):
            self.assertEqual(_friend_relation_status(dict(result, report={**report, **override})), 'unknown')
        for override in (dict(code=1), dict(cleanup_code=1), dict(report=None), dict(report=[report])):
            self.assertEqual(_friend_relation_status({**result, **override}), 'unknown')

    def fixture(self, replies):
        controller = PanelController.__new__(PanelController)
        controller.lock, controller.stop_event = threading.RLock(), threading.Event()
        controller.send_jobs, controller.state = {}, 'running'
        account, session = 'wxid_example_self', 'fixture-session'
        rows = [dict(wxid=f'wxid_example_peer_{index}', nickname=f'Fixture {index}', remark='')
                for index in range(3)]
        service = SimpleNamespace(session_is_current=Mock(return_value=True),
                                  contacts=Mock(return_value=dict(account=account, session_id=session, contacts=rows)),
                                  check_friend_relation=Mock(side_effect=replies))
        backend = SimpleNamespace(capabilities={'friend.relation.check'}, session_id=session)
        controller.app = SimpleNamespace(_connection_ready=True, backend=backend, service=service,
                                         config={'wechat_account': account})

        def run_inline(job, deliver, _name):
            deliver()
            return controller.send_status(job['request_id'])

        controller._start_send_job = run_inline
        data = dict(account=account, session_id=session, request_id='fixture-friend-relation-01',
                    confirm_wechat_effects=True)
        return controller, service, data, rows

    @patch('wxbot_framework.web_panel.time.sleep')
    def test_lists_only_verification_required_and_reuses_the_same_request(self, _sleep):
        controller, service, data, rows = self.fixture(['accepted', 'verification_required', 'unknown'])
        with self.assertRaises(PanelError):
            controller.scan_friend_relations(dict(data, confirm_wechat_effects=False))
        controller.app.backend.capabilities.clear()
        with self.assertRaises(PanelError):
            controller.scan_friend_relations(data)
        service.check_friend_relation.assert_not_called()
        controller.app.backend.capabilities.add('friend.relation.check')
        result = controller.scan_friend_relations(data)
        self.assertEqual(result['status'], 'scanned')
        self.assertEqual(result['scan'], dict(total=3, checked=3, unknown=1, rows=[rows[1]]))
        self.assertEqual(controller.scan_friend_relations(data), result)
        self.assertEqual(service.check_friend_relation.call_count, 3)

    @patch('wxbot_framework.web_panel.time.sleep')
    def test_transport_failure_stops_with_partial_results(self, _sleep):
        controller, service, data, rows = self.fixture(['verification_required', OSError(), 'accepted'])
        result = controller.scan_friend_relations(data)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['scan'], dict(total=3, checked=2, unknown=1, rows=[rows[0]]))
        self.assertEqual(service.check_friend_relation.call_count, 2)
