"""朋友圈读取离线回归：合成对象与假进程，不读取真实微信。"""
from contextlib import ExitStack
from types import SimpleNamespace
import struct
import unittest
from unittest.mock import Mock, patch

from wxbot_framework import moments_reader as reader

NATIVE_MEMORY = reader._Memory

ACCOUNT = 'wxid_offline_moments'
CORE = 0x10000000
SOURCE = 0x200000
CURRENT = 0x210000
PAIRS = 0x220000
ITEM = 0x230000
CONTENT = 0x240000
PROFILE = SimpleNamespace(version='4.1.15.56', rva=lambda value: 0xBB92FA0)


class Memory:
    def __init__(self):
        self.data = bytearray(0x80000)
        self.closed = False
        self.heap = 0x260000
        self.read_hook = None
        self.put(SOURCE, struct.pack('<Q', CORE + reader._SOURCE_VTABLE))
        self.put(SOURCE + 0x10, struct.pack('<Q', CORE + reader._SOURCE_SECONDARY_VTABLE))
        self.put(SOURCE + 0x1A0, struct.pack('<QQ', CURRENT, CURRENT + 0x100))
        self.put(CURRENT + 0x30, struct.pack('<QQQ', PAIRS, PAIRS + 16, PAIRS + 16))
        self.put(PAIRS, struct.pack('<QQ', ITEM, ITEM + 0x1000))
        self.put(ITEM, struct.pack('<Q', 18446744073709550001))
        self.string(ITEM + 8, '离线昵称')
        self.string(ITEM + 0x190, ACCOUNT)
        self.put(ITEM + 0x1B0, b'\1')
        self.put(ITEM + 0x1B4, struct.pack('<I', 1700000000))
        self.string(ITEM + 0x1B8, '只读列表里的正文\n第二行')
        self.put(ITEM + 0x300, struct.pack('<QQ', CONTENT, CONTENT + 0x1000))
        self.put(CONTENT + 0x60, struct.pack('<I', 1))
        self.put(CONTENT + 0x118, struct.pack('<QQ', 0x270000, 0x270020))

    def put(self, address, value):
        offset = address - SOURCE
        self.data[offset:offset + len(value)] = value

    def string(self, address, value):
        raw = value.encode('utf-8')
        if len(raw) <= 15:
            header = raw.ljust(16, b'\0') + struct.pack('<QQ', len(raw), 15)
        else:
            header = struct.pack('<QQQQ', self.heap, 0, len(raw), len(raw))
            self.put(self.heap, raw)
            self.heap += len(raw) + 16
        self.put(address, header)

    def read(self, address, size):
        if self.read_hook:
            result = self.read_hook(address, size)
            if result is not None:
                return result
        if address == CORE + 0xBB92FA0 and size == 8:
            return struct.pack('<Q', SOURCE)
        offset = address - SOURCE
        if offset < 0 or offset + size > len(self.data):
            raise OSError('fixture-private-address')
        return bytes(self.data[offset:offset + size])

    def regions(self, budget):
        budget.check()
        return [(SOURCE, len(self.data))]

    def close(self):
        self.closed = True


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.memory = Memory()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.target = self.stack.enter_context(patch.object(reader, '_target', return_value=(CORE, PROFILE)))
        self.open_memory = self.stack.enter_context(patch.object(reader, '_Memory', return_value=self.memory))
        self.identity = self.stack.enter_context(patch.object(reader, 'read_current_account', return_value=ACCOUNT))

    def run_read(self, **kwargs):
        return reader.read_loaded_moments(123, ACCOUNT, **kwargs)

    def test_read_snapshot_and_keep_uint64_identifier_as_string(self):
        result = self.run_read()
        self.assertEqual(set(result), {'source', 'rows', 'loaded_count', 'truncated'})
        self.assertEqual(result['source'], 'loaded_pc_moments_cache')
        self.assertEqual(result['loaded_count'], 1)
        self.assertFalse(result['truncated'])
        row = result['rows'][0]
        self.assertEqual(row['sns_id'], '18446744073709550001')
        self.assertEqual(row['nickname'], '离线昵称')
        self.assertEqual(row['content'], '只读列表里的正文\n第二行')
        self.assertTrue(row['private'])
        self.assertTrue(row['is_self'])
        self.assertEqual(row['media_count'], 2)
        self.assertEqual(row['time'], '2023-11-15T06:13:20+08:00')
        self.assertEqual(self.identity.call_count, 2)
        self.assertTrue(self.memory.closed)

    def test_limit_reports_truncated_without_reading_extra_item(self):
        self.memory.put(CURRENT + 0x30, struct.pack('<QQQ', PAIRS, PAIRS + 32, PAIRS + 32))
        result = self.run_read(limit=1)
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual(result['loaded_count'], 2)
        self.assertTrue(result['truncated'])

    def test_zero_media_and_empty_content_allowed(self):
        self.memory.put(ITEM + 0x300, b'\0' * 16)
        self.memory.string(ITEM + 0x1B8, '')
        result = self.run_read()
        self.assertIsNone(result['rows'][0]['content_type'])
        self.assertEqual(result['rows'][0]['media_count'], 0)

    def test_empty_loaded_source(self):
        self.memory.put(CURRENT + 0x30, b'\0' * 24)
        self.assertEqual(self.run_read()['rows'], [])

    def test_not_opened_has_actionable_error(self):
        self.memory.put(SOURCE, b'\0' * 8)
        with self.assertRaisesRegex(reader.MomentsReadError, '打开朋友圈'):
            self.run_read()
        self.assertTrue(self.memory.closed)

    def test_account_change_before_scan_is_rejected(self):
        self.identity.return_value = 'wxid_other_offline'
        with patch.object(reader, '_locate') as locate:
            with self.assertRaisesRegex(reader.MomentsReadError, '账号已变化'):
                self.run_read()
            locate.assert_not_called()

    def test_account_change_after_scan_discards_rows(self):
        self.identity.side_effect = [ACCOUNT, 'wxid_other_offline']
        with self.assertRaisesRegex(reader.MomentsReadError, '账号已变化'):
            self.run_read()

    def test_cancelled_before_open(self):
        with self.assertRaisesRegex(reader.MomentsReadError, '已取消'):
            self.run_read(cancelled=lambda: True)
        self.target.assert_not_called()
        self.open_memory.assert_not_called()

    def test_cancelled_during_scan(self):
        state = {'cancelled': False}
        original = self.memory.regions
        def regions(budget):
            result = original(budget)
            state['cancelled'] = True
            return result
        self.memory.regions = regions
        with self.assertRaisesRegex(reader.MomentsReadError, '已取消'):
            self.run_read(cancelled=lambda: state['cancelled'])
        self.assertTrue(self.memory.closed)

    def test_deadline_is_checked_before_process_open(self):
        with patch.object(reader.time, 'monotonic', side_effect=[0, 9]):
            with self.assertRaisesRegex(reader.MomentsReadError, '超时'):
                self.run_read()
        self.target.assert_not_called()

    def test_scanned_byte_budget_is_enforced(self):
        with patch.object(reader, '_SCAN_BYTES', 1024):
            with self.assertRaisesRegex(reader.MomentsReadError, '限定缓存范围'):
                self.run_read()

    def test_invalid_pointer_is_fixed_message(self):
        self.memory.put(PAIRS, struct.pack('<QQ', 1, 2))
        with self.assertRaises(reader.MomentsReadError) as error:
            self.run_read()
        self.assertNotIn('fixture-private', str(error.exception))
        self.assertNotIn('invalid pointer', str(error.exception))

    def test_invalid_string_length(self):
        self.memory.put(ITEM + 0x1B8 + 16, struct.pack('<Q', 8193))
        with self.assertRaises(reader.MomentsReadError):
            self.run_read()

    def test_embedded_nul_is_rejected(self):
        self.memory.string(ITEM + 0x1B8, 'hello\0world')
        with self.assertRaises(reader.MomentsReadError):
            self.run_read()

    def test_non_utf8_is_rejected(self):
        self.memory.put(ITEM + 8, b'\xff' + b'\0' * 15 + struct.pack('<QQ', 1, 15))
        with self.assertRaises(reader.MomentsReadError):
            self.run_read()

    def test_non_person_author_is_rejected(self):
        self.memory.string(ITEM + 0x190, 'filehelper')
        with self.assertRaises(reader.MomentsReadError):
            self.run_read()

    def test_invalid_media_range(self):
        self.memory.put(CONTENT + 0x118, struct.pack('<QQ', 0x270000, 0x270001))
        with self.assertRaises(reader.MomentsReadError):
            self.run_read()

    def test_invalid_visibility(self):
        self.memory.put(ITEM + 0x1B0, b'\2')
        with self.assertRaises(reader.MomentsReadError):
            self.run_read()

    def test_invalid_source_mode_is_not_a_timeline(self):
        self.memory.put(SOURCE + 0x1D0, struct.pack('<I', 1))
        with self.assertRaisesRegex(reader.MomentsReadError, '打开朋友圈'):
            self.run_read()

    def test_invalid_secondary_vtable_is_not_a_timeline(self):
        self.memory.put(SOURCE + 0x10, struct.pack('<Q', 1))
        with self.assertRaisesRegex(reader.MomentsReadError, '打开朋友圈'):
            self.run_read()

    def test_retries_once_if_snapshot_changed(self):
        original = reader._decode
        calls = []
        def decode(*args):
            calls.append(True)
            if len(calls) == 1:
                raise reader._Changed()
            return original(*args)
        with patch.object(reader, '_decode', side_effect=decode):
            self.assertEqual(len(self.run_read()['rows']), 1)
        self.assertEqual(len(calls), 2)

    def test_keeps_no_partial_rows_when_snapshot_changes_twice(self):
        with patch.object(reader, '_decode', side_effect=reader._Changed()) as decode:
            with self.assertRaisesRegex(reader.MomentsReadError, '正在变化'):
                self.run_read()
        self.assertEqual(decode.call_count, 2)

    def test_pair_replacement_is_detected(self):
        count = 0
        def hook(address, size):
            nonlocal count
            if address == PAIRS and size == 16:
                count += 1
                if count % 2 == 0:
                    return struct.pack('<QQ', ITEM + 0x2000, ITEM + 0x3000)
        self.memory.read_hook = hook
        with self.assertRaisesRegex(reader.MomentsReadError, '正在变化'):
            self.run_read()

    def test_primitive_field_replacement_is_detected(self):
        count = 0
        def hook(address, size):
            nonlocal count
            if address == ITEM and size == 0x310:
                count += 1
                if count % 2 == 0:
                    raw = bytearray(self.memory.data[ITEM - SOURCE:ITEM - SOURCE + size])
                    raw[0x1B0] = 0
                    return bytes(raw)
        self.memory.read_hook = hook
        with self.assertRaisesRegex(reader.MomentsReadError, '正在变化'):
            self.run_read()

    def test_string_body_change_is_detected(self):
        header = self.memory.read(ITEM + 0x1B8, 32)
        pointer, _, length, _ = struct.unpack('<QQQQ', header)
        count = 0
        def hook(address, size):
            nonlocal count
            if address == pointer and size == length:
                count += 1
                if count % 2 == 0:
                    return b'x' * length
        self.memory.read_hook = hook
        with self.assertRaisesRegex(reader.MomentsReadError, '正在变化'):
            self.run_read()

    def test_source_current_vector_change_is_detected(self):
        candidate = reader._source(self.memory, SOURCE, CORE)
        self.memory.put(CURRENT + 0x30, struct.pack('<QQQ', PAIRS, PAIRS + 32, PAIRS + 32))
        with self.assertRaises(reader._Changed):
            reader._decode(self.memory, candidate, ACCOUNT, CORE, 1, reader._Budget(lambda: False))

    def test_only_committed_private_readwrite_regions_are_scanned(self):
        entries = iter([
            (0, 0x10000, 0x2000, 0x20000, 4),
            (0x10000, 0x1000, 0x1000, 0x20000, 4),
            (0x11000, 0x1000, 0x1000, 0x20000, 0x104),
            (0x12000, 0x1000, 0x1000, 0x20000, 2),
            (0x13000, 0x1000, 0x1000, 0x1000000, 4),
            (0x14000, 0x1000, 0x1000, 0x20000, 0x40),
        ])
        def query(handle, address, pointer, size):
            row = next(entries, None)
            if row is None:
                return 0
            region = pointer._obj
            region.base, region.size, region.state, region.kind, region.protect = row
            return size
        memory = object.__new__(NATIVE_MEMORY)
        memory.handle = 123
        memory.kernel = SimpleNamespace(VirtualQueryEx=query)
        self.assertEqual(memory.regions(reader._Budget(lambda: False)), [(0x10000, 0x1000)])

    def test_arguments_rejected_before_target_inspection(self):
        for kwargs in ({'limit': 0}, {'limit': 51}, {'limit': True}, {'cancelled': None}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.run_read(**kwargs)
        for pid in (0, -1, True, 2 ** 32):
            with self.subTest(pid=pid), self.assertRaises(ValueError):
                reader.read_loaded_moments(pid, ACCOUNT)
        self.target.assert_not_called()


class TargetTests(unittest.TestCase):
    def test_unknown_core_fingerprint_does_not_open_memory(self):
        with patch('native.moments_transport._native', return_value=SimpleNamespace(
                modules=lambda pid: {'weixin.dll': (CORE, 'C:/offline/Weixin.dll')})), \
                patch('native.moments_transport._core_profile', return_value=None), \
                patch.object(reader, '_Memory') as memory:
            with self.assertRaisesRegex(reader.MomentsReadError, '核心指纹'):
                reader.read_loaded_moments(123, ACCOUNT)
            memory.assert_not_called()

    def test_old_profile_is_rejected_even_if_fingerprint_is_known(self):
        with patch('native.moments_transport._native', return_value=SimpleNamespace(
                modules=lambda pid: {'weixin.dll': (CORE, 'C:/offline/Weixin.dll')})), \
                patch('native.moments_transport._core_profile', return_value=SimpleNamespace(version='4.1.15.13')):
            with self.assertRaisesRegex(reader.MomentsReadError, '4.1.15.56'):
                reader._target(123)

    def test_native_exception_text_is_not_exposed(self):
        with patch.object(reader, '_target', side_effect=OSError('private-account-and-file-path')):
            with self.assertRaises(reader.MomentsReadError) as error:
                reader.read_loaded_moments(123, ACCOUNT)
        self.assertNotIn('private-account', str(error.exception))

    def test_only_read_only_process_access_is_requested(self):
        kernel = Mock()
        kernel.OpenProcess.return_value = 1234
        with patch.object(reader.c, 'WinDLL', return_value=kernel, create=True):
            memory = reader._Memory(123)
        kernel.OpenProcess.assert_called_once_with(0x410, False, 123)
        memory.close()
        memory.close()
        kernel.CloseHandle.assert_called_once_with(1234)


if __name__ == '__main__':
    unittest.main()
