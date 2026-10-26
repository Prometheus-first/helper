"""朋友圈传输离线测试：假 Channel/PE/进程句柄，从不打开真实微信进程。"""
import ctypes as c
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from native import moments_transport as transport

ACCOUNT = 'wxid_offline_robot'
EXPORTS = {name: index * 0x100 for index, name in enumerate(
    (transport._INSPECT, transport._PUBLISH, transport._WAIT, transport._REMOVE), 1)}
BASE = 0x100000


def wide(value):
    raw = bytes(value)
    return raw.decode('utf-16-le').split('\0', 1)[0]


class FakeChannel:
    def __init__(self, fixture, pid):
        self.fixture, self.pid = fixture, pid
        self.api = SimpleNamespace(modules=lambda _: fixture.loaded)
        self.pending, self.closed = False, False
        self.buffers = {}

    def verify_client(self):
        self.fixture.events.append('verify_client')

    def read_memory(self, address, size):
        raise AssertionError('账号读取应由离线桩完成')

    def allocate(self, raw):
        if len(raw) == 32144 and self.fixture.fail_config_allocate:
            raise OSError('private-path-and-content')
        pointer = len(self.buffers) + 1
        self.buffers[pointer] = raw
        return pointer

    def system(self, name, parameter):
        self.fixture.events.append(name)
        if name == b'LoadLibraryW':
            self.fixture.loaded[self.fixture.bridge.name.lower()] = (BASE, str(self.fixture.bridge))
        elif name == b'FreeLibrary':
            self.fixture.loaded.pop(self.fixture.bridge.name.lower(), None)
        return 0

    def call(self, address, parameter=None):
        name = next(name for name, offset in EXPORTS.items() if BASE + offset == address)
        self.fixture.events.append(name)
        if name in (transport._WAIT, transport._REMOVE):
            return self.fixture.cleanup_code
        if self.fixture.remote_timeout:
            self.pending = True
            raise TimeoutError('private-path-and-content')
        config = transport.MomentsConfig.from_buffer_copy(self.buffers[parameter])
        self.fixture.config = config
        path = Path(wide(config.base.log_path))
        self.fixture.report_path = path
        execute = name == transport._PUBLISH
        report = {'operation': 'moments_publish' if execute else 'moments_inspect',
                  'account': ACCOUNT, 'visibility': 'private', 'image_count': config.image_count,
                  'media_ready': config.image_count, 'text_bytes': len(config.text), 'exception': 0, 'status': 0,
                  'outcome': 'completed' if execute else 'inspect_ready', 'inspected': not execute,
                  'accepted': execute, 'completion_success': execute, 'server_result_known': execute,
                  'ret_code': 0 if execute else None, 'sns_id': '1234567890' if execute else '0',
                  'account_matches': True, 'unload_blocked': False,
                  'private_path': 'C:\\private\\picture.png'}
        report.update(self.fixture.report_changes)
        if not self.fixture.no_report:
            path.write_text(self.fixture.report_raw or json.dumps(report), encoding='utf-8')
        return self.fixture.dispatch_code

    def close(self):
        self.closed = True


class TransportTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.bridge = self.root / 'probe.dll'
        self.bridge.write_bytes(b'offline-placeholder')
        self.core = self.root / 'Weixin.dll'
        self.core.write_bytes(b'offline-placeholder')
        self.loaded = {'weixin.dll': (0x200000, str(self.core)), self.bridge.name.lower(): (BASE, str(self.bridge))}
        self.events, self.channels = [], []
        self.flags, self.cleanup_code, self.dispatch_code = 3, 0, 0
        self.fail_config_allocate, self.remote_timeout, self.no_report = False, False, False
        self.report_changes, self.report_raw = {}, None
        self.report_path, self.config = None, None
        def channel(pid):
            item = FakeChannel(self, pid)
            self.channels.append(item)
            return item
        self.patches = [patch.object(transport, '_Channel', side_effect=channel),
                        patch.object(transport, '_exports', side_effect=lambda _: (EXPORTS, self.flags)),
                        patch.object(transport, '_core_profile', return_value=SimpleNamespace(version='4.1.15.56')),
                        patch('wxbot_framework.account.read_current_account', return_value=ACCOUNT),
                        patch.object(transport, '_watch_late_report')]
        self.mocks = [item.start() for item in self.patches]
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])

    def publish(self, **options):
        return transport.publish_moments(123, ACCOUNT, '离线正文😀', bridge_path=self.bridge,
                                         workspace=self.root, **options)

    def inspect(self, **options):
        return transport.inspect_moments(123, ACCOUNT, '离线正文😀', bridge_path=self.bridge,
                                         workspace=self.root, **options)

    def test_abi_matches_native_layout_and_utf16_paths(self):
        self.assertEqual(c.sizeof(transport.MomentsConfig), 32144)
        self.assertEqual(transport.MomentsConfig.image_paths.offset, 1704)
        self.assertEqual(transport.MomentsConfig.text.offset, 20136)
        self.inspect()
        config = self.config
        self.assertEqual(config.base.size, 32144)
        self.assertEqual(config.base.pid, 123)
        self.assertEqual(config.base.text, b'')
        self.assertEqual(config.self_wxid, ACCOUNT.encode())
        self.assertEqual(config.visibility, 2)
        self.assertEqual(config.timeout_ms, 120000)
        self.assertEqual(config.reserved, 0)
        self.assertEqual(config.text, '离线正文😀'.encode())
        self.assertEqual(wide(config.base.log_path), str(self.report_path))

    def test_publish_requires_complete_receipt_then_waits_and_deletes_private_report(self):
        result = self.publish()
        self.assertEqual(result, {'status': 'published', 'sns_id': '1234567890', 'unload_blocked': False})
        self.assertIn(transport._WAIT, self.events)
        self.assertNotIn(transport._REMOVE, self.events)
        self.assertNotIn(b'FreeLibrary', self.events)
        self.assertFalse(self.report_path.exists())
        self.assertTrue(self.channels[0].closed)
        for private in (ACCOUNT, str(self.root), '离线正文', 'private_path'):
            self.assertNotIn(private, json.dumps(result))

    def test_inspect_is_distinct_from_publish_and_does_not_report_published(self):
        result = self.inspect()
        self.assertEqual(result['status'], 'inspected')
        self.assertTrue(result['inspected'])
        self.assertEqual(result['text_bytes'], len('离线正文😀'.encode()))
        self.assertIn(transport._INSPECT, self.events)
        self.assertNotIn(transport._PUBLISH, self.events)
        self.assertNotIn('sns_id', result)
        self.assertFalse(self.report_path.exists())

    def test_verified_local_png_jpeg_paths_use_utf16_abi_and_sources_are_preserved(self):
        png, jpeg = self.root / '临时图片.png', self.root / '临时图片.jpg'
        Image.new('RGB', (8, 8), 'red').save(png, format='PNG')
        Image.new('RGB', (8, 8), 'blue').save(jpeg, format='JPEG')
        before = [path.read_bytes() for path in (png, jpeg)]
        result = self.inspect(images=[png, jpeg])
        self.assertEqual(result['status'], 'inspected')
        self.assertEqual(result['image_count'], 2)
        self.assertEqual(self.config.image_count, 2)
        self.assertEqual([wide(self.config.image_paths[index]) for index in range(2)], [str(png), str(jpeg)])
        self.assertEqual([path.read_bytes() for path in (png, jpeg)], before)
        for images in ([png, png], [png] * 10):
            with self.assertRaises(ValueError):
                self.inspect(images=images)

    def test_closed_submit_gate_rejects_without_opening_channel(self):
        for flags in (None, 0, 1, 5, 7 | 8):
            self.flags = flags
            result = self.publish()
            self.assertEqual((result['status'], result['phase']), ('failed', 'preflight'))
        self.assertEqual(self.channels, [])
        self.assertEqual(self.events, [])

    def test_inspect_does_not_require_submit_gate(self):
        self.flags = 1
        self.assertEqual(self.inspect()['status'], 'inspected')

    def test_default_inspect_rejects_event_probe_or_unknown_gate_before_channel_open(self):
        for flags in (None, 0, 5, 7):
            self.flags = flags
            self.assertEqual(self.inspect()['status'], 'failed')
        self.assertEqual(self.channels, [])

    def test_explicit_event_probe_allows_inspect_only_with_submit_gate_closed(self):
        self.flags = 5
        self.report_changes = {'event_probe_enabled': True, 'native_submit_enabled': False}
        result = self.inspect(allow_event_probe=True)
        self.assertEqual(result['status'], 'inspected')
        self.assertTrue(result['event_probe_enabled'])
        self.assertFalse(result['native_submit_enabled'])
        self.assertIn(transport._INSPECT, self.events)
        self.assertNotIn(transport._PUBLISH, self.events)
        self.assertFalse(self.report_path.exists())

    def test_explicit_event_probe_rejects_submit_enabled_build_and_non_boolean_opt_in(self):
        self.flags = 7
        result = self.inspect(allow_event_probe=True)
        self.assertEqual((result['status'], result['phase']), ('failed', 'preflight'))
        self.assertEqual(self.channels, [])
        self.assertEqual(self.events, [])
        self.flags = 5
        for value in (1, 'true', None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.inspect(allow_event_probe=value)
        with self.assertRaises(TypeError):
            self.publish(allow_event_probe=True)
        self.assertEqual(self.channels, [])

    def test_production_reuse_requires_already_loaded_matching_bridge(self):
        self.loaded.pop(self.bridge.name.lower())
        result = self.publish()
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn(b'LoadLibraryW', self.events)
        self.assertNotIn(b'FreeLibrary', self.events)
        self.assertTrue(self.channels[0].closed)

    def test_probe_loads_own_copy_and_unloads_only_after_remove_guard(self):
        self.loaded.pop(self.bridge.name.lower())
        self.loaded['text_bridge.dll'] = (0x900000, 'C:\\original\\text_bridge.dll')
        result = self.inspect(keep_loaded=False)
        self.assertEqual(result['status'], 'inspected')
        self.assertEqual([event for event in self.events if event != 'verify_client'],
                         [b'LoadLibraryW', transport._INSPECT, transport._REMOVE, b'FreeLibrary'])
        self.assertIn('text_bridge.dll', self.loaded)
        self.assertNotIn(self.bridge.name.lower(), self.loaded)

    def test_probe_refuses_to_adopt_or_remove_already_running_bridge(self):
        result = self.inspect(keep_loaded=False)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn(transport._INSPECT, self.events)
        self.assertNotIn(transport._REMOVE, self.events)
        self.assertNotIn(b'FreeLibrary', self.events)

    def test_wrong_same_name_bridge_or_unsupported_core_fails_before_dispatch(self):
        self.loaded[self.bridge.name.lower()] = (BASE, str(self.root / 'foreign/probe.dll'))
        self.assertEqual(self.publish()['status'], 'failed')
        self.assertNotIn(transport._PUBLISH, self.events)
        self.mocks[2].return_value = None
        self.assertEqual(self.publish()['status'], 'failed')
        self.assertNotIn(transport._PUBLISH, self.events)

    def test_account_mismatch_before_dispatch_does_not_call_native_export(self):
        self.mocks[3].return_value = 'wxid_other_account'
        self.assertEqual(self.publish()['status'], 'failed')
        self.assertNotIn(transport._PUBLISH, self.events)

    def test_account_switch_after_dispatch_is_unknown(self):
        self.mocks[3].side_effect = [ACCOUNT, ACCOUNT, 'wxid_other_account']
        result = self.publish()
        self.assertEqual(result['status'], 'unknown')
        self.assertFalse(result['unload_blocked'])
        self.assertFalse(self.report_path.exists())

    def test_preflight_failure_report_is_definite_failure(self):
        self.dispatch_code = 50
        self.report_changes = {'outcome': 'native_abi_unverified', 'accepted': False,
                               'completion_success': False, 'server_result_known': False, 'status': 50}
        result = self.publish()
        self.assertEqual((result['status'], result['phase']), ('failed', 'preflight'))
        self.assertFalse(self.report_path.exists())

    def test_accepted_report_is_intermediate_and_timeout_is_unknown(self):
        self.report_changes = {'outcome': 'accepted', 'completion_success': False, 'server_result_known': False}
        self.cleanup_code = 170
        with patch.object(transport.time, 'monotonic', side_effect=[0, 11]):
            result = self.publish(timeout_ms=10000)
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['unload_blocked'])
        self.assertNotIn(b'FreeLibrary', self.events)
        self.mocks[4].assert_called_once_with(self.report_path, 123)

    def test_guard_busy_keeps_probe_loaded_even_with_success_report(self):
        self.loaded.pop(self.bridge.name.lower())
        self.cleanup_code = 170
        result = self.inspect(keep_loaded=False)
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['unload_blocked'])
        self.assertIn(self.bridge.name.lower(), self.loaded)
        self.assertNotIn(b'FreeLibrary', self.events)

    def test_remote_thread_timeout_is_unknown_and_does_not_attempt_unload(self):
        self.remote_timeout = True
        result = self.publish()
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['unload_blocked'])
        self.assertTrue(self.channels[0].pending)
        self.assertTrue(self.channels[0].closed)
        self.assertNotIn(transport._WAIT, self.events)
        self.assertNotIn(b'FreeLibrary', self.events)

    def test_initialization_failure_after_own_load_uses_cleanup_guard(self):
        self.loaded.pop(self.bridge.name.lower())
        self.fail_config_allocate = True
        result = self.inspect(keep_loaded=False)
        self.assertEqual(result['status'], 'failed')
        self.assertIn(transport._REMOVE, self.events)
        self.assertIn(b'FreeLibrary', self.events)
        self.assertNotIn(transport._INSPECT, self.events)

    def test_initialization_guard_busy_becomes_unknown_and_preserves_loaded_copy(self):
        self.loaded.pop(self.bridge.name.lower())
        self.fail_config_allocate, self.cleanup_code = True, 170
        result = self.inspect(keep_loaded=False)
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['unload_blocked'])
        self.assertIn(self.bridge.name.lower(), self.loaded)
        self.assertNotIn(b'FreeLibrary', self.events)

    def test_incomplete_mismatched_or_invalid_receipts_cannot_publish(self):
        changes = [{'account': 'wxid_other'}, {'operation': 'moments_inspect'}, {'text_bytes': 1},
                   {'image_count': 1}, {'visibility': 'public'}, {'sns_id': ''}, {'sns_id': '0'},
                   {'sns_id': '18446744073709551616'}, {'sns_id': 'C:/private.png'},
                   {'accepted': False}, {'completion_success': False}, {'server_result_known': False},
                   {'ret_code': 1}, {'ret_code': False}, {'exception': 1}, {'exception': False},
                   {'media_ready': -1}, {'unload_blocked': True}]
        for row in changes:
            with self.subTest(row=row):
                self.report_changes = row
                self.assertEqual(self.publish()['status'], 'unknown')
                self.assertFalse(self.report_path.exists())

    def test_malformed_duplicate_or_absent_report_is_unknown_and_deleted(self):
        for raw in ('{bad', '{"outcome":"completed","outcome":"completed"}', '[]'):
            self.report_raw = raw
            with patch.object(transport.time, 'monotonic', side_effect=[0, 11]):
                result = self.publish(timeout_ms=10000)
            self.assertEqual(result['status'], 'unknown')
            self.assertFalse(self.report_path.exists())
        self.no_report = True
        with patch.object(transport.time, 'monotonic', side_effect=[0, 11]):
            self.assertEqual(self.publish(timeout_ms=10000)['status'], 'unknown')

    def test_validation_bounds_before_channel_open(self):
        values = [dict(pid=True), dict(pid=0), dict(account='filehelper'), dict(content=''),
                  dict(content='x\0y'), dict(content='😀' * 1001), dict(content='\ud800'),
                  dict(images='C:/private.png'), dict(images=['https://example.invalid/a.png']),
                  dict(images=['relative.png']), dict(images=['C:/not-found.png']),
                  dict(visibility='friends'), dict(timeout_ms=9999), dict(timeout_ms=True)]
        for options in values:
            params = dict(pid=123, account=ACCOUNT, content='离线正文', bridge_path=self.bridge, workspace=self.root)
            params.update(options)
            with self.subTest(options=repr(options)[:60]), self.assertRaises(ValueError):
                transport.publish_moments(**params)
        self.assertEqual(self.channels, [])


