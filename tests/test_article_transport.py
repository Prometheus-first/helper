"""Active article transport tests use synthetic memory and a fake remote channel."""
from __future__ import annotations

import ctypes as c
import json
from pathlib import Path
import struct
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from native import article_transport as transport

ACCOUNT = 'wxid_synthetic_owner'
PUBLISHER = 'gh_synthetic_author'
URL = 'https://mp.weixin.qq.com/s?__biz=U3ludGhldGlj&mid=123&idx=1&sn=' + 'a' * 32
BASE = 0x900000
EXPORTS = {transport._START: 0x100, transport._STOP: 0x200, transport._ABI: 0x300}


class TreeMemory:
    host, manager, creation, handler, url = 0x100000, 0x200000, 0x300000, 0x400000, 0x500000
    def __init__(self):
        self.bytes = {}; self.reads = []; self.hook = None
        self.q(self.host + 0x5d3720, self.manager)
        self.q(self.manager, self.host + 0x4e71e0)
        self.q(self.host + 0x4e71e0 + 0x58, self.host + 0x1cb6b0)
        self.put(self.manager + 0xa0, struct.pack('<QQQ', self.creation, self.creation, 1))
        self.put(self.manager + 0x40, struct.pack('<QQQ', self.handler, self.handler, 1))
        self.put(self.creation, self.node(37, self.host + 0x4ea3e0, self.manager + 0xa8))
        self.put(self.handler, self.node(37, 0x700000, self.manager + 0x48))
        self.set_url(transport._FEED + b'?private_query=synthetic-never-read')
    @staticmethod
    def node(key, value, parent, left=0, right=0):
        result = bytearray(48)
        struct.pack_into('<QQQ', result, 0, left, right, parent)
        struct.pack_into('<I', result, 0x20, key)
        struct.pack_into('<Q', result, 0x28, value)
        return bytes(result)
    def set_url(self, value, *, capacity=None):
        capacity = len(value) + 1 if capacity is None else capacity
        self.put(self.creation + 0xe8, struct.pack('<QQQ', self.url, len(value), (1 << 63) | capacity))
        self.put(self.url, value)
    def q(self, address, value): self.put(address, struct.pack('<Q', value))
    def put(self, address, raw):
        for offset, value in enumerate(raw):self.bytes[address + offset] = value
    def read(self, address, amount):
        self.reads.append((address, amount))
        if self.hook:
            selected = self.hook(address, amount)
            if selected is not None:return selected
        try:return bytes(self.bytes[address + offset] for offset in range(amount))
        except KeyError:raise OSError('synthetic missing bytes') from None


class FrameIdentityTests(unittest.TestCase):
    def test_dynamic_frame_comes_from_live_builtin_registry_without_reading_query(self):
        memory = TreeMemory()
        self.assertEqual(transport._frame_binding(memory.read, memory.host),
                         (37, memory.manager, memory.creation, memory.handler))
        # The URI may contain private query values; identity only reads the fixed prefix and '?'.
        url_reads = [(a, n) for a, n in memory.reads if memory.url <= a < memory.url + 4096]
        self.assertTrue(url_reads)
        self.assertTrue(all(a + n <= memory.url + len(transport._FEED) + 1 for a, n in url_reads))

    def test_missing_live_handler_and_wrong_page_are_rejected(self):
        for change in ('handler', 'uri', 'capacity'):
            memory = TreeMemory()
            if change == 'handler':memory.q(memory.handler + 0x28, 0)
            elif change == 'uri':memory.set_url(b'weixin://resourceid/SubscriptionDisorder/unrelated.html')
            else:memory.set_url(transport._FEED, capacity=2)
            with self.subTest(change=change), self.assertRaises(ValueError):
                transport._frame_binding(memory.read, memory.host)

    def test_handler_replacement_during_snapshot_is_rejected(self):
        memory = TreeMemory(); calls = 0
        def hook(address, size):
            nonlocal calls
            if address == memory.handler and size == 48:
                calls += 1
                if calls > 1:return memory.node(37, 0x700010, memory.manager + 0x48)
        memory.hook = hook
        with self.assertRaises(ValueError):transport._frame_binding(memory.read, memory.host)

    def test_cyclic_tree_and_wrong_parent_are_rejected(self):
        memory = TreeMemory()
        memory.q(memory.creation, memory.creation)
        with self.assertRaises(ValueError):transport._frame_binding(memory.read, memory.host)
        memory = TreeMemory()
        child = 0x310000
        memory.put(memory.creation, memory.node(37, memory.host + 0x4ea3e0, memory.manager + 0xa8, left=child))
        memory.put(child, memory.node(36, memory.host + 0x4ea3e0, 0xdeadbeef))
        memory.put(memory.manager + 0xa0, struct.pack('<QQQ', child, memory.creation, 2))
        with self.assertRaises(ValueError):transport._frame_binding(memory.read, memory.host)


