"""Long text uses ordered native-sized chunks; no real WeChat calls."""
import unittest
from unittest.mock import Mock

from wxbot_framework.sdk import ServiceFacade


class LongTextTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock(session_id='current')
        self.backend.send_text.return_value = True
        self.backend.send_at_text.return_value = True
        self.service = ServiceFacade(text_delay={'enabled': False})
        self.service.bind(self.backend)

    def test_utf8_boundaries_and_order(self):
        content = 'a' * 1022 + '中文🌸' * 800
        self.assertTrue(self.service.send_text('filehelper', content, expected_session='current'))
        parts = [call.args[1] for call in self.backend.send_text.call_args_list]
        self.assertEqual(''.join(parts), content)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(0 < len(part.encode('utf-8')) <= 1023 for part in parts))

    def test_mentions_only_first_part(self):
        text = '消息🌸' * 600
        self.assertTrue(self.service.send_at_text('123@chatroom', text, ['wxid_member'], display_names=['小明']))
        first = self.backend.send_at_text.call_args.args[1]
        remaining = [call.args[1] for call in self.backend.send_text.call_args_list]
        self.assertEqual(self.backend.send_at_text.call_count, 1)
        self.assertEqual(first + ''.join(remaining), '@小明\u2005\n' + text)

    def test_failure_stops_remaining_parts(self):
        self.backend.send_text.side_effect = [True, False, True]
        self.assertFalse(self.service.send_text('filehelper', 'a' * 3100))
        self.assertEqual(self.backend.send_text.call_count, 2)

    def test_session_end_stops_remaining_parts(self):
        def stop_after_first(*args):
            self.service.cancel_pending()
            return True
        self.backend.send_text.side_effect = stop_after_first
        with self.assertRaises(RuntimeError):
            self.service.send_text('filehelper', 'a' * 3000, expected_session='current')
        self.assertEqual(self.backend.send_text.call_count, 1)


if __name__ == '__main__':
    unittest.main()