class CapabilityTests(unittest.TestCase):
    def test_reads_constant_function_or_readonly_data_without_loading_dll(self):
        for characteristics, raw, expected in ((0x60000020, b'\xb8\x01\0\0\0\xc3', 1),
                                                (0x40000040, b'\x03\0\0\0', 3),
                                                (0x60000020, b'\xe9\x01\0\0\0\xc3', None),
                                                (0xc0000040, b'\x03\0\0\0', None)):
            entries = [SimpleNamespace(name=name, address=value, forwarder=None) for name, value in EXPORTS.items()]
            entries.append(SimpleNamespace(name=transport._CAPABILITY, address=0x500, forwarder=None))
            pe = SimpleNamespace(FILE_HEADER=SimpleNamespace(Machine=0x8664),
                                 DIRECTORY_ENTRY_EXPORT=SimpleNamespace(symbols=entries),
                                 parse_data_directories=Mock(), close=Mock(),
                                 get_section_by_rva=lambda _: SimpleNamespace(Characteristics=characteristics),
                                 get_data=lambda *_: raw)
            with self.subTest(characteristics=characteristics, raw=raw), \
                    patch.object(transport.pefile, 'PE', return_value=pe), patch.object(transport, '_native') as native:
                _, flags = transport._exports(Path('offline.dll'))
                self.assertEqual(flags, expected)
                native.assert_not_called()
                pe.close.assert_called_once()

    def test_capability_requires_fingerprint_export_and_enabled_submit_gate(self):
        with tempfile.TemporaryDirectory() as folder:
            bridge = Path(folder).resolve() / 'offline.dll'
            bridge.write_bytes(b'offline')
            with patch.object(transport, '_exports') as exports, patch.object(transport, '_core_profile') as core, \
                    patch.object(transport, '_native') as native:
                core.return_value = SimpleNamespace(version='4.1.15.56')
                for flags, expected in ((None, False), (0, False), (1, False), (3, True), (5, False), (7, True), (11, False)):
                    exports.return_value = EXPORTS, flags
                    result = transport.moments_capabilities(bridge_path=bridge, core_path=bridge)
                    self.assertIs(result['supported'], expected)
                core.return_value = None
                self.assertFalse(transport.moments_capabilities(bridge_path=bridge, core_path=bridge)['supported'])
                exports.return_value = {}, 3
                self.assertFalse(transport.moments_capabilities(bridge_path=bridge, core_path=bridge)['supported'])
                native.assert_not_called()