class ArticleBuildPairTests(unittest.TestCase):
    def test_matching_builds_work_but_mixed_or_unknown_hosts_fail(self):
        builds = {row[0]: row for row in transport._ARTICLE_BUILDS}
        def check_pair(core_version, host_version):
            core = builds[core_version]
            host = builds[host_version]
            def fingerprint(path, size, digest):
                actual = core[1:3] if path == 'core' else host[3:5]
                if (size, digest) != actual:
                    raise transport.ArticleTransportError('fingerprint mismatch', 501)
                return (path, size, digest)
            with patch.object(transport, '_fingerprint', side_effect=fingerprint):
                return transport._verified_article_modules('core', 'host')
        for version in ('4.2.0.11', '4.2.0.12'):
            self.assertEqual(check_pair(version, version)[0].version, version)
        with self.assertRaises(transport.ArticleTransportError):
            check_pair('4.2.0.11', '4.2.0.12')
        with self.assertRaises(transport.ArticleTransportError):
            check_pair('4.2.0.12', '4.2.0.11')


@unittest.skipUnless(sys.platform == 'win32', 'Windows process snapshots require WinDLL')
class SessionSnapshotTests(unittest.TestCase):
    def snapshot(self, *, replace_host=False):
        initial = {'weixin.dll': (0x100000, 'synthetic-core.dll'),
                   'wmpf_host_export_x64.dll': (0x200000, 'synthetic-host.dll')}
        latest = dict(initial, **{'article_bridge.dll': (BASE, 'synthetic-bridge.dll')})
        if replace_host:
            latest['wmpf_host_export_x64.dll'] = (0x300000, 'synthetic-host.dll')
        api = SimpleNamespace(modules=Mock(side_effect=[initial, latest]))
        def times(handle, created, *rest):
            created._obj.value = 123
            return 1
        memory = SimpleNamespace(read=Mock(), handle=1, close=Mock(),
                                 kernel=SimpleNamespace(GetProcessTimes=Mock(side_effect=times)))
        with patch.object(transport, '_native', return_value=api), \
                patch.object(transport, '_fingerprint', side_effect=lambda path, *args: (path, 1, 2)), \
                patch('wxbot_framework.moments_reader._Memory', return_value=memory), \
                patch('wxbot_framework.account.read_current_account', return_value=ACCOUNT), \
                patch('wxbot_framework.official_accounts._login_snapshot', return_value=(1, 2, 3)), \
                patch.object(transport, '_frame_binding', return_value=(37, 1, 2, 3)):
            try:
                return transport._snapshot(713, ACCOUNT)
            finally:
                memory.close.assert_called_once()

    def test_bridge_load_does_not_change_selected_session_identity(self):
        self.assertEqual(self.snapshot().identity[:2], (713, 123))

    def test_replacing_required_host_module_still_rejects_snapshot(self):
        with self.assertRaises(transport.ArticleTransportError):
            self.snapshot(replace_host=True)


class ProcessMutexTests(unittest.TestCase):
    def kernel(self, status):
        return type('Kernel', (), {'CreateMutexW': Mock(return_value=713),
                                  'WaitForSingleObject': Mock(return_value=status),
                                  'ReleaseMutex': Mock(return_value=1), 'CloseHandle': Mock(return_value=1)})()

    def test_process_mutex_uses_pid_only_and_releases_only_if_owned(self):
        for status in (0, 128, 258):
            kernel = self.kernel(status)
            with patch.object(transport.c, 'WinDLL', return_value=kernel, create=True):
                mutex = transport._ProcessMutex(713)
                self.assertEqual(mutex.acquire(), status != 258)
                mutex.close(); mutex.close()
            kernel.CreateMutexW.assert_called_once_with(None, False, 'Local\\HelperArticleRead-713')
            kernel.WaitForSingleObject.assert_called_once_with(713, 0)
            self.assertEqual(kernel.ReleaseMutex.call_count, 0 if status == 258 else 1)
            kernel.CloseHandle.assert_called_once_with(713)


