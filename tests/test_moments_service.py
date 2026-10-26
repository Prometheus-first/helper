"""朋友圈长调用与连接取消隔离；不运行微信或发帖。"""
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from wxbot_framework.sdk import ServiceFacade


class MomentsServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = ServiceFacade()
        self.backend = SimpleNamespace(session_id='offline_session',
                                       publish_moments=Mock(return_value={'status': 'published', 'sns_id': '123'}),
                                       moments_capabilities=Mock(return_value={'supported': True}))

    def test_disconnected_or_stale_session_never_dispatches(self):
        result = self.service.publish_moments({}, expected_session='offline_session')
        self.assertEqual(result, {'status': 'failed', 'phase': 'preflight'})
        self.service.bind(self.backend)
        self.assertEqual(self.service.publish_moments({}, expected_session='old'), result)
        self.backend.publish_moments.assert_not_called()

    def test_valid_session_delivers_once_and_returns_native_ack(self):
        self.service.bind(self.backend)
        payload = {'content': 'offline example'}
        self.assertEqual(self.service.publish_moments(payload, expected_session='offline_session'),
                         {'status': 'published', 'sns_id': '123'})
        self.backend.publish_moments.assert_called_once_with(payload)

    def test_capability_check_uses_the_expected_connection(self):
        self.service.bind(self.backend)
        self.assertEqual(self.service.moments_capabilities(expected_session='offline_session'), {'supported': True})
        with self.assertRaises(RuntimeError):
            self.service.moments_capabilities(expected_session='old')
        self.backend.moments_capabilities.assert_called_once()

    def test_disconnect_during_delivery_keeps_native_media_lifetime_flags(self):
        self.service.bind(self.backend)
        def deliver(_):
            self.service._cancel.set()
            return {'status': 'unknown', 'unload_blocked': True, 'retain_images': True}
        self.backend.publish_moments.side_effect = deliver
        self.assertEqual(self.service.publish_moments({}, expected_session='offline_session'),
                         {'status': 'unknown', 'unload_blocked': True, 'retain_images': True})

    def test_slow_native_delivery_does_not_hold_connection_lock(self):
        self.service.bind(self.backend)
        acquired = threading.Event()
        threads = []
        def deliver(_):
            def cancel_connection():
                with self.service._lock:
                    self.service._cancel.set()
                    acquired.set()
            thread = threading.Thread(target=cancel_connection)
            threads.append(thread)
            thread.start()
            self.assertTrue(acquired.wait(1), '取消连接被网络调用阻塞')
            return {'status': 'published', 'sns_id': '123'}
        self.backend.publish_moments.side_effect = deliver
        result = self.service.publish_moments({}, expected_session='offline_session')
        for thread in threads:
            thread.join(1)
        self.assertEqual(result['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
