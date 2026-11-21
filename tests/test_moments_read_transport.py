"""Account-bound fresh SNS reads through fake IPC only, without a WeChat process."""
import ctypes as c
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from native import moments_read_transport as transport

ACCOUNT = 'wxid_offline_moments_robot'
OTHER_ACCOUNT = 'wxid_offline_moments_other'
BRIDGE = Path('native/build_text/text_bridge.dll').resolve()
BASE = 0x100000
CORE = 0x200000
OPERATION = 0x123456789
EXPORTS = {name: index * 0x100 for index, name in enumerate(
    (transport._START, transport._POLL, transport._CLOSE, transport._WAIT, transport._RETAIN), 1)}


class FakeChannel:
    def __init__(self, case):
        self.case = case
        self.buffers = {}
        self.calls = []
        self.close_ids = []
        self.config = None
        self.pending = self.closed = False
        self.native_result_held = False

    def verify_client(self):
        self.case.events.append('verify')

    def allocate(self, raw):
        pointer = len(self.buffers) + 1
        self.buffers[pointer] = raw
        return pointer

    def read_memory(self, pointer, size):
        return self.buffers[pointer][:size]

    def call(self, address, parameter=None):
        name = next(name for name, offset in EXPORTS.items() if BASE + offset == address)
        self.calls.append(name)
        self.case.events.append(name)
        if name == transport._START:
            self.config = transport.Config.from_buffer_copy(self.buffers[parameter])
            if self.case.start_timeout:
                self.pending = True
                raise TimeoutError('synthetic remote timeout')
            self.native_result_held = self.case.start_code == 0
            return self.case.start_code
        if name == transport._WAIT:
            return 0
        if name == transport._RETAIN:
            self.case.assertTrue(self.native_result_held)
            return self.case.retain_code
        request = transport.Poll.from_buffer_copy(self.buffers[parameter])
        if name == transport._POLL:
            request.state = 2
            request.attempted = 1
            request.begin, request.count = 0x300000, 10
            for field, value in self.case.poll_fields.items():
                setattr(request, field, value)
            self.buffers[parameter] = bytes(request)
            return 0
        self.close_ids.append(request.operation_id)
        if request.operation_id != OPERATION:
            return 1168
        code = self.case.close_codes.pop(0) if self.case.close_codes else self.case.close_code
        if code == 0:
            self.native_result_held = False
        return code

    def system(self, name, parameter):
        self.case.events.append(name)
        if name == b'LoadLibraryW':
            self.case.modules[BRIDGE.name.lower()] = (BASE, str(BRIDGE))
        elif name == b'FreeLibrary':
            self.case.modules.pop(BRIDGE.name.lower(), None)
        return 0

    def close(self):
        self.closed = True
        self.case.events.append('channel_closed')


