"""完整语音缓存离线测试：SILK 仅为封装样本，不是可播放录音。"""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from wxbot_framework.voice_receiver import VoiceReceiver, MAX_RECEIVED_VOICE_BYTES
from wxbot_framework.voices import TENCENT_SILK_HEADER, STANDARD_SILK_HEADER

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = 'wxid_voice_receiver_robot'
CHAT = '12345678@chatroom'
SILK = TENCENT_SILK_HEADER + b'\x03\0abc' * 50


def xml(payload=SILK, *, duration='1000', **attributes):
    values = {'voicelength': duration, 'length': str(len(payload)), 'endflag': '1',
              'cancelflag': '0', 'clientmsgid': 'private-client-fixture',
              'aeskey': 'private-credential-fixture', **attributes}
    return '<msg><voicemsg ' + ' '.join(f'{key}="{value}"' for key, value in values.items()) + '/></msg>'


def event(identifier='18446744073709551001', **values):
    return {'wx_type': 34, 'chat_id': CHAT, 'room_wxid': CHAT, 'self_wxid': ACCOUNT,
            'sender_id': 'wxid_voice_receiver_friend', 'native_message_id': identifier,
            'raw_content': xml(), 'msg': xml(), 'content': xml(), 'image_xml': xml(),
            'voice_payload': b'not-a-public-field', **values}


class VoiceReceiverTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.runtime/tests'
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='voice-receiver-', dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.directory = self.root / 'received'
        self.current = True
        self.events = []

    def receiver(self, callback=None, **options):
        receiver = VoiceReceiver(self.directory, ACCOUNT, callback or self.events.append,
                                 lambda: self.current, **options)
        self.addCleanup(receiver.close)
        return receiver

    def wait(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail('离线语音队列未及时完成')

    def saved(self):
        return list(self.directory.rglob('*.silk')) if self.directory.exists() else []

    def test_ready_caches_identical_tencent_bytes_and_strips_private_metadata(self):
        receiver = self.receiver()
        self.assertTrue(receiver.submit(event(), SILK))
        self.wait(lambda: len(self.events) == 1)
        row = self.events[0]
        self.assertEqual(row['voice_status'], 'ready')
        path = Path(row['voice_path'])
        self.assertEqual(path.parent, self.directory / ACCOUNT)
        self.assertEqual(len(path.stem), 32)
        self.assertEqual(path.read_bytes(), SILK)
        self.assertEqual((row['voice_duration_ms'], row['voice_size']), (1000, len(SILK)))
        public = json.dumps(row)
        for private in ('raw_content', 'aeskey', 'private-credential', 'private-client', 'voice_payload', 'image_xml'):
            self.assertNotIn(private, public)

    def test_standard_silk_requires_tail_and_is_normalized_without_encoding(self):
        receiver = self.receiver()
        source = STANDARD_SILK_HEADER + b'\x03\0abc' * 50 + b'\xff\xff'
        self.assertTrue(receiver.submit(event(raw_content=xml(source)), source))
        self.wait(lambda: len(self.events) == 1)
        self.assertEqual(self.events[0]['voice_status'], 'ready')
        self.assertEqual(Path(self.events[0]['voice_path']).read_bytes(), SILK)

    def test_group_sender_prefix_is_removed_only_for_xml_validation(self):
        receiver = self.receiver()
        receiver.submit(event(raw_content='wxid_voice_receiver_friend:\n' + xml()), SILK)
        self.wait(lambda: len(self.events) == 1)
        self.assertEqual(self.events[0]['voice_status'], 'ready')

    def test_partial_wrong_duration_wrong_size_or_unsupported_payload_never_cache(self):
        receiver = self.receiver()
        cases = [
            (SILK[:-1], xml(SILK[:-1])),
            (b'#!AMR\n' + b'x' * 100, xml(b'#!AMR\n' + b'x' * 100)),
            (STANDARD_SILK_HEADER + b'\x03\0abc' * 50, xml(STANDARD_SILK_HEADER + b'\x03\0abc' * 50)),
            (SILK, xml(length=str(len(SILK) + 1))),
            (SILK, xml(duration='2500')),
            (SILK, xml(duration='1021')),
            (SILK, xml(endflag='0')),
            (SILK, xml(cancelflag='1')),
            (SILK, '<msg><voicemsg length="260"/></msg>'),
            (SILK, '<!DOCTYPE x [<!ENTITY y "private-fixture">]>' + xml()),
            (SILK, '<msg><voicemsg/><voicemsg/></msg>'),
        ]
        for index, (payload, metadata) in enumerate(cases):
            with self.subTest(index=index):
                receiver.submit(event(str(index + 1), raw_content=metadata), payload)
        self.wait(lambda: len(self.events) == len(cases))
        self.assertTrue(all(row['voice_status'] == 'failed' and not row['voice_path'] for row in self.events))
        self.assertEqual(self.saved(), [])
        self.assertNotIn('private-fixture', json.dumps(self.events))

    def test_invalid_ids_accounts_types_and_oversized_input_fail_without_writes(self):
        receiver = self.receiver()
        cases = [(event(native_message_id='0'), SILK),
                 (event(self_wxid='wxid_other_account'), SILK),
                 (event(wx_type=3), SILK), (event(), bytearray(SILK)),
                 (event(), b''), (event(), b'x' * (MAX_RECEIVED_VOICE_BYTES + 1)),
                 (event(raw_content='x' * 16385), SILK)]
        for value, payload in cases:
            self.assertFalse(receiver.submit(value, payload))
        self.assertEqual(len(self.events), len(cases))
        self.assertEqual(self.saved(), [])

    def test_one_frame_recording_timer_difference_is_accepted(self):
        receiver = self.receiver()
        for index, duration in enumerate(('993', '980', '1020')):
            receiver.submit(event(str(index + 1), raw_content=xml(duration=duration)), SILK)
        self.wait(lambda: len(self.events) == 3)
        self.assertEqual([row['voice_duration_ms'] for row in self.events], [1000, 1000, 1000])

    def test_duplicate_message_is_saved_once_and_chats_are_isolated(self):
        receiver = self.receiver()
        self.assertTrue(receiver.submit(event(), SILK))
        self.assertTrue(receiver.submit(event(), SILK))
        self.assertTrue(receiver.submit(event(chat_id='wxid_private_chat', room_wxid=''), SILK))
        self.wait(lambda: len(self.events) == 2)
        self.assertEqual(len(self.saved()), 2)

    def test_inactive_session_never_delivers_or_creates_directories(self):
        receiver = self.receiver()
        self.current = False
        self.assertFalse(receiver.submit(event(), SILK))
        self.assertFalse(self.directory.exists())
        self.assertEqual(self.events, [])

    def test_session_change_after_save_removes_only_its_new_file(self):
        receiver = self.receiver()
        account_dir = self.directory / ACCOUNT
        account_dir.mkdir(parents=True)
        preserved = account_dir / 'other-feature.silk'
        preserved.write_bytes(SILK)
        original = receiver._save
        def save(body):
            result = original(body)
            self.current = False
            return result
        with patch.object(receiver, '_save', side_effect=save):
            receiver.submit(event(), SILK)
            self.wait(lambda: receiver._queue.unfinished_tasks == 0)
        self.assertEqual(self.events, [])
        self.assertEqual(self.saved(), [preserved])

    def test_callback_failure_is_sanitized_and_does_not_kill_worker(self):
        calls = []
        def callback(value):
            calls.append(value)
            raise RuntimeError('private-path-and-credential')
        receiver = self.receiver(callback)
        receiver.submit(event('1'), SILK)
        receiver.submit(event('2'), SILK)
        self.wait(lambda: len(calls) == 2 and receiver._queue.unfinished_tasks == 0)
        self.assertTrue(receiver.is_alive)
        self.assertEqual(self.saved(), [])

    def test_queue_full_is_observable_and_close_cancels_pending_work(self):
        receiver = self.receiver(queue_size=1)
        entered, release = threading.Event(), threading.Event()
        original = receiver._process
        def process(*values):
            entered.set()
            release.wait(3)
            original(*values)
        try:
            with patch.object(receiver, '_process', side_effect=process):
                self.assertTrue(receiver.submit(event('1'), SILK))
                self.assertTrue(entered.wait(2))
                self.assertTrue(receiver.submit(event('2'), SILK))
                self.assertFalse(receiver.submit(event('3'), SILK))
                receiver.close(timeout=0)
                self.assertTrue(receiver.is_alive)
                release.set()
                receiver.close()
        finally:
            release.set()
        self.assertFalse(receiver.is_alive)
        self.assertEqual([row['voice_error'] for row in self.events], ['语音接收队列已满'])
        self.assertEqual(self.saved(), [])

    def test_configuration_and_directory_validation_are_explicit(self):
        for account in ('.', '..', CHAT):
            with self.subTest(account=account), self.assertRaises(ValueError):
                VoiceReceiver(self.directory, account, self.events.append, lambda: True)
        for size in (True, 0, 1025):
            with self.assertRaises(ValueError):
                self.receiver(queue_size=size)
        with self.assertRaises(ValueError):
            VoiceReceiver(Path('relative-cache'), ACCOUNT, self.events.append, lambda: True)
        receiver = self.receiver()
        for timeout in (True, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                receiver.close(timeout)

    def test_reparse_or_symlink_cache_directory_is_rejected(self):
        original = Path.is_symlink
        def linked(path):
            return path == self.directory or original(path)
        with patch.object(Path, 'is_symlink', linked), self.assertRaises(ValueError):
            self.receiver()

    def test_uuid_collision_never_removes_an_existing_file(self):
        receiver = self.receiver()
        account_dir = self.directory / ACCOUNT
        account_dir.mkdir(parents=True)
        existing = account_dir / ('a' * 32 + '.silk')
        existing.write_bytes(b'preexisting-file')
        class Token:
            hex = 'a' * 32
        with patch('wxbot_framework.voice_receiver.uuid.uuid4', return_value=Token()):
            receiver.submit(event(), SILK)
            self.wait(lambda: len(self.events) == 1)
        self.assertEqual(self.events[0]['voice_status'], 'failed')
        self.assertEqual(existing.read_bytes(), b'preexisting-file')


if __name__ == '__main__':
    unittest.main()
