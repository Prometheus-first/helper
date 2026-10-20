"""按群音色、取消竞态、合成回退和临时文件生命周期。"""
import json
from pathlib import Path
from unittest.mock import Mock, patch
import unittest

import test_chat_summary as daily
from test_tts import wav
from plugins.voice_reply import Plugin
from plugins.mention_chat import Plugin as ChatPlugin
from wxbot_framework.members import SnapshotExpired
from wxbot_framework.speech import SpeechController, deliver_model_reply, speech_chunks
from wxbot_framework.text import own_group_command
from wxbot_framework.tts import TTSError, Voice, VoicePage

ROOM, OTHER_ROOM, CAT = daily.ROOM, daily.OTHER_ROOM, daily.CAT
FIRST, SECOND = Voice('a' * 32, '测试甲'), Voice('b' * 32, '测试乙')


class SpeechTests(unittest.TestCase):
    setUp = daily.DailySummaryTests.setUp
    tearDown = daily.DailySummaryTests.tearDown
    new_store = daily.DailySummaryTests.new_store
    message = daily.DailySummaryTests.message
    model = daily.DailySummaryTests.model

    def setup_speech(self):
        self.active = True
        self.speech = SpeechController(self.root / 'tts.json', self.root / 'tts', self.store, lambda: self.active)
        self.client = Mock(default_voice=FIRST.name, max_chars_per_clip=20)
        self.client.voices.return_value = (FIRST, SECOND)
        self.client.public_voices.return_value = VoicePage((), 1, 10, False)
        self.client.synthesize.return_value = wav()
        with patch('wxbot_framework.speech.FishTTS.from_file', return_value=self.client):
            self.speech.reload()
        self.ctx._speech = self.speech
        self.voice_paths = []

        def send_voice(room, path):
            self.assertTrue(Path(path).exists())
            self.voice_paths.append(Path(path))
            self.backend.sent.append((room, 'voice'))
            return True

        self.backend.send_voice = send_voice
        self.backend.send_at_text = lambda room, text, ids: self.backend.sent.append((room, text, ids)) or True

    def test_selection_persists_and_isolated_by_group_and_account(self):
        self.setup_speech()
        chosen = self.speech.select(ROOM, SECOND.name)
        self.assertEqual(self.new_store().voice_selection(ROOM), chosen)
        self.assertEqual(self.speech.selected(OTHER_ROOM)[1], FIRST)
        self.assertEqual(self.new_store('wxid_other_robot').voice_selection(ROOM).voice_id, '')
        self.store.clear(ROOM)
        self.assertEqual(self.store.voice_selection(ROOM), chosen)
        self.speech.select(ROOM, '关闭')
        deliver_model_reply(self.ctx, self.message('提问'), '文字回答')
        self.client.synthesize.assert_not_called()
        self.assertEqual(self.backend.sent[-1][1], '文字回答')

    def test_success_sends_only_voice_and_removes_temporary_audio(self):
        self.setup_speech()
        deliver_model_reply(self.ctx, self.message('提问'), '这是模型生成的回答。', display_name='测试成员')
        self.assertEqual(self.backend.sent, [(ROOM, 'voice')])
        self.assertFalse(self.voice_paths[0].exists())
        self.client.synthesize.assert_called_once_with('这是模型生成的回答。', FIRST.id)

    def test_synthesis_failure_falls_back_before_any_voice_is_sent(self):
        self.setup_speech()
        self.client.synthesize.side_effect = TTSError('模拟接口失败')
        deliver_model_reply(self.ctx, self.message('提问'), '文字回答', display_name='测试成员')
        self.assertEqual(len(self.backend.sent), 1)
        self.assertIn('文字回答', self.backend.sent[0][1])
        self.assertEqual(list((self.root / 'tts').rglob('*.wav')), [])

    def test_uncertain_native_send_is_not_retried_as_text(self):
        self.setup_speech()
        self.backend.send_voice = Mock(return_value=False)
        with self.assertRaises(RuntimeError):
            deliver_model_reply(self.ctx, self.message('提问'), '回答')
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(list((self.root / 'tts').rglob('*.wav')), [])

    def test_voice_switch_disable_clear_personality_and_disconnect_cancel_inflight_audio(self):
        for change in ('voice', 'disable', 'clear', 'personality', 'disconnect', 'shutdown'):
            with self.subTest(change=change):
                self.setup_speech()
                self.store.set_voice(ROOM, FIRST.id, FIRST.name)
                message = self.message('问题', sequence=len(self.backend.sent) + 1)
                self.store.record(message)
                snapshot = self.store.conversation_snapshot(message)
                personality = self.store.personality(ROOM)

                def synthesize(*_):
                    if change == 'voice': self.speech.select(ROOM, SECOND.name)
                    elif change == 'disable': self.active = False
                    elif change == 'clear': self.store.clear(ROOM)
                    elif change == 'personality': self.store.set_personality(ROOM, '夸夸' if personality.name != '夸夸' else '卑微')
                    elif change == 'disconnect': self.service.cancel_pending()
                    else: self.speech.close()
                    return wav()

                self.client.synthesize.side_effect = synthesize
                with self.assertRaises(SnapshotExpired):
                    deliver_model_reply(self.ctx, message, '回答',
                                        delivery_guard=lambda: self.ctx.response_guard(snapshot, personality))
                self.assertEqual(self.backend.sent, [])
                self.assertEqual(list((self.root / 'tts').rglob('*.wav')), [])
                if change == 'disconnect':
                    self.service.close()
                    self.service.bind(self.backend)

    def test_commands_list_switch_unknown_and_disable_are_plain_text(self):
        self.setup_speech()
        plugin = Plugin()
        plugin.members = self.store
        for text in ('/查看音色', '/音色 测试乙', '/音色 不存在', '/音色 关闭'):
            plugin.on_message(self.message(text), self.ctx)
        self.assertIn(FIRST.name, self.backend.sent[0][1])
        self.assertIn('测试乙', self.backend.sent[1][1])
        self.assertIn('未找到', self.backend.sent[2][1])
        self.assertIn('文字回复', self.backend.sent[3][1])
        self.assertEqual(self.speech.selected(OTHER_ROOM)[1], FIRST)
        self.client.synthesize.assert_not_called()
        self.assertTrue(own_group_command('/查看音色'))
        self.assertTrue(own_group_command('/音色 测试乙'))

    def test_mention_model_output_uses_speech_and_keeps_personality_context(self):
        self.setup_speech()
        self.store.record(self.message('今天写代码'))
        msg = self.message('@机器人\u2005评价一下', sequence=11,
                           data={'is_at_self': True, 'at_members': [daily.ROBOT]})
        self.store.record(msg)
        self.ctx._llm = Mock(complete_json=Mock(return_value={'reply': '继续保持。'}))
        plugin = ChatPlugin()
        plugin.on_load(self.ctx)
        plugin.on_message(msg, self.ctx)
        self.client.synthesize.assert_called_once_with('继续保持。', FIRST.id)
        self.assertEqual(self.backend.sent[-1], (ROOM, 'voice'))
        prompt = json.loads(self.ctx._llm.complete_json.call_args.args[1])
        self.assertIn('今天写代码', json.dumps(prompt, ensure_ascii=False))

    def test_chunks_do_not_lose_text(self):
        text = '第一句话。第二句话！第三句话很长很长很长很长。'
        chunks = list(speech_chunks(text, 12))
        self.assertEqual(''.join(chunks), text)
        self.assertTrue(all(len(part) <= 12 for part in chunks))

    def test_public_menu_search_pagination_and_id_listing_do_not_switch_voice(self):
        self.setup_speech()
        public = Voice('c' * 32, '公开测试')
        self.client.public_voices.return_value = VoicePage((public,), 2, 10, True, True)
        plugin = Plugin()
        plugin.members = self.store
        plugin.on_message(self.message('/查看音色 温柔 2'), self.ctx)
        text = ''.join(row[1] for row in self.backend.sent)
        self.assertIn('中文公开音色（第 2 页）', text)
        self.assertIn(public.name, text)
        self.assertNotIn(public.id, text)
        self.assertIn('/查看音色 温柔 3', text)
        self.assertNotIn('个人音色：', text)
        self.client.public_voices.assert_called_once_with(page=2, keyword='温柔', page_size=10, all_languages=False)
        self.assertEqual(self.store.voice_selection(ROOM).voice_id, '')
        self.client.synthesize.assert_not_called()

    def test_menu_shows_ids_only_for_duplicate_or_truncated_names_and_preserves_all_language_paging(self):
        self.setup_speech()
        a, b, long = Voice('c' * 32, '重名'), Voice('d' * 32, '重名'), Voice('e' * 32, '很长' * 30)
        self.client.public_voices.return_value = VoicePage((a, b, long), 1, 10, True)
        plugin = Plugin()
        plugin.members = self.store
        plugin.on_message(self.message('/查看音色 全部 温柔'), self.ctx)
        text = ''.join(row[1] for row in self.backend.sent)
        self.assertIn('全部公开音色', text)
        self.assertIn('/查看音色 全部 温柔 2', text)
        for voice in (a, b, long):
            self.assertIn(voice.id, text)
        self.client.public_voices.assert_called_once_with(page=1, keyword='温柔', page_size=10, all_languages=True)

    def test_workspace_exact_name_has_priority_over_public_names(self):
        self.setup_speech()
        self.speech.select(ROOM, FIRST.name)
        self.client.public_voices.assert_not_called()
        self.client.public_voice.assert_not_called()

    def test_public_name_selection_persists_and_synthesizes_correct_id(self):
        self.setup_speech()
        public = Voice('c' * 32, '公开测试')
        self.client.public_voices.return_value = VoicePage((public,), 1, 100, False)
        self.speech.select(ROOM, public.name)
        self.assertEqual(self.new_store().voice_selection(ROOM).voice_id, public.id)
        self.assertEqual(self.speech.selected(OTHER_ROOM)[1], FIRST)
        deliver_model_reply(self.ctx, self.message('问题'), '公开音色回答')
        self.client.synthesize.assert_called_once_with('公开音色回答', public.id)
        self.client.public_voice.assert_not_called()
        self.assertEqual(self.backend.sent, [(ROOM, 'voice')])

    def test_duplicate_public_names_across_pages_require_id_without_changing_selection(self):
        self.setup_speech()
        a, b = Voice('c' * 32, '重名'), Voice('d' * 32, '重名')
        self.client.public_voices.side_effect = [VoicePage((a,), 1, 100, True), VoicePage((b,), 2, 100, False)]
        before = self.store.voice_selection(ROOM)
        with self.assertRaises(TTSError) as caught:
            self.speech.select(ROOM, '重名')
        self.assertIn(a.id, str(caught.exception))
        self.assertIn(b.id, str(caught.exception))
        self.assertEqual(self.store.voice_selection(ROOM), before)

    def test_public_id_outside_browsed_pages_is_loaded_once_and_works_after_restart(self):
        self.setup_speech()
        public = Voice('e' * 32, '未浏览的公开音色')
        self.client.public_voice.return_value = public
        self.speech.select(ROOM, public.id)
        self.assertEqual(self.speech.selected(ROOM)[1], public)
        self.client.public_voice.assert_called_once_with(public.id)
        with patch('wxbot_framework.speech.FishTTS.from_file', return_value=self.client):
            self.speech.reload()
        self.assertEqual(self.speech.selected(ROOM)[1], public)
        self.assertEqual(self.client.public_voice.call_count, 2)

    def test_reload_during_public_fetch_cancels_stale_response(self):
        self.setup_speech()
        def fetch(**kwargs):
            with patch('wxbot_framework.speech.FishTTS.from_file', return_value=self.client):
                self.speech.reload()
            return VoicePage((), 1, 10, False)
        self.client.public_voices.side_effect = fetch
        with self.assertRaises(SnapshotExpired):
            self.speech.public_catalog()


if __name__ == '__main__':
    unittest.main()
