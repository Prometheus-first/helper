"""版本锁定 IPC 的离线协议与生命周期测试，所有进程调用均为 Mock。"""
import base64
from contextlib import nullcontext
import ctypes as c
import json
from pathlib import Path
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

from native import miniprogram_transport as transport


APP = 'wx0123456789abcdef'
ACCOUNT = 'wxid_offline_robot'
SESSION = 'helper_mp_' + 'a' * 32
CALL = 'mp_' + 'b' * 32
REQUESTS = [{'method': 'Input.insertText', 'params': {'text': '中文😀'}}]
BASE = 0x100000
ADDRESS = 0x500000
EXPORTS = {b'MpCommand': 0x100, b'MpStopCommand': 0x200, b'MpCanUnload': 0x300}
BRIDGE = Path(__file__).resolve()


def short_field(key, raw):
    """只构造本测试固定的一字节长度 fixture，不复用生产 protobuf 编码器。"""
    if isinstance(raw, str):
        raw = raw.encode('utf-8')
    assert 0 <= len(raw) < 128
    return bytes([key, len(raw)]) + raw


def receipt(*, session=SESSION, call_id=CALL, outer_call=CALL, results=None, error=0, prefix=b''):
    if results is None:
        results = [{'code': 0, 'data': {'data': {'text': '中文😀'}}}]
    wire = prefix + short_field(0x12, session) + short_field(0x1a, call_id) + bytes([0x20, error])
    for result in results:
        wire += short_field(0x2a, json.dumps(result, ensure_ascii=False, separators=(',', ':')))
    return json.dumps({'call_id': outer_call, 'resp_args': base64.b64encode(wire).decode('ascii')}).encode()


def wrapped_wire(wire):
    return json.dumps({'call_id': CALL, 'resp_args': base64.b64encode(wire).decode('ascii')}).encode()


class ProtocolTests(unittest.TestCase):
    def test_request_matches_independent_utf8_protobuf_fixture(self):
        payload = json.loads(transport.encode_request(SESSION, CALL, REQUESTS))
        command = b'{"method":"Input.insertText","params":{"text":"' + '中文😀'.encode() + b'"}}'
        expected = (b'\x0a\x2a' + SESSION.encode() + b'\x12\x23' + CALL.encode() +
                    b'\x1a' + bytes([len(command)]) + command + b'\x28\xb0\xea\x01')
        self.assertEqual(payload['call_id'], CALL)
        self.assertEqual(base64.b64decode(payload['req_args'], validate=True), expected)

    def test_response_decodes_utf8_and_never_decodes_sensitive_native_header(self):
        # 原生 field 1 可能含凭据甚至非 UTF-8；未知字段也不能成为公开返回值。
        private = short_field(0x0a, b'authorization-secret\xff\x00')
        private += short_field(0x3a, b'cookie-secret\xff')
        private += b'\x41' + b'12345678' + b'\x4d' + b'1234'
        self.assertEqual(transport.decode_response(receipt(prefix=private), SESSION, CALL, 1),
                         [{'text': '中文😀'}])

    def test_call_session_and_duplicate_correlation_fields_are_rejected(self):
        fixtures = [receipt(outer_call='mp_' + 'c' * 32), receipt(call_id='mp_' + 'c' * 32),
                    receipt(session='helper_mp_' + 'c' * 32),
                    receipt(prefix=short_field(0x12, SESSION)),
                    receipt(prefix=short_field(0x1a, CALL))]
        for raw in fixtures:
            with self.subTest(raw=raw), self.assertRaises(transport.MiniProgramError):
                transport.decode_response(raw, SESSION, CALL, 1)

    def test_native_deadline_and_partial_receipt_are_unknown_without_retry(self):
        for raw, count in ((receipt(error=1), 1), (receipt(), 2), (receipt(results=[]), 1)):
            with self.subTest(count=count), self.assertRaises(transport.MiniProgramOutcomeUnknown):
                transport.decode_response(raw, SESSION, CALL, count)

    def test_malformed_lengths_varints_and_wire_types_are_rejected(self):
        malformed = [b'\x00', b'\x12\x05x', b'\x12\x80', b'\x12' + b'\xff' * 10 + b'\x01',
                     b'\x09short', b'\x0dxx', b'\x0b', short_field(0x12, b'\xff')]
        for wire in malformed:
            with self.subTest(wire=wire), self.assertRaises(transport.MiniProgramError):
                transport.decode_response(wrapped_wire(wire), SESSION, CALL, 1)

    def test_invalid_envelopes_and_native_error_do_not_leak_private_body(self):
        fixtures = [b'not json', b'[]', json.dumps({'call_id': CALL, 'resp_args': '!'}).encode(),
                    receipt(results=[{'code': 7, 'data': {'private': 'token-secret'}}]),
                    receipt(results=[{'code': False, 'data': {'data': {}}}]),
                    receipt(results=[{'code': 'private-secret', 'data': {'data': {}}}]),
                    receipt(results=[{'code': 0, 'data': {'data': 'private-secret'}}])]
        for raw in fixtures:
            with self.subTest(raw=raw), self.assertRaises(transport.MiniProgramError) as caught:
                transport.decode_response(raw, SESSION, CALL, 1)
            self.assertNotIn('secret', str(caught.exception))

    def test_request_size_command_count_and_nonfinite_values_rejected_before_ipc(self):
        for requests in ([], REQUESTS * 9, [{'method': 'Unknown', 'params': {}}],
                         [{'method': 'Input.insertText', 'params': {}, 'extra': True}],
                         [{'method': 'Input.insertText', 'params': []}],
                         [{'method': 'Input.insertText', 'params': {'text': '汉' * 6000}}],
                         [{'method': 'Input.dispatchTouchEvent', 'params': {'x': float('nan')}}]):
            with self.subTest(requests=str(requests)[:120]), self.assertRaises(ValueError):
                transport.encode_request(SESSION, CALL, requests)
        self.assertIsInstance(transport.encode_request(SESSION, CALL, REQUESTS * 8), bytes)
        for session, call_id in (('bad-session', CALL), (SESSION, 'bad-call')):
            with self.subTest(session=session, call_id=call_id), self.assertRaises(ValueError):
                transport.encode_request(session, call_id, REQUESTS)


