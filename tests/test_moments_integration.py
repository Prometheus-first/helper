"""朋友圈控制台及 HTTP 的离线集成，只启动 FakeApp 和临时测试 HTTP 监听。"""
import base64
import http.client
from io import BytesIO
import json
from pathlib import Path
import random
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs

from PIL import Image

from test_moments import image_row, request
from test_robot_hub import ACCOUNTS, FakeWorker
from test_web_panel import FakeApp, PanelFixture
from wxbot_framework.panel_common import PanelError
from wxbot_framework.robot_hub import RobotHub, RobotProxy
from wxbot_framework.web_panel import PanelServer


class FakeMomentsService:
    def __init__(self, app):
        self.app = app
        self.supported = True
        self.calls, self.capability_calls = [], []
        self.delivery = None

    def moments_capabilities(self, *, expected_session):
        self.capability_calls.append(expected_session)
        return {'supported': self.supported, 'reason': '' if self.supported else '测试后端未开放发布',
                'private_path': 'C:\\private\\native.bin'}

    def publish_moments(self, payload, *, expected_session):
        self.calls.append((payload, expected_session))
        if self.delivery is not None:
            return self.delivery(payload)
        return {'status': 'published', 'sns_id': '99887766'}


class MomentsApp(FakeApp):
    def __init__(self, root, store):
        super().__init__(root, store)
        self.backend.session_id = 'offline_' + self.config['wechat_account']
        self.backend.raw = SimpleNamespace(self_wxid=self.config['wechat_account'])
        self.service = FakeMomentsService(self)


class MomentsWorker(FakeWorker):
    def __init__(self, root, workspace):
        super().__init__(root, workspace)
        self.controller.factory = MomentsApp

    def request(self, route, data=None):
        if route == 'moments-connection':
            return self.controller.moments_connection()
        if route == 'moments-publish':
            return self.controller.publish_moments(data)
        if route.startswith('moments-status?'):
            return self.controller.moments_status(parse_qs(route.split('?', 1)[1])['id'][0])
        return super().request(route, data)


class MomentsFixture(PanelFixture):
    def setUp(self):
        super().setUp()
        self.controller.factory = MomentsApp

    def wait(self, predicate):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.005)
        self.fail('离线任务超时')

    def start(self):
        self.controller.start()
        self.wait(lambda: self.controller.app._connection_ready)
        return self.controller.app

    def body(self, **values):
        return request(session_id=self.controller.app.backend.session_id, **values)

    def final(self, identifier='request_moments_0001'):
        return self.wait(lambda: (row if (row := self.controller.moments_status(identifier))['status'] != 'pending' else None))


