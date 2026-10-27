"""朋友圈读取路由与连接隔离的离线测试，数据均为模拟记录。"""
import threading
import unittest

from test_moments_integration import MomentsFixture, MomentsHTTPTests
from wxbot_framework.panel_common import PanelError
from wxbot_framework.sdk import ServiceFacade


def sample():
    return {'source': 'loaded_pc_moments_cache', 'loaded_count': 1, 'truncated': False,
            'rows': [{'sns_id': '12345', 'author': 'wxid_sample', 'nickname': '示例',
                      'is_self': False, 'timestamp': 1700000000, 'time': '2023-11-15T06:13:20+08:00',
                      'private': False, 'content': '示例正文', 'content_type': 1,
                      'media_count': 2, 'internal_url': 'must-not-leak'}], 'private_field': 'must-not-leak'}


class ReadControllerTests(MomentsFixture):
    def test_result_is_explicit_session_scoped_and_field_limited(self):
        app = self.start()
        calls = []
        def read(limit, *, expected_session):
            calls.append((limit, expected_session))
            return sample()
        app.service.read_moments = read
        session = app.backend.session_id
        result = self.controller.read_moments({'session_id': session, 'limit': 20})
        self.assertEqual(calls, [(20, session)])
        self.assertEqual(result['rows'][0]['content'], '示例正文')
        self.assertNotIn('internal_url', result['rows'][0])
        self.assertNotIn('private_field', result)
        for bad in ({'session_id': 'old'}, {'session_id': session, 'limit': True},
                    {'session_id': session, 'limit': 51}):
            with self.assertRaises(PanelError):
                self.controller.read_moments(bad)
        self.assertEqual(len(calls), 1)

    def test_connection_switch_discards_old_result_and_only_one_read_runs(self):
        app = self.start()
        entered, release = threading.Event(), threading.Event()
        def read(limit, *, expected_session):
            entered.set()
            release.wait(3)
            return sample()
        app.service.read_moments = read
        session = app.backend.session_id
        errors = []
        def run():
            try:
                self.controller.read_moments({'session_id': session})
            except PanelError as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(PanelError):
                self.controller.read_moments({'session_id': session})
            app.backend.session_id = 'a-new-session'
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].status, 409)


class ReadHTTPTests(MomentsHTTPTests):
    def test_read_requires_local_token_and_returns_data_only_on_post(self):
        app = self.start()
        calls = []
        def read(limit, *, expected_session):
            calls.append(expected_session)
            return sample()
        app.service.read_moments = read
        body = {'session_id': app.backend.session_id}
        # Windows 有时会因拒绝请求时正文未读完而重置连接；两种结果都不能调用读取接口。
        def rejected(**kwargs):
            try:
                return self.http('/api/moments-read', body, **kwargs)[0] == 403
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                return True
        self.assertTrue(rejected(token=False))
        self.assertTrue(rejected(origin='https://attacker.invalid'))
        self.assertEqual(calls, [])
        self.assertEqual(self.http('/api/moments-read')[0], 404)
        status, result = self.http('/api/moments-read', body)
        self.assertEqual(status, 200)
        self.assertEqual(result['rows'][0]['content'], '示例正文')


class ReadCancellationTests(unittest.TestCase):
    def test_close_cancels_inflight_read_without_deadlock(self):
        entered, exited = threading.Event(), threading.Event()
        class Backend:
            session_id = 'session'
            def read_moments(self, limit, *, cancelled):
                entered.set()
                while not cancelled():
                    threading.Event().wait(.002)
                exited.set()
                return sample()
            def stop(self):
                if not exited.wait(2):
                    raise RuntimeError('read did not cancel')
        service = ServiceFacade()
        service.bind(Backend())
        errors = []
        def run():
            try:
                service.read_moments(expected_session='session')
            except RuntimeError as error:
                errors.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(entered.wait(2))
        service.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