class ExchangeTests(unittest.TestCase):
    def setUp(self):
        self.channel = Mock(pending=False)
        self.channel.allocate.return_value = ADDRESS
        self.channel.call.return_value = 0
        stopping = patch.object(transport, '_stop', return_value=True)
        self.stop = stopping.start()
        self.addCleanup(stopping.stop)
        identifier = patch.object(transport.uuid, 'uuid4', return_value=SimpleNamespace(hex='b' * 32))
        identifier.start()
        self.addCleanup(identifier.stop)

    def exchange(self, cancelled=lambda: False):
        return transport._exchange(self.channel, BASE, EXPORTS, 42, SESSION, REQUESTS, cancelled)

    def test_complete_response_uses_bounded_read_and_one_native_submission(self):
        raw = receipt()
        self.channel.read_memory.side_effect = [struct.pack('<8I', c.sizeof(transport.CommandConfig),
            42, 2, 0, len(raw), 0, 1, len(raw)), raw]
        self.assertEqual(self.exchange(), [{'text': '中文😀'}])
        self.assertEqual(self.channel.read_memory.call_args_list,
                         [call(ADDRESS, 32), call(ADDRESS + transport.CommandConfig.response.offset, len(raw))])
        self.channel.call.assert_called_once_with(BASE + EXPORTS[b'MpCommand'], ADDRESS)
        self.stop.assert_called_once_with(self.channel, BASE, EXPORTS)

    def test_cancel_before_admission_never_submits(self):
        with self.assertRaisesRegex(transport.MiniProgramError, '取消'):
            self.exchange(cancelled=lambda: True)
        self.channel.call.assert_not_called()
        self.channel.read_memory.assert_not_called()
        self.stop.assert_called_once()

    def test_cancel_after_admission_is_unknown_and_stops_without_retry(self):
        cancelled = Mock(side_effect=[False, True])
        with self.assertRaises(transport.MiniProgramOutcomeUnknown):
            self.exchange(cancelled)
        self.channel.call.assert_called_once()
        self.channel.read_memory.assert_not_called()
        self.stop.assert_called_once()

    def test_deadline_does_not_wait_or_repeat_admission(self):
        with patch.object(transport.time, 'monotonic', side_effect=[0, 36]):
            with self.assertRaises(transport.MiniProgramOutcomeUnknown):
                self.exchange()
        self.channel.call.assert_called_once()
        self.channel.read_memory.assert_not_called()
        self.stop.assert_called_once()

    def test_invalid_native_response_sizes_never_read_payload(self):
        for length in (0, 1048576, 1048577):
            self.channel.reset_mock()
            self.channel.read_memory.return_value = struct.pack('<8I', 0, 42, 2, 0, length, 0, 0, 0)
            with self.subTest(length=length), self.assertRaises(transport.MiniProgramError):
                self.exchange()
            self.channel.read_memory.assert_called_once_with(ADDRESS, 32)

    def test_native_buffer_overflow_is_unknown_and_still_stops(self):
        self.channel.read_memory.return_value = struct.pack('<8I', 0, 42, 3, 0, 0, 0, 0, 0)
        with self.assertRaises(transport.MiniProgramOutcomeUnknown):
            self.exchange()
        self.channel.read_memory.assert_called_once_with(ADDRESS, 32)
        self.stop.assert_called_once()

    def test_remote_admission_timeout_keeps_pending_and_reports_unknown(self):
        def timed_out(*args):
            self.channel.pending = True
            raise TimeoutError('private-native-diagnostic')
        self.channel.call.side_effect = timed_out
        self.stop.return_value = False
        with self.assertRaises(transport.MiniProgramOutcomeUnknown) as caught:
            self.exchange()
        self.assertTrue(self.channel.pending)
        self.assertNotIn('private-native-diagnostic', str(caught.exception))
        self.channel.call.assert_called_once()


class NativeLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.channel = Mock(pending=False)
        self.channel.allocate.return_value = ADDRESS
        self.channel.call.return_value = 0
        initial = {'weixin.dll': (0x200000, 'offline-core.dll'),
                   'wmpf_host_export_x64.dll': (0x300000, 'offline-host.dll')}
        loaded = initial | {BRIDGE.name.lower(): (BASE, str(BRIDGE))}
        self.native = SimpleNamespace(modules=Mock(side_effect=[initial, loaded]))
        patches = [patch.object(transport, '_Channel', return_value=self.channel),
                   patch.object(transport, '_native', return_value=self.native),
                   patch.object(transport, '_ProcessMutex', return_value=nullcontext()),
                   patch.object(transport, '_exports', return_value=EXPORTS),
                   patch.object(transport, '_verify_module'), patch.object(transport, '_check_account')]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def execute(self, **kwargs):
        return transport.execute_commands(42, ACCOUNT, APP, SESSION, REQUESTS,
                                          bridge_path=BRIDGE, check_app=False, **kwargs)

    def test_cancel_before_load_closes_channel_without_loading_or_freeing_library(self):
        with self.assertRaisesRegex(transport.MiniProgramError, '取消'):
            self.execute(cancelled=lambda: True)
        self.channel.system.assert_not_called()
        self.channel.call.assert_not_called()
        self.channel.close.assert_called_once()

    def test_success_stops_before_freeing_library_and_closes_channel(self):
        with patch.object(transport, '_exchange', return_value=[{'ok': True}]):
            self.assertEqual(self.execute(), [{'ok': True}])
        self.assertEqual(self.channel.call.call_args_list,
                         [call(BASE + EXPORTS[b'MpStopCommand']), call(BASE + EXPORTS[b'MpCanUnload'])])
        self.assertEqual([item.args[0] for item in self.channel.system.call_args_list],
                         [b'LoadLibraryW', b'FreeLibrary'])
        self.assertLess(self.channel.mock_calls.index(call.call(BASE + EXPORTS[b'MpCanUnload'])),
                        self.channel.mock_calls.index(call.system(b'FreeLibrary', BASE)))
        self.assertLess(self.channel.mock_calls.index(call.system(b'FreeLibrary', BASE)),
                        self.channel.mock_calls.index(call.close()))
        self.channel.close.assert_called_once()

    def test_pending_remote_call_never_stops_or_frees_library(self):
        def pending(*args):
            self.channel.pending = True
            raise transport.MiniProgramOutcomeUnknown('pending fixture')
        with patch.object(transport, '_exchange', side_effect=pending):
            with self.assertRaises(transport.MiniProgramOutcomeUnknown):
                self.execute()
        self.channel.call.assert_not_called()
        self.assertEqual([item.args[0] for item in self.channel.system.call_args_list], [b'LoadLibraryW'])
        self.assertTrue(self.channel.pending)
        self.channel.close.assert_called_once()

    def test_stop_denied_keeps_library_and_marks_channel_pending(self):
        self.channel.call.return_value = 170
        with patch.object(transport, '_exchange', return_value=[{'ok': True}]):
            with self.assertRaises(transport.MiniProgramOutcomeUnknown):
                self.execute()
        self.channel.call.assert_called_once_with(BASE + EXPORTS[b'MpStopCommand'])
        self.assertEqual([item.args[0] for item in self.channel.system.call_args_list], [b'LoadLibraryW'])
        self.assertTrue(self.channel.pending)
        self.channel.close.assert_called_once()

    def test_stop_exception_keeps_library_and_marks_channel_pending(self):
        self.channel.call.side_effect = TimeoutError('offline stop timeout')
        with patch.object(transport, '_exchange', return_value=[{'ok': True}]):
            with self.assertRaises(TimeoutError):
                self.execute()
        self.assertTrue(self.channel.pending)
        self.assertEqual([item.args[0] for item in self.channel.system.call_args_list], [b'LoadLibraryW'])
        self.channel.close.assert_called_once()

    def test_invalid_request_is_rejected_before_native_discovery(self):
        with self.assertRaises(ValueError):
            transport.execute_commands(42, ACCOUNT, APP, SESSION, [], bridge_path=BRIDGE)
        self.native.modules.assert_not_called()


if __name__ == '__main__':
    unittest.main()
