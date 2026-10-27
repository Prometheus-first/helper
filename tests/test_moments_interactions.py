"""朋友圈互动离线测试：不连接微信，不提交真实互动。"""
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import test_moments_integration as fixture
from test_moments_read_integration import sample
from wxbot_framework.moments_interactions import MomentsInteractions, _validate
from wxbot_framework.panel_common import PanelError
from wxbot_framework.robot_hub import RobotProxy
from wxbot_framework.sdk import ServiceFacade
from wxbot_framework.web_panel import PanelServer


def request(**values):
    return dict({'request_id': 'request_1234567890', 'session_id': 'session', 'sns_id': '12345',
                 'action': 'comment', 'content': '示例评论'}, **values)


def wait_status(service, request_id, expected, timeout=2):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = service.status(request_id)
        if value['status'] == expected:
            return value
        time.sleep(.005)
    raise AssertionError(f'等待 {expected} 超时')


class ValidationTests(unittest.TestCase):
    def test_ids_and_comment_utf8_validation(self):
        self.assertEqual(_validate(request(sns_id='18446744073709551615', content='中' * 341))[0]['sns_id'],
                         '18446744073709551615')
        for values in ({'sns_id': '0'}, {'sns_id': '01'}, {'sns_id': '18446744073709551616'},
                       {'sns_id': 42}, {'content': '中' * 342}, {'content': ' '}, {'content': '\ud800'},
                       {'content': 'a\0b'}, {'action': 'like', 'content': 'x'}, {'action': 'bad'},
                       {'session_id': 'not valid'}, {'extra': 'x'}):
            with self.subTest(values=values), self.assertRaises(PanelError):
                _validate(request(**values))


class InteractionJobsTests(unittest.TestCase):
    def setUp(self):
        self.session = 'session'
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = []
        def deliver(payload):
            self.calls.append(payload.copy())
            self.entered.set()
            self.release.wait(2)
            return {'status': 'completed'}
        self.service = MomentsInteractions(deliver, lambda: {'session_id': self.session},
                                           lambda session, sns: (session, sns) == ('session', '12345'), timeout=.06)

    def tearDown(self):
        self.release.set()
        self.service.close()

    def test_idempotency_and_busy_timeout_do_not_resubmit(self):
        self.service.submit(request())
        self.assertTrue(self.entered.wait(1))
        self.assertEqual(self.service.submit(request())['status'], 'pending')
        with self.assertRaises(PanelError):
            self.service.submit(request(content='另一条'))
        with self.assertRaises(PanelError):
            self.service.submit(request(request_id='request_1234567891'))
        self.assertEqual(wait_status(self.service, 'request_1234567890', 'unknown')['outcome'], '')
        self.assertEqual(self.service.submit(request())['status'], 'unknown')
        self.release.set()
        time.sleep(.03)
        self.assertEqual(self.service.status('request_1234567890')['status'], 'unknown')
        self.assertEqual(len(self.calls), 1)

    def test_session_change_and_unseen_id_fail_before_dispatch(self):
        for data in (request(session_id='old'), request(sns_id='99999')):
            with self.assertRaises(PanelError):
                self.service.submit(data)
        self.assertEqual(self.calls, [])

    def test_explicit_preflight_failure_and_already_liked(self):
        for raw, expected, outcome in (({'status': 'failed', 'phase': 'preflight'}, 'failed', ''),
                                       ({'status': 'already_liked'}, 'completed', 'already_liked')):
            service = MomentsInteractions(lambda _: raw, lambda: {'session_id': 'session'}, lambda *_: True)
            service.submit(request(action='like', content=''))
            result = wait_status(service, 'request_1234567890', expected)
            self.assertEqual(result['outcome'], outcome)
            service.close()
        service = MomentsInteractions(lambda _: {'status': 'already_liked'},
                                      lambda: {'session_id': 'session'}, lambda *_: True)
        service.submit(request())
        wait_status(service, 'request_1234567890', 'unknown')
        service.close()

    def test_unload_blocked_and_exception_keep_account_busy(self):
        for deliver in (lambda _: {'status': 'unknown', 'unload_blocked': True},
                        Mock(side_effect=RuntimeError('private data'))):
            service = MomentsInteractions(deliver, lambda: {'session_id': 'session'}, lambda *_: True)
            service.submit(request())
            result = wait_status(service, 'request_1234567890', 'unknown')
            self.assertNotIn('private data', str(result))
            with self.assertRaises(PanelError):
                service.submit(request(request_id='request_1234567891'))
            service.close()

    def test_session_changed_after_native_dispatch_is_unknown(self):
        self.service.submit(request())
        self.assertTrue(self.entered.wait(1))
        self.session = 'changed'
        self.release.set()
        self.assertEqual(wait_status(self.service, 'request_1234567890', 'unknown')['outcome'], '')
        self.assertEqual(len(self.calls), 1)

    def test_close_during_delivery_keeps_unknown_after_late_ack(self):
        self.service.submit(request())
        self.assertTrue(self.entered.wait(1))
        self.service.close()
        self.assertEqual(self.service.status('request_1234567890')['status'], 'unknown')
        self.release.set()
        time.sleep(.03)
        self.assertEqual(self.service.status('request_1234567890')['status'], 'unknown')
        with self.assertRaises(PanelError):
            self.service.submit(request(request_id='request_1234567891'))
        self.assertEqual(len(self.calls), 1)


