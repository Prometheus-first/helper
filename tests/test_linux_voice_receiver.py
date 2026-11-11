"""Linux 语音离线缓存；合成 SILK 仅测试封装，不用于播放。"""
from dataclasses import FrozenInstanceError, replace
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from plugins.anti_recall_voice import Plugin
from wxbot_framework.linux_voice_receiver import LinuxVoicePayload, LinuxVoiceReceiver
from wxbot_framework.sdk import Message, PluginContext, ServiceFacade
from wxbot_framework.voices import TENCENT_SILK_HEADER, STANDARD_SILK_HEADER

ACCOUNT, PEER, ROOM, SESSION = 'wxid_voice_robot', 'wxid_voice_peer', '12345678@chatroom', 'voice-session'
SILK = TENCENT_SILK_HEADER + b'\x03\0abc' * 50


def xml(body=SILK, duration=1000, **attrs):
    values = dict(voicelength=str(duration), length=str(len(body)), endflag='1',
                  cancelflag='0', aeskey='private-key-fixture', clientmsgid='private-client-fixture')
    values.update(attrs)
    return '<msg><voicemsg ' + ' '.join(f'{k}="{v}"' for k, v in values.items()) + '/></msg>'


def original(identifier='81', *, body=SILK, metadata=None, **options):
    data = dict(_linux_voice=LinuxVoicePayload(xml(body) if metadata is None else metadata, body),
                native_message_id=identifier, self_wxid=ACCOUNT, raw_content='private-raw-fixture',
                bytearray=bytearray(b'private-raw-fixture'), sender_role='unknown')
    data.update(options.pop('data', {}))
    values = dict(text='private-raw-fixture', chat_id=ROOM, sender_id=PEER, type=34,
                  is_group=True, session_id=SESSION, data=data)
    values.update(options)
    return Message(**values)


