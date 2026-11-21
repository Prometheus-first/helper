"""Call-video session controls without WeChat or media devices."""
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock

from wxbot_framework.backend import NativeTextBackend
from wxbot_framework.sdk import CapabilityError, PluginContext, ServiceFacade


class CallVideoSDKTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock(session_id='current')
        self.service = ServiceFacade(text_delay={'enabled': False})
        self.service.bind(self.backend)
        self.addCleanup(self.service.close)
        self.context = PluginContext('video-test', {}, self.service, {'call_video.replace'})

    def test_file_and_program_sources_share_the_session_bound_api(self):
        self.context.start_call_video_file('test.mp4', width=720, height=1280,
            loop=True, quality='high', expected_session='current')
        self.backend.start_call_video_file.assert_called_once_with('test.mp4', {
            'width': 720, 'height': 1280, 'loop': True, 'fps': 15, 'quality': 'high'})
        self.context.stop_call_video(expected_session='current')
        self.context.start_call_video_frames(640, 480, expected_session='current')
        self.context.submit_call_video_frame(b'packed-I420', expected_session='current')
        self.backend.start_call_video_frames.assert_called_once_with(640, 480, 'native')
        self.backend.submit_call_video_frame.assert_called_once_with(b'packed-I420')

    def test_undeclared_plugin_capability_and_stale_session_cannot_enter_backend(self):
        context = PluginContext('no-video', {}, self.service, {'text.send'})
        with self.assertRaises(CapabilityError):
            context.start_call_video_frames(640, 480)
        with self.assertRaises(RuntimeError):
            self.context.submit_call_video_frame(b'frame', expected_session='old')
        self.backend.start_call_video_frames.assert_not_called()
        self.backend.submit_call_video_frame.assert_not_called()

    def test_cancelled_connection_cannot_start_a_decoder(self):
        self.service.cancel_pending()
        with self.assertRaises(RuntimeError):
            self.context.start_call_video_file('test.mp4', width=640, height=480)
        self.backend.start_call_video_file.assert_not_called()

    def test_waiting_frame_is_rejected_after_reconnection(self):
        waiting = threading.Event()
        held = threading.Lock()
        held.acquire()

        class WaitingLock:
            def __enter__(self):
                waiting.set()
                held.acquire()
            def __exit__(self, *args):
                held.release()

        self.service._send_lock = WaitingLock()
        errors = []
        def submit():
            try:
                self.context.submit_call_video_frame(b'frame', expected_session='current')
            except RuntimeError as error:
                errors.append(error)
        worker = threading.Thread(target=submit)
        worker.start()
        self.assertTrue(waiting.wait(2))
        with self.service._lock:
            self.service._backend = Mock(session_id='replacement')
        held.release()
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 1)
        self.backend.submit_call_video_frame.assert_not_called()

    def test_capability_and_backend_route_are_version_scoped(self):
        backend = NativeTextBackend.__new__(NativeTextBackend)
        backend.raw = SimpleNamespace(wechat_version='4.1.15.56', start_call_video_file=Mock())
        self.assertNotIn('call_video.replace', backend.capabilities)
        with self.assertRaises(CapabilityError):
            backend.start_call_video_file('test.mp4', {})
        backend.raw.start_call_video_file.assert_not_called()
        for version in ('4.2.0.11', '4.2.0.12'):
            with self.subTest(version=version):
                backend.raw.wechat_version = version
                self.assertIn('call_video.replace', backend.capabilities)
                backend.start_call_video_file('test.mp4', {})
        self.assertEqual(backend.raw.start_call_video_file.call_count, 2)

    def test_ai_camera_start_uses_the_same_capability_and_session_gate(self):
        self.context.start_call_video_facefusion('source.png', python_executable='python.exe',
            repository='facefusion', width=720, height=1280, expected_session='current')
        args, _ = self.backend.start_call_video_facefusion.call_args
        self.assertEqual(args[0], 'source.png')
        self.assertEqual((args[1]['camera_id'], args[1]['quality']), (0, 'high'))
        self.backend.start_call_video_facefusion.reset_mock()
        with self.assertRaises(RuntimeError):
            self.context.start_call_video_facefusion('source.png', python_executable='python.exe',
                repository='facefusion', width=720, height=1280, expected_session='old')
        self.backend.start_call_video_facefusion.assert_not_called()

    def test_portrait_animation_rejects_old_sessions_and_missing_capability(self):
        options = {'python_executable': 'python.exe', 'repository': 'portrait', 'width': 720, 'height': 1280}
        self.context.start_call_video_liveportrait('source.png', expected_session='current', **options)
        args, _ = self.backend.start_call_video_liveportrait.call_args
        self.assertEqual(args[0], 'source.png')
        self.assertEqual(args[1]['quality'], 'high')
        self.backend.start_call_video_liveportrait.reset_mock()
        with self.assertRaises(RuntimeError):
            self.context.start_call_video_liveportrait('source.png', expected_session='old', **options)
        context = PluginContext('no-video', {}, self.service, set())
        with self.assertRaises(CapabilityError):
            context.start_call_video_liveportrait('source.png', **options)
        self.backend.start_call_video_liveportrait.assert_not_called()

    def test_body_avatar_start_preserves_session_and_runtime_options(self):
        options={'python_executable':'python.exe','repository':'lhmpp',
                 'linux_python_executable':'/opt/avatar/bin/python','width':720,'height':1280}
        self.context.start_call_video_body_avatar('body.png',expected_session='current',tracking_mode='head',**options)
        args,_=self.backend.start_call_video_body_avatar.call_args
        self.assertEqual(args[1]['wsl_distribution'],'Ubuntu')
        self.assertEqual(args[1]['linux_python_executable'],'/opt/avatar/bin/python')
        self.assertEqual(args[1]['tracking_mode'],'head')
        self.backend.start_call_video_body_avatar.reset_mock()
        with self.assertRaises(RuntimeError):
            self.context.start_call_video_body_avatar('body.png',expected_session='old',**options)
        self.backend.start_call_video_body_avatar.assert_not_called()


if __name__ == '__main__':
    unittest.main()