class MomentsReadTransportTests(unittest.TestCase):
    def setUp(self):
        self.events = []
        self.start_code, self.close_code = 0, 0
        self.retain_code = 0
        self.start_timeout = False
        self.close_codes, self.poll_fields = [], {}
        self.modules = {'weixin.dll': (CORE, 'offline-core.dll'),
                        BRIDGE.name.lower(): (BASE, str(BRIDGE))}
        self.channel = FakeChannel(self)
        self.result = {'source': 'server_moments_page', 'rows': []}
        self.decoder = Mock(side_effect=self.decode)
        patches = {
            'path': patch.object(transport, '_local_path', return_value=BRIDGE),
            'profile': patch.object(transport, '_core_profile',
                                    return_value=SimpleNamespace(version='4.2.0.12')),
            'native': patch.object(transport, '_native',
                                   return_value=SimpleNamespace(modules=lambda _: self.modules)),
            'exports': patch.object(transport, '_exports', return_value=(EXPORTS, None)),
            'channel': patch.object(transport, '_Channel', return_value=self.channel),
            'account': patch.object(transport, 'read_current_account', return_value=ACCOUNT),
            'decode': patch.object(transport, 'decode_moments_vector', self.decoder),
            'thread': patch.object(transport.threading, 'Thread'),
            'sleep': patch.object(transport.time, 'sleep'),
            'token': patch.object(transport.secrets, 'randbits', return_value=OPERATION),
            'cache': patch('wxbot_framework.moments_reader.read_loaded_moments'),
        }
        self.mocks = {name: item.start() for name, item in patches.items()}
        for item in patches.values():
            self.addCleanup(item.stop)

    def decode(self, read_memory, begin, count, account, core, *, limit, cancelled):
        self.assertTrue(self.channel.native_result_held)
        self.assertFalse(self.channel.closed)
        self.assertNotIn(transport._CLOSE, self.channel.calls)
        self.assertEqual((begin, count, account, core, limit), (0x300000, 10, ACCOUNT, CORE, 20))
        self.events.append('decode')
        return self.result

    def run_read(self, **kwargs):
        return transport.read_fresh_moments(42, ACCOUNT, bridge_path=BRIDGE, **kwargs)

    def assert_one_submission(self):
        self.assertEqual(self.channel.calls.count(transport._START), 1)
        self.mocks['cache'].assert_not_called()

    def test_success_decodes_before_close_and_releases_exactly_once(self):
        self.assertIs(self.run_read(), self.result)
        self.assertEqual(self.channel.calls, [transport._START, transport._POLL, transport._RETAIN, transport._CLOSE])
        self.assertEqual(self.channel.close_ids, [OPERATION])
        self.assertTrue(self.channel.closed)
        self.assertFalse(self.channel.native_result_held)
        self.assertLess(self.events.index('decode'), self.events.index(transport._CLOSE))
        self.assertEqual(self.channel.config.account.decode('ascii'), ACCOUNT)
        self.assertEqual(self.channel.config.operation_id, OPERATION)
        self.mocks['thread'].assert_not_called()
        self.assert_one_submission()

    def test_retention_failure_closes_read_and_never_returns_unowned_posts(self):
        self.retain_code = 13
        with self.assertRaisesRegex(transport.MomentsReadError, '引用保留失败'):
            self.run_read()
        self.assertEqual(self.channel.calls.count(transport._CLOSE), 1)
        self.assertFalse(self.channel.native_result_held)

    def test_standalone_read_does_not_leave_cached_refs_before_unload(self):
        self.modules.pop(BRIDGE.name.lower())
        self.run_read(keep_loaded=False)
        self.assertNotIn(transport._RETAIN, self.channel.calls)
        self.assertNotIn(BRIDGE.name.lower(), self.modules)

    def test_old_bridge_without_retention_is_rejected_before_submission(self):
        exports = dict(EXPORTS)
        exports.pop(transport._RETAIN)
        self.mocks['exports'].return_value = (exports, None)
        with self.assertRaisesRegex(transport.MomentsReadError, '对象复用'):
            self.run_read()
        self.assertEqual(self.channel.calls, [])

    def test_native_failure_never_decodes_retries_or_reads_old_cache(self):
        cases = ({'state': 3}, {'state': 4}, {'state': 5}, {'attempted': 0},
                 {'error_type': 1}, {'native_code': -44}, {'exception': 1})
        for fields in cases:
            with self.subTest(fields=fields):
                self.poll_fields = fields
                self.channel = FakeChannel(self)
                self.mocks['channel'].return_value = self.channel
                self.decoder.reset_mock()
                with self.assertRaises(transport.MomentsReadError):
                    self.run_read()
                self.decoder.assert_not_called()
                self.assert_one_submission()
                self.assertEqual(self.channel.calls.count(transport._CLOSE), 1)
        self.mocks['thread'].assert_not_called()

    def test_mismatched_operation_id_is_rejected_and_original_id_closed(self):
        self.poll_fields = {'operation_id': OPERATION + 1}
        with self.assertRaisesRegex(transport.MomentsReadError, '身份不匹配'):
            self.run_read()
        self.decoder.assert_not_called()
        self.assert_one_submission()
        self.assertEqual(self.channel.close_ids, [OPERATION])
        self.mocks['thread'].assert_not_called()

    def test_mismatched_poll_size_is_rejected(self):
        self.poll_fields = {'size': c.sizeof(transport.Poll) - 1}
        with self.assertRaisesRegex(transport.MomentsReadError, '身份不匹配'):
            self.run_read()
        self.decoder.assert_not_called()
        self.assert_one_submission()

    def test_preflight_busy_does_not_close_or_reap_somebody_elses_operation(self):
        self.start_code = 170
        with self.assertRaises(transport.MomentsReadError):
            self.run_read()
        self.assertEqual(self.channel.calls, [transport._START])
        self.decoder.assert_not_called()
        self.mocks['thread'].assert_not_called()

    def test_cancel_after_submission_closes_without_decoding(self):
        cancelled = Mock(side_effect=[False, False, True])
        with self.assertRaises(transport.MomentsReadError):
            self.run_read(cancelled=cancelled)
        self.assertEqual(self.channel.calls, [transport._START, transport._CLOSE])
        self.decoder.assert_not_called()
        self.assert_one_submission()
        self.mocks['thread'].assert_not_called()

    def test_account_change_after_decode_discards_result_and_closes(self):
        self.mocks['account'].side_effect = [ACCOUNT, ACCOUNT, ACCOUNT, OTHER_ACCOUNT]
        with self.assertRaisesRegex(transport.MomentsReadError, '丢弃'):
            self.run_read()
        self.decoder.assert_called_once()
        self.assertEqual(self.channel.calls.count(transport._CLOSE), 1)
        self.assert_one_submission()

    def test_timeout_closes_original_operation_without_resubmission(self):
        with patch.object(transport.time, 'monotonic', side_effect=[10.0, 12.0]):
            with self.assertRaisesRegex(transport.MomentsReadError, '超时'):
                self.run_read(timeout=1)
        self.assertEqual(self.channel.calls, [transport._START, transport._CLOSE])
        self.decoder.assert_not_called()
        self.assert_one_submission()
        self.mocks['thread'].assert_not_called()

    def test_busy_close_retains_original_cleanup_owner(self):
        self.close_code = 170
        self.assertIs(self.run_read(), self.result)
        self.assert_one_submission()
        self.assertEqual(self.channel.calls.count(transport._CLOSE), 20)
        self.assertEqual(set(self.channel.close_ids), {OPERATION})
        self.assertEqual(self.mocks['thread'].call_args.kwargs['args'], (42, BRIDGE, OPERATION, False))
        self.mocks['thread'].return_value.start.assert_called_once()

    def test_remote_start_timeout_reaps_without_another_start_or_close_on_pending_channel(self):
        self.start_timeout = True
        with self.assertRaisesRegex(transport.MomentsReadError, '无法确认'):
            self.run_read()
        self.assertEqual(self.channel.calls, [transport._START])
        self.decoder.assert_not_called()
        self.assertEqual(self.mocks['thread'].call_args.kwargs['args'], (42, BRIDGE, OPERATION, False))

    def test_old_bridge_is_rejected_before_opening_channel_or_injecting(self):
        self.mocks['exports'].return_value = ({transport._WAIT: EXPORTS[transport._WAIT]}, None)
        with self.assertRaisesRegex(transport.MomentsReadError, '更新朋友圈桥接'):
            self.run_read()
        self.mocks['channel'].assert_not_called()
        self.assertEqual(self.events, [])

    def test_older_wechat_is_rejected_before_channel_creation(self):
        self.mocks['profile'].return_value = SimpleNamespace(version='4.2.0.11')
        with self.assertRaisesRegex(transport.MomentsReadError, '4.2.0.12'):
            self.run_read()
        self.mocks['channel'].assert_not_called()

    def test_standalone_read_unloads_only_after_result_and_close(self):
        self.modules.pop(BRIDGE.name.lower())
        self.assertIs(self.run_read(keep_loaded=False), self.result)
        self.assertLess(self.events.index(transport._CLOSE), self.events.index(b'FreeLibrary'))
        self.assertIn(transport._WAIT, self.channel.calls)
        self.assert_one_submission()

    def test_reaper_only_polls_and_closes_original_operation(self):
        self.poll_fields = {'state': 1}
        self.close_codes = [170, 0]
        transport._reap(42, BRIDGE, OPERATION, False)
        self.assertEqual(self.channel.calls, [transport._POLL, transport._CLOSE,
                                              transport._POLL, transport._CLOSE])
        self.assertEqual(self.channel.close_ids, [OPERATION, OPERATION])
        self.assertTrue(self.channel.closed)
        self.decoder.assert_not_called()

    def test_owned_reaper_waits_before_unloading(self):
        transport._reap(42, BRIDGE, OPERATION, True)
        self.assertEqual(self.channel.calls, [transport._POLL, transport._CLOSE, transport._WAIT])
        self.assertEqual(self.events[-2:], [b'FreeLibrary', 'channel_closed'])
        self.assertNotIn(transport._START, self.channel.calls)

    def test_decode_failure_still_releases_result_once(self):
        self.decoder.side_effect = transport.MomentsReadError('synthetic invalid vector')
        with self.assertRaises(transport.MomentsReadError):
            self.run_read()
        self.assertEqual(self.channel.calls, [transport._START, transport._POLL, transport._CLOSE])
        self.assertFalse(self.channel.native_result_held)
        self.assert_one_submission()

    def test_quarantined_result_keeps_owned_bridge_for_cleanup(self):
        self.modules.pop(BRIDGE.name.lower())
        self.poll_fields = {'state': 4}
        self.close_code = 170
        with self.assertRaises(transport.MomentsReadError):
            self.run_read(keep_loaded=False)
        self.decoder.assert_not_called()
        self.assertNotIn(b'FreeLibrary', self.events)
        self.assertEqual(self.mocks['thread'].call_args.kwargs['args'], (42, BRIDGE, OPERATION, True))
        self.assert_one_submission()

    def test_reaper_quarantine_keeps_native_bridge_loaded(self):
        self.poll_fields = {'state': 4}
        transport._reap(42, BRIDGE, OPERATION, True)
        self.assertEqual(self.channel.calls, [transport._POLL])
        self.assertNotIn(b'FreeLibrary', self.events)
        self.assertTrue(self.channel.closed)

    def test_reaper_rejects_mismatched_identity_before_closing_or_unloading(self):
        for fields in ({'operation_id': OPERATION + 1}, {'size': c.sizeof(transport.Poll) - 1}):
            with self.subTest(fields=fields):
                self.poll_fields = fields
                self.channel = FakeChannel(self)
                self.mocks['channel'].return_value = self.channel
                self.events.clear()
                transport._reap(42, BRIDGE, OPERATION, True)
                self.assertEqual(self.channel.calls, [transport._POLL])
                self.assertNotIn(b'FreeLibrary', self.events)
                self.assertTrue(self.channel.closed)
