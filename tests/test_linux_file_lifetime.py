"""The accepted native send may read its named source after Python returns."""
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import patch

from wxbot_framework.files import prepare_file
from wxbot_framework.linux_sender import LinuxNativeSender, LinuxSendError, LinuxSendIndeterminate


@unittest.skipUnless(os.name == 'posix' and hasattr(socket, 'SO_PEERCRED'), 'requires Linux')
class LinuxFileLifetimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='helper-file-lifetime-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.sender = LinuxNativeSender(directory=self.root, allowed_targets=('123@chatroom',))
        self.sender.bind_pid(os.getpid())
        self.sender.features = frozenset({'local_media'})
        self.sender._context_ready = True
        def cleanup():
            with patch('wxbot_framework.linux_sender.os.kill', side_effect=ProcessLookupError):
                self.sender.close()
        self.addCleanup(cleanup)
        self.source = self.root / '中文测试文件.md'
        self.source.write_bytes('原始内容\n'.encode())

    @staticmethod
    def response():
        return {'callback': True, 'result': json.dumps({'success': True})}

    def test_late_native_read_survives_plugin_named_snapshot_cleanup(self):
        submitted = []
        def accept(command, body=b''):
            self.assertEqual(command, b'F')
            target, encoded = body.split(b'\n')
            self.assertEqual(target, b'123@chatroom')
            path = Path(os.fsdecode(bytes.fromhex(encoded.decode('ascii'))))
            submitted.append(path)
            self.assertEqual(path.name, self.source.name)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
            return self.response()
        with patch.object(self.sender, '_request', accept):
            with prepare_file(self.source, self.root / 'prepared') as file:
                transient = file.path
                self.assertTrue(self.sender.send_file('123@chatroom', transient))
            self.assertFalse(transient.exists())
        read = []
        worker = threading.Thread(target=lambda: read.append(submitted[0].read_bytes()))
        worker.start()
        worker.join(timeout=2)
        self.assertEqual(read, [self.source.read_bytes()])

    def test_quota_rejects_without_evicting_inflight_files(self):
        with patch('wxbot_framework.linux_sender.MAX_RETAINED_FILE_COUNT', 2), \
                patch.object(self.sender, '_request', return_value=self.response()) as send:
            self.sender.send_file('123@chatroom', self.source)
            self.sender.send_file('123@chatroom', self.source)
            retained = tuple(self.sender._retained_files)
            with self.assertRaisesRegex(LinuxSendError, '上限'):
                self.sender.send_file('123@chatroom', self.source)
            self.assertEqual(send.call_count, 2)
            self.assertTrue(all(path.is_file() for path in retained))

    def test_byte_budget_rejects_before_native_send(self):
        with patch('wxbot_framework.linux_sender.MAX_RETAINED_FILE_BYTES', self.source.stat().st_size), \
                patch.object(self.sender, '_request', return_value=self.response()) as send:
            self.sender.send_file('123@chatroom', self.source)
            with self.assertRaisesRegex(LinuxSendError, '总预算'):
                self.sender.send_file('123@chatroom', self.source)
            self.assertEqual(send.call_count, 1)

    def test_unknown_send_and_failed_native_stage_both_retain_input(self):
        with patch.object(self.sender, '_request', return_value={'callback': False, 'pending': True}):
            with self.assertRaises(LinuxSendIndeterminate):
                self.sender.send_file('123@chatroom', self.source)
        pending = tuple(self.sender._retained_files)
        self.assertEqual(len(pending), 1)
        with patch.object(self.sender, '_request', return_value={'callback': True, 'error': 'native rejected'}):
            with self.assertRaisesRegex(LinuxSendError, 'native rejected'):
                self.sender.send_file('123@chatroom', self.source)
        self.assertEqual(len(self.sender._retained_files), 2)
        self.assertTrue(pending[0].exists())
        self.assertEqual(len(list(self.sender._directory.glob('file-*/*'))), 2)

    def test_close_does_not_remove_input_until_native_process_is_gone(self):
        with patch.object(self.sender, '_request', return_value=self.response()):
            self.sender.send_file('123@chatroom', self.source)
        path = next(iter(self.sender._retained_files))
        self.sender.close()
        self.assertTrue(path.exists())
        self.assertFalse(self.sender.cleanup_retained_files())
        with patch('wxbot_framework.linux_sender.os.kill', side_effect=ProcessLookupError):
            self.assertTrue(self.sender.cleanup_retained_files())
        self.assertFalse(path.exists())
        self.assertEqual(self.sender._retained_file_bytes, 0)

    def test_unauthorized_target_never_copies_or_sends(self):
        with patch.object(self.sender, '_request') as send:
            with self.assertRaises(LinuxSendError):
                self.sender.send_file('456@chatroom', self.source)
        send.assert_not_called()
        self.assertEqual(list(self.sender._directory.glob('file-*')), [])

    def test_preflight_failure_before_submission_cleans_snapshot(self):
        self.sender._context_ready = False
        with patch.object(self.sender, 'ensure_context', side_effect=LinuxSendError('no coroutine')), \
                patch.object(self.sender, '_request') as send:
            with self.assertRaisesRegex(LinuxSendError, 'no coroutine'):
                self.sender.send_file('123@chatroom', self.source)
        send.assert_not_called()
        self.assertEqual(self.sender._retained_files, {})
        self.assertEqual(list(self.sender._directory.glob('file-*')), [])