class MomentsControllerTests(MomentsFixture):
    def test_stopped_routes_do_not_start_robot_or_claim_support(self):
        self.controller.factory = Mock(side_effect=AssertionError('禁止自动启动机器人'))
        for operation in (self.controller.moments_connection,
                          lambda: self.controller.publish_moments(request())):
            with self.assertRaises(PanelError) as caught:
                operation()
            self.assertEqual(caught.exception.status, 409)
        self.controller.factory.assert_not_called()
        self.assertIsNone(self.controller.app)

    def test_capabilities_are_real_session_scoped_and_do_not_expose_extra_native_fields(self):
        app = self.start()
        result = self.controller.moments_connection()
        self.assertEqual(result, {'session_id': app.backend.session_id, 'account': 'wxid_test_robot',
                                 'supported': True, 'reason': '', 'images': True, 'max_images': 9,
                                 'visibility': ['public', 'private']})
        self.assertEqual(app.service.capability_calls, [app.backend.session_id])
        self.assertNotIn('private_path', json.dumps(result))
        self.assertNotIn('native.bin', json.dumps(result))

    def test_missing_or_unverified_capability_blocks_publish_without_creating_job(self):
        app = self.start()
        for capability in (None, lambda **_: {'supported': False}, lambda **_: {'supported': 'yes'},
                           lambda **_: {'supported': True, 'reason': 'verified'}):
            app.service.moments_capabilities = capability
            if capability is not None and capability()['supported'] is True:
                app.service.publish_moments = None
            result = self.controller.moments_connection()
            self.assertFalse(result['supported'])
            self.assertTrue(result['reason'])
            with self.assertRaises(PanelError) as caught:
                self.controller.publish_moments(self.body())
            self.assertEqual(caught.exception.status, 409)
        self.assertEqual(app.service.calls, [])
        with self.assertRaises(PanelError) as caught:
            self.controller.moments_status('request_moments_0001')
        self.assertEqual(caught.exception.status, 404)

    def test_capability_exception_does_not_leak_diagnostics(self):
        app = self.start()
        app.service.moments_capabilities = Mock(side_effect=RuntimeError('C:\\private\\sk-secret-key'))
        result = self.controller.moments_connection()
        self.assertFalse(result['supported'])
        self.assertNotIn('private', result['reason'])
        self.assertNotIn('sk-secret', json.dumps(result))

    def test_disconnected_stopping_actual_account_mismatch_and_same_session_switch_are_rejected(self):
        app = self.start()
        self.controller.moments_connection()
        app._connection_ready = False
        with self.assertRaises(PanelError):
            self.controller.moments_connection()
        app._connection_ready = True
        self.controller.state = 'stopping'
        with self.assertRaises(PanelError):
            self.controller.moments_connection()
        self.controller.state = 'running'
        app.backend.raw.self_wxid = 'wxid_other_robot'
        with self.assertRaises(PanelError):
            self.controller.moments_connection()
        app.config['wechat_account'] = 'wxid_other_robot'
        with self.assertRaises(PanelError):
            self.controller.moments_connection()
        self.assertEqual(app.service.calls, [])

    def test_session_switch_during_capability_query_is_rejected(self):
        app = self.start()
        def capability(**_):
            app.backend.session_id = 'changed_session'
            return {'supported': True}
        app.service.moments_capabilities = capability
        with self.assertRaises(PanelError) as caught:
            self.controller.moments_connection()
        self.assertEqual(caught.exception.status, 409)

    def test_publish_delegates_to_job_module_and_service_with_expected_session(self):
        app = self.start()
        data = self.body(images=[image_row()], visibility='private')
        self.controller.publish_moments(data)
        self.assertEqual(self.final()['status'], 'published')
        self.assertEqual(len(app.service.calls), 1)
        payload, expected = app.service.calls[0]
        self.assertEqual(expected, data['session_id'])
        self.assertEqual(payload['session_id'], data['session_id'])
        self.assertEqual(payload['content'], data['content'])
        self.assertEqual(payload['visibility'], 'private')
        self.assertEqual(len(payload['images']), 1)
        self.assertFalse(Path(payload['images'][0]).exists())
        app._connection_ready = False
        self.assertEqual(self.controller.publish_moments(data), self.final())
        self.assertEqual(len(app.service.calls), 1)

    def test_capability_disabled_during_staging_is_failed_before_native_submission(self):
        app = self.start()
        original = self.controller.moments._stage
        def stage(images):
            result = original(images)
            app.service.supported = False
            return result
        with patch.object(self.controller.moments, '_stage', side_effect=stage):
            self.controller.publish_moments(self.body(images=[image_row()]))
            self.assertEqual(self.final()['status'], 'failed')
        self.assertEqual(app.service.calls, [])
        self.assertEqual(list((self.root / '.runtime/moments').iterdir()), [])

    def test_account_switch_after_native_submission_reports_unknown(self):
        app = self.start()
        def delivery(payload):
            app.config['wechat_account'] = 'wxid_other_robot'
            app.backend.raw.self_wxid = 'wxid_other_robot'
            return {'status': 'published', 'sns_id': '12345'}
        app.service.delivery = delivery
        self.controller.publish_moments(self.body())
        self.assertEqual(self.final()['status'], 'unknown')
        self.assertEqual(len(app.service.calls), 1)

    def test_timeout_wrapper_waits_for_actual_service_exit_before_image_cleanup(self):
        app = self.start()
        entered, release = threading.Event(), threading.Event()
        def delivery(payload):
            entered.set()
            release.wait(4)
            return {'status': 'published', 'sns_id': '12345'}
        app.service.delivery = delivery
        self.controller.moments._timeout = .1
        try:
            self.controller.publish_moments(self.body(images=[image_row()]))
            self.assertTrue(entered.wait(3))
            path = Path(app.service.calls[0][0]['images'][0])
            self.assertEqual(self.final()['status'], 'unknown')
            self.assertTrue(path.exists())
        finally:
            release.set()
        self.wait(lambda: not path.exists())
        self.assertEqual(self.final()['status'], 'unknown')
        self.assertEqual(len(app.service.calls), 1)

    def test_controller_close_closes_publisher(self):
        with patch.object(self.controller.moments, 'close', wraps=self.controller.moments.close) as close:
            self.controller.close()
        close.assert_called_once_with()


