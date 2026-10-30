"""登录尝试隔离和本地接口测试；不启动微信、不记录真实二维码。"""
import base64
import http.client
import io
import json
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock

from PIL import Image

from wxbot_framework.panel_common import PanelError
from wxbot_framework.panel_http import PanelServer
from wxbot_framework.wechat_login import WechatLoginManager, _find_qr

ROOT = Path(__file__).resolve().parents[1]
URL = 'http://weixin.qq.com/x/ABCDEFGHIJKLMN012345'


class FakeTarget:
    def __init__(self, root, pid):
        self.pid, self.is_alive, self.wxid, self.url, self.closed = pid, True, '', URL, False
    def alive(self): return self.is_alive
    def account(self): return self.wxid
    def qr(self): return self.url
    def close(self): self.closed = True


class LoginTests(unittest.TestCase):
    def setUp(self):
        self.now = 10
        self.created = []
        def factory(root, pid):
            target = FakeTarget(root, pid)
            self.created.append(target)
            return target
        self.process = Mock(pid=1234)
        self.process.poll.return_value = None
        self.manager = WechatLoginManager(ROOT, launch=lambda root: self.process,
                                          target_factory=factory, clock=lambda: self.now)
        self.addCleanup(self.manager.close)

    def test_start_and_attach_are_isolated_and_return_valid_png(self):
        first = self.manager.start({})
        second = self.manager.attach({'pid': 5678})
        self.assertNotEqual(first['attempt_id'], second['attempt_id'])
        for row in (first, second):
            result = self.manager.status(row['attempt_id'])
            self.assertEqual((result['status'], result['pid'], result['mime_type']),
                             ('qr_available', row['pid'], 'image/png'))
            raw = base64.b64decode(result['qrcode_base64'], validate=True)
            self.assertEqual(Image.open(io.BytesIO(raw)).format, 'PNG')
            self.assertNotIn(URL, json.dumps(result))
        self.assertEqual(len(self.created), 2)

    def test_login_and_exit_never_return_old_qr(self):
        row = self.manager.attach({'pid': 5678})
        target = self.created[-1]
        target.wxid = 'wxid_example_robot'
        result = self.manager.status(row['attempt_id'])
        self.assertEqual(result['status'], 'logged_in')
        self.assertNotIn('qrcode_base64', result)
        target.is_alive = False
        self.assertEqual(self.manager.status(row['attempt_id'])['status'], 'exited')

    def test_process_exits_before_native_load(self):
        row = self.manager.start({})
        self.process.poll.return_value = 0
        self.assertEqual(self.manager.status(row['attempt_id'])['status'], 'exited')
        self.assertEqual(self.created, [])

    def test_attach_rejects_logged_in_and_duplicate_processes(self):
        row = self.manager.attach({'pid': 5678})
        with self.assertRaises(PanelError): self.manager.attach({'pid': 5678})
        self.manager.cancel({'attempt_id': row['attempt_id']})
        def factory(root, pid):
            target = FakeTarget(root, pid)
            target.wxid = 'wxid_logged_in'
            self.created.append(target)
            return target
        self.manager.target_factory = factory
        with self.assertRaises(PanelError): self.manager.attach({'pid': 5678})
        self.assertTrue(self.created[-1].closed)

    def test_expiration_and_cancel_close_handles_without_terminating_wechat(self):
        row = self.manager.attach({'pid': 5678})
        target = self.created[-1]
        self.now += 301
        with self.assertRaises(PanelError): self.manager.status(row['attempt_id'])
        self.assertTrue(target.closed)
        row = self.manager.start({})
        self.manager.status(row['attempt_id'])
        self.manager.cancel({'attempt_id': row['attempt_id']})
        self.assertTrue(self.created[-1].closed)
        self.process.terminate.assert_not_called()

    def test_candidate_selection_rejects_ambiguous_or_unrelated_urls(self):
        one = URL.encode()
        self.assertEqual(_find_qr([one, one]), URL)
        self.assertIsNone(_find_qr([b'https://other.example/QR', b'weixin://example']))
        with self.assertRaises(PanelError):
            _find_qr([one, b'http://weixin.qq.com/x/DIFFERENT1234567890'])

    def test_http_api_requires_local_token_and_has_no_cache(self):
        manager = self.manager
        class Hub:
            root = ROOT
            def robot(self, identifier): raise AssertionError('must use global route')
            def wechat_login(self): return manager
        server = PanelServer(Hub(), 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(method, path, body=None, token=None):
            conn = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=5)
            headers = {'X-Helper-Token': server.token if token is None else token,
                       'Content-Type': 'application/json'}
            conn.request(method, path, None if body is None else json.dumps(body), headers)
            response = conn.getresponse()
            result = response.status, json.loads(response.read()), response.getheader('Cache-Control')
            conn.close()
            return result
        try:
            self.assertEqual(request('POST', '/api/wechat-login/attach', {'pid': 5678}, 'invalid')[0], 403)
            status, result, _ = request('POST', '/api/wechat-login/attach', {'pid': 5678})
            self.assertEqual(status, 200)
            status, result, cache = request('GET', '/api/wechat-login/status?attempt_id=' + result['attempt_id'])
            self.assertEqual((status, result['status'], cache), (200, 'qr_available', 'no-store'))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


if __name__ == '__main__':
    unittest.main()
