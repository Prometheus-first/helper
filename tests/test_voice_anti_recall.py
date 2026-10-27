"""原语音防撤回离线测试：封装样本与模拟发送，不连接微信。"""
from dataclasses import replace
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from plugins.anti_recall_voice import Plugin
from wxbot_framework.sdk import Message, PluginContext, ServiceFacade

ROOT = Path(__file__).resolve().parents[1]
ROOM, FRIEND = '12345678@chatroom', 'wxid_voice_recall_friend'
SILK = b'\x02#!SILK_V3' + b'\x03\0abc' * 50  # 仅封装样本，不用于播放。
IDENTIFIER = '18446744073709551001'


class Backend:
    session_id = 'current'
    def __init__(self):
        self.voices = []
        self.result = True
    def send_voice(self, chat, path):
        self.voices.append((chat, Path(path).read_bytes()))
        return self.result
    def stop(self):
        return {}


class VoiceRecallTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.runtime/tests'
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='voice-recall-', dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name).resolve() / 'received.silk'
        self.path.write_bytes(SILK)
        self.backend = Backend()
        self.service = ServiceFacade(text_delay={'enabled': False})
        self.service.bind(self.backend)
        self.addCleanup(self.service.close)
        self.ctx = PluginContext('anti_recall_voice', {}, self.service,
                                 {'voice.receive', 'voice.send', 'message.recall'})
        self.plugin = Plugin()
        self.plugin.on_load(self.ctx)
        self.addCleanup(self.plugin.on_unload, self.ctx)

    def original(self, *, chat=ROOM, session='current', identifier=IDENTIFIER, status='ready', **values):
        return Message('', chat, sender_id=FRIEND, type=34, is_group=chat.endswith('@chatroom'),
                       session_id=session, data={'native_message_id': identifier, 'voice_status': status,
                                                 'voice_path': str(self.path), 'voice_duration_ms': 1000, **values})

    def recall(self, *, chat=ROOM, session='current', identifier=IDENTIFIER):
        return Message('', chat, type=10002, is_group=chat.endswith('@chatroom'), session_id=session,
                       data={'monitor_only': True, 'recall': {'chat_id': chat, 'message_id': identifier}})

    def test_reposts_original_voice_once_without_filename_preparation(self):
        with patch('wxbot_framework.media_recall.prepare_file', side_effect=AssertionError('语音不走文件发送')):
            self.plugin.on_message(self.original(), self.ctx)
            self.plugin.on_message(self.recall(), self.ctx)
            self.plugin.on_message(self.recall(), self.ctx)
            self.plugin.on_message(self.original(), self.ctx)
        self.assertEqual(self.backend.voices, [(ROOM, SILK)])
        self.assertIsNone(self.ctx._llm)

    def test_recall_before_ready_waits_for_complete_audio(self):
        self.plugin.on_message(self.recall(), self.ctx)
        for status in ('pending', 'failed'):
            self.plugin.on_message(self.original(status=status), self.ctx)
        self.assertEqual(self.backend.voices, [])
        self.plugin.on_message(self.original(), self.ctx)
        self.assertEqual(self.backend.voices, [(ROOM, SILK)])

    def test_private_chat_and_message_identity_are_isolated(self):
        self.plugin.on_message(self.original(chat=FRIEND), self.ctx)
        self.plugin.on_message(self.recall(), self.ctx)
        self.plugin.on_message(self.recall(chat=FRIEND, identifier='7'), self.ctx)
        self.assertEqual(self.backend.voices, [])
        self.plugin.on_message(self.recall(chat=FRIEND), self.ctx)
        self.assertEqual(self.backend.voices, [(FRIEND, SILK)])

    def test_old_connection_and_bot_echo_do_not_send(self):
        self.plugin.on_message(self.original(bot_generated=True), self.ctx)
        self.plugin.on_message(self.recall(), self.ctx)
        self.plugin.on_message(self.original(session='old', identifier='2'), self.ctx)
        self.plugin.on_message(self.recall(identifier='2'), self.ctx)
        self.plugin.on_message(self.recall(session='old', identifier='2'), self.ctx)
        self.assertEqual(self.backend.voices, [])

    def test_changed_or_missing_cache_is_not_replaced(self):
        self.plugin.on_message(self.original(), self.ctx)
        before = self.path.stat()
        self.path.write_bytes(SILK + b'changed')
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000))
        self.plugin.on_message(self.recall(), self.ctx)
        self.assertEqual(self.backend.voices, [])

    def test_voice_duration_and_format_must_match(self):
        for identifier, duration in (('1', 2000), ('2', True)):
            self.plugin.on_message(self.original(identifier=identifier, voice_duration_ms=duration), self.ctx)
            self.plugin.on_message(self.recall(identifier=identifier), self.ctx)
        self.path.write_bytes(b'not-a-voice')
        self.plugin.on_message(self.original(identifier='3'), self.ctx)
        self.plugin.on_message(self.recall(identifier='3'), self.ctx)
        self.assertEqual(self.backend.voices, [])

    def test_session_is_checked_again_when_sending(self):
        self.plugin.on_message(self.original(), self.ctx)
        self.backend.session_id = 'new'
        self.plugin.on_message(self.recall(), self.ctx)
        self.assertEqual(self.backend.voices, [])

    def test_uncertain_send_is_not_retried(self):
        self.backend.result = False
        self.plugin.on_message(self.original(), self.ctx)
        self.plugin.on_message(self.recall(), self.ctx)
        self.plugin.on_message(self.recall(), self.ctx)
        self.assertEqual(self.backend.voices, [(ROOM, SILK)])

    def test_reload_drops_indexes_but_keeps_shared_cache(self):
        self.plugin.on_message(self.original(), self.ctx)
        self.plugin.on_unload(self.ctx)
        self.plugin.on_load(self.ctx)
        self.plugin.on_message(self.recall(), self.ctx)
        self.assertTrue(self.path.exists())
        self.assertEqual(self.backend.voices, [])

    def test_other_media_and_forged_chat_notices_do_not_match(self):
        self.plugin.on_message(replace(self.original(), type=1), self.ctx)
        self.plugin.on_message(self.recall(), self.ctx)
        self.plugin.on_message(self.original(identifier='2'), self.ctx)
        malformed = replace(self.recall(identifier='2'), data={'recall': {'chat_id': FRIEND, 'message_id': '2'}})
        self.plugin.on_message(malformed, self.ctx)
        self.assertEqual(self.backend.voices, [])

    def test_manifest_defaults_and_editable_business_function(self):
        manifest = json.loads((ROOT / 'plugins/anti_recall_voice/plugin.json').read_text(encoding='utf-8'))
        self.assertTrue(manifest['default_enabled'])
        self.assertEqual(set(manifest['requires']), {'voice.receive', 'voice.send', 'message.recall'})
        self.assertEqual(manifest['business'][0]['function'], 'Plugin.send_recalled')


if __name__ == '__main__':
    unittest.main()