class MomentsHTTPTests(MomentsFixture):
    def setUp(self):
        super().setUp()
        self.server = PanelServer(self.controller, 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def http(self, path, body=None, *, token=True, origin=None, host=None, declared_size=None, method=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=4)
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Helper-Token'] = self.server.token
        if origin is not None:
            headers['Origin'] = origin
        if host is not None:
            headers['Host'] = host
        if declared_size is not None:
            headers['Content-Length'] = str(declared_size)
        raw = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode('utf-8')
        connection.request(method or ('POST' if raw is not None else 'GET'), path, body=raw, headers=headers)
        response = connection.getresponse()
        raw_result = response.read()
        connection.close()
        return response.status, json.loads(raw_result)

    def test_moments_routes_require_token_same_origin_and_local_host(self):
        app = self.start()
        data = self.body()
        for path, body in (('/api/moments-connection', None), ('/api/moments-publish', data),
                           ('/api/moments-status?id=' + data['request_id'], None)):
            with self.subTest(path=path):
                self.assertEqual(self.http(path, body, token=False)[0], 403)
                self.assertEqual(self.http(path, body, origin='https://attacker.invalid')[0], 403)
                self.assertEqual(self.http(path, body, host=f'attacker.invalid:{self.port}')[0], 403)
        self.assertEqual(app.service.calls, [])
        self.assertEqual(app.service.capability_calls, [])

    def test_http_publish_status_and_single_account_consecutive_click_limit(self):
        app = self.start()
        entered, release = threading.Event(), threading.Event()
        def delivery(payload):
            entered.set()
            release.wait(4)
            return {'status': 'published', 'sns_id': '222333'}
        app.service.delivery = delivery
        status, connection = self.http('/api/moments-connection')
        self.assertEqual(status, 200)
        data = request(session_id=connection['session_id'])
        try:
            self.assertEqual(self.http('/api/moments-publish', data)[0], 200)
            self.assertTrue(entered.wait(3))
            second = dict(data, request_id='request_moments_0002')
            self.assertEqual(self.http('/api/moments-publish', second)[0], 409)
            self.assertEqual(self.http('/api/moments-publish', data)[1]['status'], 'pending')
            self.assertEqual(self.http('/api/moments-status?id=' + data['request_id'])[1]['status'], 'pending')
        finally:
            release.set()
        self.assertEqual(self.final()['status'], 'published')
        self.assertEqual(self.http('/api/moments-status?id=' + data['request_id'])[1]['status'], 'published')
        self.assertEqual(len(app.service.calls), 1)

    def test_only_publish_body_limit_is_raised_and_oversized_header_is_rejected(self):
        self.start()
        noise = random.Random(91).randbytes(128 * 192 * 3)
        output = BytesIO()
        Image.frombytes('RGB', (128, 192), noise).save(output, format='PNG')
        row = {'name': 'noise.png', 'data_url': 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode()}
        data = self.body(images=[row])
        raw = json.dumps(data).encode()
        self.assertGreater(len(raw), 65536)
        self.assertEqual(self.http('/api/moments-publish', raw)[0], 200)
        self.assertEqual(self.final()['status'], 'published')
        self.assertEqual(self.http('/api/send-text', b' ' * 65537)[0], 413)
        self.assertEqual(self.http('/api/moments-publish-extra', b' ' * 65537)[0], 413)
        self.assertEqual(self.http('/api/moments-publish', b'{}', declared_size=29 * 1024 * 1024 + 65537)[0], 413)

    def test_http_unsupported_and_stopped_never_start_robot(self):
        self.assertEqual(self.http('/api/moments-connection')[0], 409)
        self.assertEqual(self.http('/api/moments-publish', request())[0], 409)
        self.assertIsNone(self.controller.app)
        app = self.start()
        app.service.supported = False
        self.assertFalse(self.http('/api/moments-connection')[1]['supported'])
        self.assertEqual(self.http('/api/moments-publish', self.body())[0], 409)
        self.assertEqual(app.service.calls, [])

    def test_robot_proxy_routes_and_http_account_scoping(self):
        calls = []
        proxy = RobotProxy(SimpleNamespace(request=lambda *args: calls.append(args) or {}), 'selected_robot')
        proxy.moments_connection()
        proxy.publish_moments(request())
        proxy.moments_status('test/id?')
        self.assertEqual(calls[0], ('selected_robot', 'moments-connection', None))
        self.assertEqual(calls[1][0:2], ('selected_robot', 'moments-publish'))
        self.assertEqual(calls[2], ('selected_robot', 'moments-status?id=test%2Fid%3F', None))
        hub = RobotHub(self.root, worker_factory=MomentsWorker, scan=lambda _: ACCOUNTS)
        self.addCleanup(hub.close)
        first = hub.robot('default')
        first.bind_account(ACCOUNTS[0])
        second_id = hub.create_robot({'name': '第二个离线机器人'})['id']
        second = hub.robot(second_id)
        second.bind_account(ACCOUNTS[1])
        self.server.controller = hub
        before = set(hub.workers)
        self.assertEqual(self.http('/api/moments-publish?robot=' + second_id, request())[0], 409)
        self.assertEqual(set(hub.workers), before)
        self.assertIsNone(hub.workers[second_id].controller.app)
        first.start()
        second.start()
        self.wait(lambda: all(worker.controller.app._connection_ready for worker in hub.workers.values()))
        account1 = self.http('/api/moments-connection?robot=default')[1]
        account2 = self.http('/api/moments-connection?robot=' + second_id)[1]
        self.assertEqual(account1['account'], ACCOUNTS[0]['wxid'])
        self.assertEqual(account2['account'], ACCOUNTS[1]['wxid'])
        data = request(session_id=account2['session_id'])
        self.assertEqual(self.http('/api/moments-publish?robot=' + second_id, data)[0], 200)
        self.wait(lambda: hub.workers[second_id].controller.moments_status(data['request_id'])['status'] != 'pending')
        self.assertEqual(self.http('/api/moments-status?robot=default&id=' + data['request_id'])[0], 404)
        self.assertEqual(len(hub.workers[second_id].controller.app.service.calls), 1)
        self.assertEqual(hub.workers['default'].controller.app.service.calls, [])
        self.assertEqual(self.http('/api/moments-connection?robot=unknown')[0], 404)

    def test_moments_static_asset_is_explicitly_whitelisted(self):
        directory = self.root / 'webui'
        directory.mkdir(exist_ok=True)
        (directory / 'moments.js').write_text('window.offlineMoments = true;', encoding='utf-8')
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=4)
        connection.request('GET', '/moments.js')
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.read(), b'window.offlineMoments = true;')
        connection.close()


if __name__ == '__main__':
    unittest.main()