class InteractionControllerTests(fixture.MomentsFixture):
    def prepare(self):
        app = self.start()
        app.service.read_moments = lambda *_, **__: sample()
        app.service.moments_capabilities = lambda **_: {'supported': True, 'interaction_supported': True}
        app.service.interact_moments = Mock(return_value={'status': 'completed'})
        return app

    def test_only_previously_read_ids_and_current_session(self):
        app = self.prepare()
        session = app.backend.session_id
        data = request(session_id=session)
        with self.assertRaises(PanelError):
            self.controller.interact_moments(data)
        self.controller.read_moments({'session_id': session})
        self.controller.interact_moments(data)
        wait_status(self.controller.moments_interactions, data['request_id'], 'completed')
        app.service.interact_moments.assert_called_once()
        app.backend.session_id = 'new_session'
        with self.assertRaises(PanelError):
            self.controller.interact_moments(request(request_id='request_1234567891', session_id='new_session'))
        app.service.interact_moments.assert_called_once()


class InteractionHTTPTests(fixture.MomentsFixture):
    def setUp(self):
        super().setUp()
        self.server = PanelServer(self.controller, 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    http = fixture.MomentsHTTPTests.http
    def test_interaction_routes_obey_session_and_token(self):
        app = self.start()
        app.service.read_moments = lambda *_, **__: sample()
        app.service.moments_capabilities = lambda **_: {'supported': True, 'interaction_supported': True}
        app.service.interact_moments = Mock(return_value={'status': 'completed'})
        data = request(session_id=app.backend.session_id)
        try:
            status, _ = self.http('/api/moments-interact', data, token=False)
            self.assertEqual(status, 403)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            pass
        app.service.interact_moments.assert_not_called()
        self.assertEqual(self.http('/api/moments-read', {'session_id': app.backend.session_id})[0], 200)
        self.assertEqual(self.http('/api/moments-interact', data)[0], 200)
        wait_status(self.controller.moments_interactions, data['request_id'], 'completed')
        status, result = self.http('/api/moments-interaction-status?id=' + data['request_id'])
        self.assertEqual((status, result['status']), (200, 'completed'))


class InteractionFacadeTests(unittest.TestCase):
    def test_session_is_checked_and_stale_ack_is_unknown(self):
        service = ServiceFacade()
        backend = SimpleNamespace(session_id='session', interact_moments=Mock(return_value={'status': 'completed'}))
        service.bind(backend)
        self.assertEqual(service.interact_moments({}, expected_session='old')['phase'], 'preflight')
        self.assertEqual(service.interact_moments({'session_id': 'old'}, expected_session='session')['phase'], 'preflight')
        backend.interact_moments.assert_not_called()
        def change(_):
            service._cancel.set()
            return {'status': 'completed', 'unload_blocked': True}
        backend.interact_moments.side_effect = change
        result = service.interact_moments({'session_id': 'session'}, expected_session='session')
        self.assertEqual(result, {'status': 'unknown', 'unload_blocked': True})

    def test_multi_robot_proxy_routes_to_selected_robot(self):
        hub = Mock()
        proxy = RobotProxy(hub, 'robot_one')
        body = request()
        proxy.interact_moments(body)
        hub.request.assert_called_with('robot_one', 'moments-interact', body)
        proxy.moments_interaction_status('request_1234567890')
        hub.request.assert_called_with('robot_one', 'moments-interaction-status?id=request_1234567890', None)


if __name__ == '__main__':
    unittest.main()
