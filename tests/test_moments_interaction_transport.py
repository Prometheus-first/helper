"""Native interaction IPC tests; all process and memory operations are fakes."""
import ctypes as c
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from native import moments_interaction_transport as transport

ACCOUNT = 'wxid_offline_robot'
BRIDGE = Path('native/build_text/text_bridge.dll').resolve()
BASE = 0x100000
EXPORTS = {value: i * 0x100 for i, value in enumerate(transport._NAMES.values(), 1)}


class Channel:
    def __init__(self, case):
        self.case, self.buffers, self.calls, self.closed = case, {}, [], False
        self.config = None

    def verify_client(self):
        pass

    def allocate(self, data):
        pointer = len(self.buffers) + 1
        self.buffers[pointer] = data
        return pointer

    def call(self, address, parameter=None):
        name = next(key for key, value in EXPORTS.items() if BASE + value == address)
        self.calls.append(name)
        if name == transport._NAMES['Start']:
            self.config = transport.Config.from_buffer_copy(self.buffers[parameter])
            if self.case.start_error:
                raise TimeoutError('sensitive diagnostic must not escape')
            return self.case.start_code
        request = transport.Poll.from_buffer_copy(self.buffers[parameter])
        self.case.assertEqual(request.operation_id, self.config.operation_id)
        if name == transport._NAMES['Poll']:
            request.state = self.case.state
            request.attempted = int(self.case.state not in (6, 7, 8, 13))
            request.native_known = request.native_value = int(self.case.state == 11)
            request.cleanup_complete = request.tasks = request.retired = 1
            self.buffers[parameter] = bytes(request)
        return 0

    def read_memory(self, address, size):
        return self.buffers[address][:size]

    def close(self):
        self.closed = True


class InteractionTransportTests(unittest.TestCase):
    def setUp(self):
        self.start_code, self.start_error, self.state = 0, False, 11
        self.channel = Channel(self)
        self.payload = {'sns_id': '123456789012345678', 'action': 'comment', 'content': '离线评论'}
        modules = {'weixin.dll': (0x200000, 'offline-core.dll'), BRIDGE.name.lower(): (BASE, str(BRIDGE))}
        patches = [patch.object(transport, 'interaction_capability', return_value=True),
                   patch.object(transport, '_core_profile', return_value=object()),
                   patch.object(transport, '_native', return_value=SimpleNamespace(modules=lambda _: modules)),
                   patch.object(transport, '_exports', return_value=(EXPORTS, None)),
                   patch.object(transport, '_Channel', return_value=self.channel),
                   patch('wxbot_framework.account.read_current_account', return_value=ACCOUNT),
                   patch.object(transport.threading, 'Thread')]
        self.mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

    def run_call(self, **kwargs):
        return transport.interact_moments(42, ACCOUNT, self.payload, bridge_path=BRIDGE, **kwargs)

    def test_completed_once_and_cleanup_uses_same_operation_token(self):
        self.assertEqual(self.run_call(), {'status': 'completed', 'unload_blocked': False})
        self.assertEqual(self.channel.calls, [transport._NAMES[x] for x in ('Start', 'Poll', 'Close')])
        self.assertTrue(self.channel.closed)
        self.assertGreater(self.channel.config.operation_id, 0)
        self.assertEqual(self.channel.config.content.decode(), '离线评论')
        self.mocks[-1].assert_not_called()

    def test_busy_does_not_close_a_different_pending_task(self):
        self.start_code = 170
        self.assertEqual(self.run_call()['status'], 'failed')
        self.assertEqual(self.channel.calls, [transport._NAMES['Start']])
        self.mocks[-1].assert_not_called()

    def test_already_liked_requires_no_send(self):
        self.payload.update(action='like', content=''); self.state = 13
        self.assertEqual(self.run_call()['status'], 'already_liked')

    def test_preflight_missing_post_is_failed_not_unknown(self):
        self.state = 6
        result = self.run_call()
        self.assertEqual((result['status'], result['phase']), ('failed', 'preflight'))

    def test_native_false_is_unknown_without_retry(self):
        self.state = 12
        self.assertEqual(self.run_call(), {'status': 'unknown', 'unload_blocked': False})
        self.assertEqual(self.channel.calls.count(transport._NAMES['Start']), 1)

    def test_quarantine_never_reports_success_or_calls_close(self):
        self.state = 9
        self.assertEqual(self.run_call(), {'status': 'unknown', 'unload_blocked': True})
        self.assertNotIn(transport._NAMES['Close'], self.channel.calls)
        self.mocks[-1].return_value.start.assert_called_once()

    def test_remote_start_timeout_never_retries_and_keeps_cleanup_owner(self):
        self.start_error = True
        self.assertEqual(self.run_call(), {'status': 'unknown', 'unload_blocked': True})
        self.assertEqual(self.channel.calls, [transport._NAMES['Start']])
        self.mocks[-1].return_value.start.assert_called_once()

    def test_timeout_only_cancels_and_never_reposts(self):
        self.assertEqual(self.run_call(timeout=0)['status'], 'unknown')
        self.assertEqual(self.channel.calls, [transport._NAMES['Start'], transport._NAMES['Cancel']])

    def test_account_change_discards_success(self):
        self.mocks[-2].side_effect = [ACCOUNT, 'wxid_other_fixture']
        self.assertEqual(self.run_call(), {'status': 'unknown', 'unload_blocked': False})

    def test_account_read_failure_after_close_does_not_pin_finished_task(self):
        self.mocks[-2].side_effect = [ACCOUNT, OSError('offline fixture')]
        self.assertEqual(self.run_call(), {'status': 'unknown', 'unload_blocked': False})
        self.mocks[-1].assert_not_called()

    def test_config_validation(self):
        for change in ({'sns_id': 12}, {'sns_id': '0'}, {'sns_id': str(2**64)},
                       {'action': 'unknown'}, {'content': ''}, {'content': ' '},
                       {'content': '喵' * 342}, {'content': '\0'}, {'content': '\ud800'}):
            with self.subTest(change=change), self.assertRaises((ValueError, UnicodeError)):
                transport._config(42, ACCOUNT, self.payload | change)
        a = transport._config(42, ACCOUNT, self.payload)
        b = transport._config(42, ACCOUNT, self.payload)
        self.assertNotEqual(a.operation_id, b.operation_id)

    def test_result_cannot_turn_inconsistent_receipt_into_success(self):
        poll = transport.Poll(); poll.state = 11
        self.assertEqual(transport._result(poll, True)['status'], 'unknown')
        poll.attempted = poll.native_known = poll.native_value = 1
        poll.exception = 1
        self.assertEqual(transport._result(poll, True)['status'], 'unknown')
        self.assertEqual(transport._result(poll, False)['unload_blocked'], True)


if __name__ == '__main__':
    unittest.main()