class FakeChannel:
    def __init__(self, fixture, pid):
        self.fixture, self.pid = fixture, pid
        self.api = type('API', (), {'modules': lambda _, pid: fixture.loaded})()
        self.buffers, self.pending, self.closed = {}, False, False
        self.freed = False
    def verify_client(self):self.fixture.events.append('verified')
    def allocate(self, data):
        address = (len(self.buffers) + 1) * 0x400000
        self.buffers[address] = bytes(data)
        return address
    def system(self, name, parameter):
        self.fixture.events.append(name)
        if name == b'LoadLibraryW':
            self.fixture.loaded[self.fixture.bridge.name.lower()] = (BASE, self.fixture.load_path or str(self.fixture.bridge))
            return BASE & 0xffffffff
        elif name == b'FreeLibrary':self.fixture.loaded.pop(self.fixture.bridge.name.lower(), None)
        return 1
    def call(self, address, parameter=None):
        name = next(name for name, offset in EXPORTS.items() if address == BASE + offset)
        self.fixture.events.append(name)
        if name == transport._STOP:return self.fixture.cleanup_status
        config = transport.ArticleConfig.from_buffer_copy(self.buffers[parameter])
        self.fixture.config = config
        if self.fixture.start_status:return self.fixture.start_status
        if self.fixture.wait_forever:
            config.stage = 2
        else:
            if config.operation == 1:
                raw = self.fixture.raw or json.dumps({'err_msg': 'H5ExtTransfer:ok', 'jsapi_resp': {'resp_json': json.dumps(self.fixture.response)}}).encode()
            else:
                nonce = config.nonce.decode()
                raw = ('WeixinJSBridge._handleMessageFromWeixin(' + json.dumps({'__msg_type': 'callback', '__callback_id': 'helper_' + nonce,
                         '__params': {'err_msg': 'OpenUrlWithExtraWebview:ok'}}) + ');').encode()
            config.stage, config.status, config.response_size = 4, 0, len(raw)
            self.buffers[config.response_address] = raw
        self.buffers[parameter] = bytes(config)
        return 0
    def read_memory(self, address, size):return self.buffers[address][:size]
    def close(self):
        self.closed = True
        if not self.pending:
            self.freed = True
            self.buffers = {key: bytes(len(value)) for key, value in self.buffers.items()}


class TransportTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.bridge = Path(directory.name).resolve() / 'article_bridge.dll'; self.bridge.write_bytes(b'synthetic')
        self.snapshot = transport._Snapshot(('synthetic-process-and-login',), 37, 0x100000, 0x200000, 0x300000, 0x400000)
        self.response = {'BaseResponse': {'Ret': 0}, 'AccountInfo': {'UserName': PUBLISHER}, 'MsgList': {'Msg': [], 'PagingInfo': {'IsEnd': 1}}}
        self.raw = None; self.loaded = {}; self.channels = []; self.events = []; self.load_path = None
        self.config = None; self.cleanup_status = self.start_status = 0; self.wait_forever = False
        def channel(pid):
            value = FakeChannel(self, pid); self.channels.append(value); return value
        self.mutex = Mock(); self.mutex.acquire.return_value = True
        self.patches = [patch.object(transport, '_snapshot', return_value=self.snapshot), patch.object(transport, '_exports', return_value=EXPORTS),
                        patch.object(transport, '_Channel', side_effect=channel), patch.object(transport, '_ProcessMutex', return_value=self.mutex)]
        self.mocks = [item.start() for item in self.patches]
        self.addCleanup(lambda: [item.stop() for item in reversed(self.patches)])
    def history(self, **options):
        return transport._request(1, 123, ACCOUNT, PUBLISHER, bridge_path=self.bridge, **options)
    def test_fixed_abi_and_dynamic_frame_are_marshaled_then_private_buffers_are_zeroed(self):
        self.assertEqual(c.sizeof(transport.ArticleConfig), 12656)
        self.assertEqual(self.history(offset='c3ludGhldGljLWN1cnNvcg=='), self.response)
        config = self.config
        self.assertEqual(config.frame, 37)
        self.assertGreater(config.request_id, 0)
        self.assertLessEqual(config.request_id, 0x7fffffff)
        self.assertEqual(config.account, ACCOUNT.encode())
        self.assertEqual(config.publisher, PUBLISHER.encode())
        self.assertEqual(config.offset, b'c3ludGhldGljLWN1cnNvcg==')
        self.assertTrue(self.channels[0].freed)
        self.assertEqual(self.loaded, {})
        self.assertEqual(self.events.count(transport._START), 1)
        self.assertIn(transport._STOP, self.events)
        self.assertIn(b'FreeLibrary', self.events)

    def test_open_accepts_only_canonical_article_and_verifies_nonce_ack(self):
        self.assertEqual(transport._request(2, 123, ACCOUNT, PUBLISHER, url=URL, bridge_path=self.bridge), {'opened': True})
        for url in ('https://attacker.invalid/a', URL + '&key=synthetic-private'):
            with self.assertRaises(transport.ArticleTransportError):
                transport._request(2, 123, ACCOUNT, PUBLISHER, url=url, bridge_path=self.bridge)
        self.assertEqual(self.events.count(transport._START), 1)

    def test_wrong_publisher_and_duplicate_response_fields_do_not_escape(self):
        self.response['AccountInfo']['UserName'] = 'gh_other_author'
        with self.assertRaises(transport.ArticleTransportError):self.history()
        self.raw = b'{"err_msg":"H5ExtTransfer:ok","err_msg":"private-synthetic"}'
        with self.assertRaises(transport.ArticleTransportError) as error:self.history()
        self.assertNotIn('private-synthetic', str(error.exception))
        self.assertTrue(all(channel.freed for channel in self.channels))

    def test_preflight_cancellation_performs_no_process_mutation(self):
        with self.assertRaises(transport.ArticleTransportError):self.history(cancelled=lambda: True)
        self.assertFalse(self.channels)

    def test_inflight_cancellation_stops_hook_before_unloading_once(self):
        self.wait_forever = True; calls = 0
        def cancelled():
            nonlocal calls
            calls += 1
            return calls >= 4
        with self.assertRaises(transport.ArticleTransportError):self.history(cancelled=cancelled)
        self.assertEqual(self.events.count(transport._START), 1)
        self.assertEqual(self.events.count(transport._STOP), 1)
        self.assertTrue(self.channels[0].freed)

    def test_login_change_discards_a_successful_response(self):
        changed = transport._Snapshot(('changed-login',), 37, 0x100000, 0x200000, 0x300000, 0x400000)
        self.mocks[0].side_effect = [self.snapshot, self.snapshot, self.snapshot, changed]
        with self.assertRaises(transport.ArticleTransportError):self.history()
        self.assertTrue(self.channels[0].freed)

    def test_timeout_never_retries_and_cleans_the_observer(self):
        self.wait_forever = True
        with patch.object(transport.time, 'monotonic', side_effect=[0, 2]), self.assertRaises(transport.ArticleTransportError) as error:
            self.history(timeout_ms=100)
        self.assertEqual(error.exception.status, 504)
        self.assertEqual(self.events.count(transport._START), 1)
        self.assertTrue(self.channels[0].freed)

    def test_cleanup_failure_keeps_live_allocations_and_module_owned_by_client(self):
        self.cleanup_status = 170
        with self.assertRaises(transport.ArticleTransportError):self.history()
        self.assertTrue(self.channels[0].pending)
        self.assertFalse(self.channels[0].freed)
        self.assertIn(self.bridge.name.lower(), self.loaded)
        self.assertNotIn(b'FreeLibrary', self.events)

    def test_existing_module_is_not_taken_over_or_unloaded(self):
        self.loaded[self.bridge.name.lower()] = (BASE, str(self.bridge))
        with self.assertRaises(transport.ArticleTransportError):self.history()
        self.assertNotIn(transport._START, self.events)
        self.assertNotIn(transport._STOP, self.events)
        self.assertNotIn(b'FreeLibrary', self.events)

    def test_other_worker_mutex_blocks_before_module_loading_and_never_stops_its_request(self):
        self.mutex.acquire.return_value = False
        with self.assertRaises(transport.ArticleTransportError):self.history()
        self.assertFalse(self.channels)
        self.mutex.close.assert_called_once()

    def test_foreign_loader_result_only_releases_own_reference_without_calling_exports(self):
        self.load_path = str(self.bridge.parent / 'foreign' / self.bridge.name)
        with self.assertRaises(transport.ArticleTransportError):self.history()
        self.assertIn(b'FreeLibrary', self.events)
        self.assertNotIn(transport._START, self.events)
        self.assertNotIn(transport._STOP, self.events)

    def test_capability_is_read_only(self):
        self.assertTrue(transport.article_capabilities(123, ACCOUNT, bridge_path=self.bridge)['supported'])
        self.assertFalse(self.channels)


if __name__ == '__main__':unittest.main()