class ChannelMemoryTests(unittest.TestCase):
    def api(self):
        writes = []
        def write(process, address, buffer, size, count):
            count._obj.value = size
            writes.append(c.string_at(buffer, size))
            return True
        kernel = SimpleNamespace(OpenProcess=Mock(return_value=10), CloseHandle=Mock(),
                                 VirtualAllocEx=Mock(return_value=0x50000),
                                 WriteProcessMemory=Mock(side_effect=write), VirtualFreeEx=Mock())
        return SimpleNamespace(K=kernel, check=lambda result, _: result or None,
                               remote_call=Mock(return_value=0)), writes

    def test_completed_remote_call_zeros_and_frees_buffer_then_closes_handle(self):
        api, writes = self.api()
        with patch.object(transport, '_native', return_value=api):
            channel = transport._Channel(123)
            pointer = channel.allocate(b'private-payload')
            channel.call(0x1234, pointer)
            channel.close()
        self.assertEqual(writes, [b'private-payload', b'\0' * len(b'private-payload')])
        api.K.VirtualFreeEx.assert_called_once_with(10, 0x50000, 0, 0x8000)
        api.K.CloseHandle.assert_called_once_with(10)

    def test_timed_out_remote_thread_keeps_live_input_buffer_but_closes_local_handle(self):
        api, writes = self.api()
        api.remote_call.side_effect = TimeoutError('未完成')
        with patch.object(transport, '_native', return_value=api):
            channel = transport._Channel(123)
            pointer = channel.allocate(b'private-payload')
            with self.assertRaises(TimeoutError):
                channel.call(0x1234, pointer)
            channel.close()
        self.assertTrue(channel.pending)
        self.assertEqual(writes, [b'private-payload'])
        api.K.VirtualFreeEx.assert_not_called()
        api.K.CloseHandle.assert_called_once_with(10)


if __name__ == '__main__':
    unittest.main()
