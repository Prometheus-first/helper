"""Automatic-comment configuration crosses the authenticated panel/worker routes."""
from unittest.mock import Mock, patch

from test_moments_integration import MomentsHTTPTests
from wxbot_framework.robot_hub import RobotProxy


class AutoHTTPTests(MomentsHTTPTests):
    def test_configuration_is_authenticated_session_bound_and_independent_of_publish(self):
        self.assertEqual(self.http('/api/moments-auto-comment')[1]['enabled'], False)
        app = self.start()
        app.service.moments_capabilities = Mock(return_value={
            'supported': True, 'interaction_supported': True,
            'like_supported': False, 'comment_supported': True})
        app.service.interact_moments = Mock()
        body = {'session_id': app.backend.session_id, 'enabled': True, 'content': '离线固定评论',
                'interval_seconds': 120, 'authors': []}
        with patch.object(self.controller.moments_auto, 'start'):
            self.assertEqual(self.http('/api/moments-auto-comment', body, token=False)[0], 403)
            self.assertEqual(self.http('/api/moments-auto-comment', body, origin='https://invalid.example')[0], 403)
            self.assertFalse(self.controller.moments_auto.path.exists())
            self.assertEqual(self.http('/api/moments-auto-comment', body | {'session_id': 'old'})[0], 409)
            status, saved = self.http('/api/moments-auto-comment', body)
        self.assertEqual(status, 200)
        self.assertTrue(saved['enabled'])
        self.assertEqual(saved['content'], body['content'])
        self.assertNotIn('account', saved)
        self.assertEqual(self.http('/api/moments-auto-comment')[1]['content'], body['content'])
        connection = self.controller.moments_connection()
        self.assertTrue(connection['comment_supported'])
        self.assertFalse(connection['like_supported'])
        app.service.interact_moments.assert_not_called()
        self.assertEqual(self.http('/api/moments-auto-comment', body | {'enabled': False})[0], 200)

    def test_proxy_routes_auto_comment_to_selected_robot(self):
        worker = Mock()
        proxy = RobotProxy(worker, 'selected_robot')
        proxy.moments_auto_status()
        worker.request.assert_called_with('selected_robot', 'moments-auto-comment', None)
        proxy.configure_moments_auto({'enabled': False})
        worker.request.assert_called_with('selected_robot', 'moments-auto-comment', {'enabled': False})
