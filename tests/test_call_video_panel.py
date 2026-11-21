import base64
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import json
import shutil

from PIL import Image
from wxbot_framework.call_video_panel import CallVideoPanel
from wxbot_framework.panel_common import PanelError


def photo(color='blue'):
    buffer = BytesIO()
    Image.new('RGB', (32, 48), color).save(buffer, format='PNG')
    return {'name': '测试.png', 'data_url': 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()}


class CallVideoPanelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'wechat_account': 'wxid_account_a'}
        self.service = Mock()
        self.app = SimpleNamespace(stop_event=threading.Event(), _connection_ready=True, service=self.service,
            backend=SimpleNamespace(capabilities={'call_video.replace'}, session_id='session-a'))
        self.controller = SimpleNamespace(workspace=self.root, lock=threading.RLock(), app=self.app,
            store=SimpleNamespace(load=lambda: dict(self.config)), _call=lambda callback: callback(self.app))
        self.panel = CallVideoPanel(self.controller)
        self.addCleanup(self.panel.close)
        (self.root / 'python.exe').touch()
        (self.root / 'facefusion.py').touch()
        self.settings = {'python_executable': str(self.root / 'python.exe'), 'repository': str(self.root), 'mode': 'balanced'}
        self.service.call_video_statistics.return_value = {'last_tick': 10000, 'input_width': 720, 'input_height': 1280, 'ai': None}

    def start(self):
        identifier = self.panel.upload(photo())['photo_id']
        with patch('wxbot_framework.call_video_panel.threading.Thread') as thread:
            self.panel.start({'photo_id': identifier, 'session_id': 'session-a', 'settings': self.settings})
        return identifier

    def test_photo_normalization_and_account_isolation_survive_new_manager(self):
        identifier = self.panel.upload(photo())['photo_id']
        row = self.panel.snapshot()['photos'][0]
        self.assertEqual(row['id'], identifier)
        path = self.panel._photo_path('wxid_account_a', identifier)
        with Image.open(path) as image:
            self.assertEqual(image.format, 'JPEG')
        self.config['wechat_account'] = 'wxid_account_b'
        self.assertEqual(self.panel.snapshot()['photos'], [])
        with self.assertRaises(PanelError):
            self.panel._photo_path('wxid_account_b', identifier)
        self.config['wechat_account'] = 'wxid_account_a'
        restored = CallVideoPanel(self.controller)
        self.addCleanup(restored.close)
        self.assertEqual(restored.snapshot()['photos'][0]['id'], identifier)

    def test_invalid_upload_and_traversal_never_create_photos(self):
        for data in ({'name':'../x.png', 'data_url':photo()['data_url']},
                     {'name':'x.png','data_url':'data:image/png;base64,bm90IHBob3Rv'},
                     {'name':'x.png','data_url':'https://example.invalid/picture.png'}):
            with self.subTest(data=data), self.assertRaises(PanelError):
                self.panel.upload(data)
        with self.assertRaises(PanelError):
            self.panel._photo_path('wxid_account_a', '../secret')
        self.assertEqual(self.panel.snapshot()['photos'], [])

    def test_reuploading_a_used_photo_returns_its_saved_entry(self):
        original = self.panel.upload(photo())['photo_id']
        self.assertEqual(self.panel.upload(photo())['photo_id'], original)
        self.assertEqual(len(self.panel.snapshot()['photos']), 1)

    def test_start_waits_for_fresh_call_and_never_restarts_after_reconnect(self):
        self.start()
        self.service.call_video_statistics.return_value['last_tick'] = 0
        with patch('wxbot_framework.facefusion_stream.frame_clock_ns', return_value=10_000_000_000):
            self.panel._tick()
        self.service.start_call_video_facefusion.assert_not_called()
        self.app.backend.session_id = 'session-b'
        self.panel._tick()
        self.assertFalse(self.panel.enabled)
        self.service.start_call_video_facefusion.assert_not_called()
        self.service.stop_call_video.assert_not_called()

    def test_fresh_call_starts_with_preview_and_switch_failure_keeps_old_photo(self):
        original = self.start()
        with patch('wxbot_framework.facefusion_stream.frame_clock_ns', return_value=10_000_000_000):
            self.panel._tick()
        self.assertTrue(self.service.start_call_video_facefusion.call_args.kwargs['preview'])
        self.assertEqual(self.service.start_call_video_facefusion.call_args.kwargs['model'], 'hyperswap_1a_256')
        self.assertFalse(self.service.start_call_video_facefusion.call_args.kwargs['enhance'])
        second = self.panel.upload(photo('red'))['photo_id']
        self.service.switch_call_video_photo.return_value = {'request_id': 'request-b'}
        self.panel.switch({'session_id':'session-a', 'photo_id':second})
        self.service.call_video_statistics.return_value['ai'] = {'phase':'streaming',
            'source_request_id':'request-b', 'source_status':'failed', 'source_error':'保留原照片'}
        with patch('wxbot_framework.facefusion_stream.frame_clock_ns', return_value=10_000_000_000):
            self.panel._tick()
        self.assertEqual(self.panel.photo, original)
        self.assertIsNone(self.panel.pending)
        self.assertEqual(self.panel.error, '保留原照片')
        self.service.start_call_video_facefusion.assert_called_once()

    def test_successful_switch_keeps_same_producer_and_active_photo_cannot_be_deleted(self):
        self.start()
        with patch('wxbot_framework.facefusion_stream.frame_clock_ns', return_value=10_000_000_000):
            self.panel._tick()
        second = self.panel.upload(photo('red'))['photo_id']
        self.service.switch_call_video_photo.return_value = {'request_id': 'request-b'}
        self.panel.switch({'session_id':'session-a','photo_id':second})
        self.service.call_video_statistics.return_value['ai'] = {'phase':'streaming',
            'source_request_id':'request-b', 'source_status':'applied'}
        with patch('wxbot_framework.facefusion_stream.frame_clock_ns', return_value=10_000_000_000):
            self.panel._tick()
        self.assertEqual(self.panel.photo, second)
        self.service.start_call_video_facefusion.assert_called_once()
        with self.assertRaises(PanelError):
            self.panel.delete({'photo_id':second})
        self.panel.stop({'session_id':'session-a'})
        self.panel.delete({'photo_id':second})
        self.service.stop_call_video.assert_called_once_with(expected_session='session-a')

    def test_preview_is_discarded_if_account_changes_during_read(self):
        self.start()
        def change(**kwargs):
            self.app.backend.session_id = 'session-b'
            return {'sequence':1,'jpeg':b'PRIVATE FRAME'}
        self.service.call_video_preview.side_effect = change
        self.assertEqual(self.panel.preview(), {'image':None})

    def test_stale_stop_cannot_stop_new_account_and_hangup_stops_producer(self):
        self.start()
        with self.assertRaises(PanelError):
            self.panel.stop({'session_id':'old-session'})
        self.service.stop_call_video.assert_not_called()
        self.panel.phase = 'streaming'
        with patch('wxbot_framework.facefusion_stream.frame_clock_ns', return_value=14_000_000_000):
            self.panel._tick()
        self.assertFalse(self.panel.enabled)
        self.service.stop_call_video.assert_called_once_with(expected_session='session-a')