@unittest.skipUnless(os.name == 'posix', 'Linux 私有缓存语义')
class LinuxVoiceReceiverTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.current, self.rows = True, []

    def receiver(self, **options):
        receiver = LinuxVoiceReceiver(account=ACCOUNT, session_id=SESSION,
            cache_root=self.root / 'cache', is_current=lambda: self.current,
            callback=self.rows.append, **options)
        self.addCleanup(receiver.close)
        return receiver

    def wait(self, condition):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            if condition():
                return
            time.sleep(.01)
        self.fail('offline voice worker did not finish')

    def assert_clean(self, message):
        visible = {key: value for key, value in message.data.items()
                   if key not in ('voice_xml', 'raw_content')}
        public = repr(message) + json.dumps(visible)
        for token in ('_linux_voice', 'private-key', 'private-client', 'private-raw', 'bytearray', 'raw_content'):
            self.assertNotIn(token, public)
        if message.data.get('voice_xml'):
            self.assertEqual(message.data['raw_content'], message.data['voice_xml'])
        self.assertEqual(message.text, '')
        self.assertTrue(message.data['monitor_only'])
        self.assertTrue(message.data['media_cache_only'])
        self.assertFalse(message.data['dispatch_to_plugins'])

    def test_payload_is_immutable_and_repr_hides_xml_and_audio(self):
        payload = LinuxVoicePayload(xml(), SILK)
        self.assertEqual(repr(payload), 'LinuxVoicePayload()')
        with self.assertRaises(FrozenInstanceError):
            payload.body = b'changed'

    def test_database_fallback_waits_for_exact_message_then_validates_audio(self):
        class Source:
            closed = False
            calls = []
            def lookup(self, chat, identifier):
                self.calls.append((chat, identifier))
                return SILK if len(self.calls) == 2 else None
            def close(self):
                self.closed = True
        source = Source()
        receiver = self.receiver(source=source)
        result = receiver.submit(original(body=b'', metadata=xml()))
        self.assertEqual(result.data['voice_status'], 'pending')
        self.wait(lambda: bool(self.rows))
        self.assertEqual(source.calls, [(ROOM, '81')] * 2)
        self.assertEqual(self.rows[0].data['voice_status'], 'ready')
        self.assertEqual(Path(self.rows[0].data['voice_path']).read_bytes(), SILK)
        receiver.close()
        self.assertTrue(source.closed)

    def test_database_audio_still_requires_declared_metadata_match(self):
        class Source:
            def lookup(self, *key): return SILK
            def close(self): pass
        receiver = self.receiver(source=Source())
        receiver.submit(original(body=b'', metadata=xml(length='123')))
        self.wait(lambda: bool(self.rows))
        self.assertEqual(self.rows[0].data['voice_status'], 'failed')
        self.assert_clean(self.rows[0])

    def test_database_wait_cancels_without_late_delivery(self):
        called = threading.Event()
        class Source:
            closed = False
            def lookup(self, *key):
                called.set()
                return None
            def close(self): self.closed = True
        source = Source()
        receiver = self.receiver(source=source)
        receiver.submit(original(body=b'', metadata=xml()))
        self.assertTrue(called.wait(1))
        receiver.close()
        self.assertTrue(source.closed)
        self.assertEqual(self.rows, [])

    def test_ready_is_private_exact_audio_and_close_removes_owned_files(self):
        receiver = self.receiver()
        pending = receiver.submit(original())
        self.assertEqual(pending.data['voice_status'], 'pending')
        self.assert_clean(pending)
        self.wait(lambda: len(self.rows) == 1)
        ready = self.rows[0]
        self.assert_clean(ready)
        path = Path(ready.data['voice_path'])
        self.assertEqual(path.read_bytes(), SILK)
        self.assertEqual((ready.data['voice_duration_ms'], ready.data['voice_size']), (1000, len(SILK)))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        receiver.close()
        self.assertFalse(receiver.is_alive)
        self.assertFalse(path.exists())

    def test_complete_xml_is_preserved_on_ready_and_without_inline_audio(self):
        receiver = self.receiver()
        metadata = xml(voiceurl='fixture-cdn-token', voiceformat='4', extra='preserved &amp; exact')
        pending = receiver.submit(original(metadata=metadata))
        self.assertEqual(pending.data['voice_xml'], metadata)
        self.wait(lambda: bool(self.rows))
        self.assertEqual(self.rows[0].data['voice_xml'], metadata)
        self.assertEqual(self.rows[0].data['raw_content'], metadata)
        unavailable = receiver.submit(original(identifier='82', metadata=metadata, body=b''))
        self.assertEqual(unavailable.data['voice_status'], 'unavailable')
        self.assertEqual(unavailable.data['voice_xml'], metadata)
        self.assertEqual(unavailable.data['raw_content'], metadata)

    def test_standard_silk_normalizes_and_own_group_sync_can_be_cached(self):
        receiver = self.receiver(allowed_targets=[ROOM])
        source = STANDARD_SILK_HEADER + b'\x03\0abc' * 50 + b'\xff\xff'
        receiver.submit(original(body=source, metadata=xml(source, duration=1020),
            is_self=True, sender_id=ACCOUNT))
        self.wait(lambda: bool(self.rows))
        self.assertEqual(Path(self.rows[0].data['voice_path']).read_bytes(), SILK)

    def test_wrong_account_session_scope_and_bot_echo_are_rejected(self):
        receiver = self.receiver(allowed_targets=[ROOM])
        cases = [original(session_id='old'), original(data={'self_wxid': 'wxid_other'}),
                 original(chat_id=PEER, is_group=False), original(data={'bot_generated': True})]
        for message in cases:
            result = receiver.submit(message)
            self.assertEqual(result.data['voice_status'], 'unavailable')
            self.assert_clean(result)
        self.current = False
        self.assertEqual(receiver.submit(original()).data['voice_status'], 'unavailable')
        self.assertEqual(self.rows, [])

    def test_malformed_inputs_and_unsupported_audio_never_leak_payload(self):
        receiver = self.receiver()
        immediate = [original(body=bytearray(SILK)), original(body=b'x' * 262145),
                     original(metadata='x' * 16385), original(identifier='0'),
                     original(metadata='wxid_other:\n' + xml()),
                     original(data={'_linux_voice': object()})]
        for message in immediate:
            result = receiver.submit(message)
            self.assertEqual(result.data['voice_status'], 'failed')
            self.assert_clean(result)
        invalid = [(SILK[:-1], xml(SILK[:-1])), (b'#!AMR\n' + b'x' * 30, xml()),
                   (SILK, xml(duration=1021)), (SILK, xml(length='999')),
                   (SILK, xml(endflag='0')), (SILK, xml(cancelflag='1')),
                   (SILK, '<!DOCTYPE msg [<!ENTITY x "private-key">]>' + xml()),
                   (SILK, xml(duration=60001)),
                   (STANDARD_SILK_HEADER + b'\x03\0abc' * 50, xml())]
        for index, (body, metadata) in enumerate(invalid):
            receiver.submit(original(str(index + 100), body=body, metadata=metadata))
        self.wait(lambda: len(self.rows) == len(invalid))
        for result in self.rows:
            self.assertEqual(result.data['voice_status'], 'failed')
            self.assert_clean(result)
        self.assertEqual(list(self.root.rglob('*.silk')), [])

    def test_exact_group_author_prefix_is_accepted(self):
        receiver = self.receiver()
        receiver.submit(original(metadata=PEER + ':\n' + xml()))
        self.wait(lambda: bool(self.rows))
        self.assertEqual(self.rows[0].data['voice_status'], 'ready')

    def test_missing_audio_is_unavailable_and_preserves_valid_xml(self):
        receiver = self.receiver()
        for message in (original(body=b''), original(data={'_linux_voice': None})):
            result = receiver.submit(message)
            self.assertEqual(result.data['voice_status'], 'unavailable')
            self.assert_clean(result)
        self.assertEqual(self.rows, [])

    def test_duplicates_do_not_save_or_callback_twice_and_quota_evicts_only_owned(self):
        receiver = self.receiver(max_files=1)
        unrelated = self.root / 'cache' / 'keep.silk'
        unrelated.write_bytes(b'not-owned')
        receiver.submit(original())
        self.wait(lambda: len(self.rows) == 1)
        first = Path(self.rows[0].data['voice_path'])
        self.assertEqual(receiver.submit(original()).data['voice_status'], 'ready')
        self.assertEqual(len(self.rows), 1)
        receiver.submit(original('82'))
        self.wait(lambda: len(self.rows) == 2)
        self.assertFalse(first.exists())
        self.assertEqual(receiver.submit(original()).data['voice_status'], 'unavailable')
        receiver.close()
        self.assertEqual(unrelated.read_bytes(), b'not-owned')

    def test_byte_budget_and_queue_budget_are_enforced(self):
        receiver = self.receiver(max_cache_bytes=len(SILK) - 1)
        self.assertEqual(receiver.submit(original()).data['voice_status'], 'failed')
        receiver.close()
        receiver = self.receiver(queue_size=1)
        entered, resume = threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            resume.wait(3)
        with patch.object(receiver, '_process', side_effect=blocked):
            receiver.submit(original())
            self.assertTrue(entered.wait(2))
            try:
                self.assertEqual(receiver.submit(original('82')).data['voice_status'], 'pending')
                failed = receiver.submit(original('83'))
                self.assertEqual(failed.data['voice_status'], 'failed')
                self.assert_clean(failed)
            finally:
                resume.set()
                receiver.close()

    def test_stale_during_processing_and_close_suppress_callbacks(self):
        receiver = self.receiver()
        entered, resume = threading.Event(), threading.Event()
        from wxbot_framework.voice_receiver import _voice_data
        def blocked(*args):
            entered.set()
            resume.wait(3)
            return _voice_data(*args)
        with patch('wxbot_framework.linux_voice_receiver._voice_data', side_effect=blocked):
            receiver.submit(original())
            self.assertTrue(entered.wait(2))
            self.current = False
            receiver.close(timeout=0)
            resume.set()
            receiver.close()
        self.assertEqual(self.rows, [])
        self.assertEqual(list(self.root.rglob('*.silk')), [])

    def test_cache_error_is_fixed_message_and_root_links_or_public_permissions_reject(self):
        receiver = self.receiver()
        with patch.object(receiver, '_save', side_effect=OSError('private-path-and-key')):
            receiver.submit(original())
            self.wait(lambda: bool(self.rows))
        self.assertEqual(self.rows[0].data['voice_status'], 'failed')
        self.assertNotIn('private-path-and-key', repr(self.rows))
        self.assert_clean(self.rows[0])
        receiver.close()
        (self.root / 'cache').chmod(0o755)
        with self.assertRaises(ValueError):
            self.receiver()
        target = self.root / 'private'
        target.mkdir(mode=0o700)
        link = self.root / 'linked'
        link.symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            LinuxVoiceReceiver(account=ACCOUNT, session_id=SESSION, cache_root=link, is_current=lambda: True)

    def test_ready_recall_before_completion_reposts_audio_once_through_existing_plugin(self):
        class Backend:
            session_id = SESSION
            def __init__(self): self.sent = []
            def send_voice(self, chat, path):
                self.sent.append((chat, Path(path).read_bytes()))
                return True
            def stop(self): return {}
        backend = Backend()
        service = ServiceFacade(text_delay={'enabled': False})
        service.bind(backend)
        self.addCleanup(service.close)
        ctx = PluginContext('anti_recall_voice', {}, service, {'voice.send', 'voice.receive', 'message.recall'})
        plugin = Plugin()
        plugin.on_load(ctx)
        self.addCleanup(plugin.on_unload, ctx)
        recall = Message('', ROOM, type=10002, is_group=True, session_id=SESSION,
            data={'recall': {'chat_id': ROOM, 'message_id': '81'}})
        plugin.on_message(recall, ctx)
        receiver = self.receiver()
        pending = receiver.submit(original())
        plugin.on_message(pending, ctx)
        self.wait(lambda: bool(self.rows))
        plugin.on_message(self.rows[0], ctx)
        plugin.on_message(recall, ctx)
        plugin.on_message(self.rows[0], ctx)
        self.assertEqual(backend.sent, [(ROOM, SILK)])

    def test_failed_disk_flush_removes_partial_owned_file(self):
        receiver = self.receiver()
        with patch('wxbot_framework.linux_voice_receiver.os.fsync',
                   side_effect=OSError('private-path-and-key')):
            receiver.submit(original())
            self.wait(lambda: bool(self.rows))
        self.assertEqual(self.rows[0].data['voice_status'], 'failed')
        self.assertNotIn('private-path-and-key', repr(self.rows))
        self.assertEqual(list(self.root.rglob('*.silk')), [])

    def test_non_voice_is_unchanged_and_callback_can_close_without_deadlock(self):
        receiver = self.receiver()
        plain = Message('hello', PEER, session_id=SESSION)
        self.assertIs(receiver.submit(plain), plain)
        finished = threading.Event()
        def callback(message):
            receiver.close()
            finished.set()
        receiver.set_callback(callback)
        receiver.submit(original())
        self.assertTrue(finished.wait(2))
        receiver.close()
        self.assertFalse(receiver.is_alive)
        self.assertEqual(list(self.root.rglob('*.silk')), [])


if __name__ == '__main__':
    unittest.main()