from tests import test_web_panel as web_fixtures
from wxbot_framework.panel_http import PanelServer


class CallVideoHTTPTests(web_fixtures.PanelFixture):
    request = web_fixtures.HTTPTests.request

    def setUp(self):
        super().setUp()
        self.controller.store.bind_account('wxid_test_robot')
        shutil.copytree(web_fixtures.ROOT / 'webui', self.root / 'webui')
        self.server = PanelServer(self.controller, 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def test_authenticated_upload_gallery_and_delete_routes(self):
        self.assertEqual(self.request('/call-video.js', token=False)[0], 200)
        self.assertEqual(self.request('/api/call-video/upload', photo(), token=False)[0], 403)
        self.assertEqual(self.request('/api/call-video/upload', photo(), origin='https://foreign.invalid')[0], 403)
        result = self.request('/api/call-video/upload', photo())
        self.assertEqual(result[0], 200)
        identifier = json.loads(result[2])['photo_id']
        state = json.loads(self.request('/api/call-video')[2])
        self.assertEqual(state['photos'][0]['id'], identifier)
        self.assertFalse(state['connected'])
        self.assertEqual(self.request('/api/call-video/start', {'photo_id':identifier,'session_id':'stale'})[0], 409)
        self.assertEqual(json.loads(self.request('/api/call-video/preview')[2]), {'image':None})
        self.assertEqual(self.request('/api/call-video/delete', {'photo_id':identifier})[0], 200)
        self.assertEqual(json.loads(self.request('/api/call-video')[2])['photos'], [])

    def test_robot_proxy_forwards_video_routes_to_the_selected_worker(self):
        from wxbot_framework.robot_hub import RobotProxy
        hub = Mock()
        video = RobotProxy(hub, 'robot-b').call_video
        video.upload({'name':'photo'})
        hub.request.assert_called_with('robot-b', 'call-video/upload', {'name':'photo'})
        video.switch({'session_id':'current','photo_id':'id'})
        hub.request.assert_called_with('robot-b', 'call-video/switch', {'session_id':'current','photo_id':'id'})
