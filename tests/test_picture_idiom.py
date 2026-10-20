"""题卡渲染、群内首答、出题失败和切换游戏，均不连接微信或网络。"""
from contextlib import nullcontext
import logging
from pathlib import Path
import random
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from plugins.picture_idiom import Plugin
from plugins.picture_idiom.bank import PUZZLES, Puzzle
from plugins.picture_idiom.render import CardRenderer, find_font
from plugins.picture_idiom.source import RepeatedQuestion, SourceError
from wxbot_framework.personality import PersonalityState
from wxbot_framework.sdk import Message
from wxbot_framework.text import FOUR_CHINESE, TextRouter

ROOT = Path(__file__).resolve().parents[1]
ROOM, OTHER = '123@chatroom', '456@chatroom'
A, B = 'wxid_player_a', 'wxid_player_b'


class Context:
    def __init__(self):
        self.members = SimpleNamespace(account='wxid_test_robot', groups={ROOM, OTHER})
        self.config = {'source': 'local'}
        self.sent = []
        self.image_success = self.text_success = True
        self.persona = '默认'
        self.logger = logging.getLogger('PictureIdiomTest')
        self.active = {'picture_idiom', 'idiom_chain'}

    def plugin_is_active(self, name):
        return name in self.active

    def personality(self, message):
        return PersonalityState(message.chat_id, self.persona)

    def personality_guard(self, state):
        return nullcontext()

    def reply_image(self, message, path):
        self.sent.append(('image', message.chat_id, path))
        return self.image_success

    def reply(self, message, text, **kwargs):
        self.sent.append(('text', message.chat_id, text))
        return self.text_success

    def reply_at(self, message, text, **kwargs):
        self.sent.append(('at', message.chat_id, text, message.sender_id))
        return self.text_success


class PictureGameTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.ctx = Context()
        self.clock = patch('plugins.picture_idiom.time.monotonic', side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        with patch('plugins.picture_idiom.CardRenderer') as renderer:
            renderer.return_value.render.side_effect = lambda puzzle: Path('card-' + puzzle.code + '.png')
            self.plugin = Plugin()
            self.plugin.on_load(self.ctx)
        self.plugin.random = random.Random(0)

    def send(self, text, *, room=ROOM, sender=A, received=None, session='one', data=None, **kwargs):
        self.now += 0.1
        message = Message(text, room, sender_id=sender, is_group=True, session_id=session,
                          received_at=self.now if received is None else received, data=data or {}, **kwargs)
        self.plugin.on_message(message, self.ctx)
        return message

    def test_start_sends_only_picture_and_repeat_does_not_change_question(self):
        self.send('看图猜成语')
        self.assertEqual([row[0] for row in self.ctx.sent], ['image'])
        self.assertEqual(self.plugin.games[ROOM].current.answer, '绕梁三日')
        ready_at = self.plugin.games[ROOM].ready_at
        self.send('看图猜成语')
        self.assertEqual(self.plugin.games[ROOM].ready_at, ready_at)
        self.assertEqual(len(self.plugin.games[ROOM].deck), len(PUZZLES) - 1)
        self.assertEqual(self.ctx.sent[0][2], self.ctx.sent[1][2])

    def test_first_correct_reply_wins_and_queued_answers_do_not_win_next_round(self):
        self.send('看图猜成语')
        queued_at = self.now + 0.01
        self.send('绕梁三日', sender=A)
        next_question = self.plugin.games[ROOM].current
        self.send('绕梁三日', sender=B, received=queued_at)
        winners = [row for row in self.ctx.sent if row[0] == 'at']
        self.assertEqual(len(winners), 1)
        self.assertEqual(winners[0][3], A)
        self.assertNotEqual(next_question.code, PUZZLES[0].code)
        self.assertEqual(self.plugin.games[ROOM].current, next_question)

    def test_wrong_answer_preserves_question_and_cooldown_limits_replies(self):
        self.send('看图猜成语')
        self.send('五颜六色')
        self.send('三心二意', sender=B)
        self.assertEqual(len([row for row in self.ctx.sent if row[0] == 'at']), 1)
        self.assertEqual(self.plugin.games[ROOM].current, PUZZLES[0])
        self.now += 4
        self.send('/猜 三心二意')
        self.assertEqual(len([row for row in self.ctx.sent if row[0] == 'at']), 2)

    def test_hint_reveal_and_change_question(self):
        self.send('看图猜成语')
        self.send('提示')
        self.assertIn(PUZZLES[0].hint, self.ctx.sent[-1][2])
        self.send('提示')
        self.assertIn('首字是“绕”', self.ctx.sent[-1][2])
        self.send('公布答案')
        self.assertIn(PUZZLES[0].answer, self.ctx.sent[-2][2])
        current = self.plugin.games[ROOM].current
        self.send('换一题')
        self.assertIn(current.answer, self.ctx.sent[-2][2])
        self.assertNotEqual(self.plugin.games[ROOM].current, current)

    def test_groups_exit_and_sessions_are_isolated(self):
        self.send('看图猜成语')
        self.send('看图猜成语', room=OTHER)
        self.send('退出猜成语')
        self.assertNotIn(ROOM, self.plugin.games)
        self.assertIn(OTHER, self.plugin.games)
        self.send('随便聊聊', room=OTHER, session='two')
        self.assertEqual(self.plugin.games, {})

    def test_image_or_ack_failure_does_not_consume_next_question(self):
        self.ctx.image_success = False
        self.send('看图猜成语')
        self.assertNotIn(ROOM, self.plugin.games)
        self.ctx.image_success = True
        self.send('看图猜成语')
        self.ctx.text_success = False
        self.send('绕梁三日')
        self.assertEqual(self.plugin.games[ROOM].current, PUZZLES[0])
        self.ctx.text_success = True
        self.ctx.image_success = False
        self.send('绕梁三日')
        self.assertIsNone(self.plugin.games[ROOM].current)
        self.ctx.image_success = True
        self.send('看图猜成语')
        self.assertIsNotNone(self.plugin.games[ROOM].current)

    def test_other_group_bot_output_and_mentions_do_not_trigger(self):
        self.send('看图猜成语', room='999@chatroom')
        self.send('看图猜成语', data={'bot_generated': True})
        self.send('看图猜成语', data={'is_at_self': True})
        self.assertEqual(self.ctx.sent, [])

    def test_explicit_guess_and_invalid_guess(self):
        self.send('看图猜成语')
        self.send('/猜 不是四个字呀')
        self.assertIn('用法', self.ctx.sent[-1][2])
        self.send('/猜 绕梁三日！')
        self.assertIn('绕梁三日', self.ctx.sent[-2][2])

    def test_starting_chain_clears_picture_game_only_when_chain_is_active(self):
        self.send('看图猜成语')
        self.send('成语接龙')
        self.assertNotIn(ROOM, self.plugin.games)
        self.send('看图猜成语')
        self.ctx.active.remove('idiom_chain')
        self.send('成语接龙')
        self.assertIn(ROOM, self.plugin.games)

    def test_own_account_controls_pass_router(self):
        router = TextRouter()
        router.set_account(self.ctx.members.account)
        for text in ('看图猜成语', '/看图猜成语', '换一题', '提示', '公布答案', '退出猜成语', '/猜 绕梁三日'):
            with self.subTest(text=text):
                row = router.convert(self.ctx.members.account, ROOM, text)
                self.assertTrue(row['dispatch_to_plugins'])

    def test_personality_changes_feedback_without_corrupting_answer(self):
        self.send('看图猜成语')
        self.ctx.persona = '嘴臭'
        self.send('绕梁三日')
        text = self.ctx.sent[-2][2]
        self.assertIn('绕梁三日', text)
        self.assertLessEqual(len(text), 20)

    def test_deck_cycles_without_immediate_repeat(self):
        self.send('看图猜成语')
        seen = [self.plugin.games[ROOM].current.code]
        for _ in range(len(PUZZLES) - 1):
            self.send('换一题')
            seen.append(self.plugin.games[ROOM].current.code)
        self.assertEqual(len(set(seen)), len(PUZZLES))
        self.send('换一题')
        self.assertNotEqual(seen[-1], self.plugin.games[ROOM].current.code)

    def online_question(self, answer='一刀两断'):
        self.plugin.source = 'ffapi'
        puzzle = Puzzle('online-test', answer, f'答案首字是“{answer[0]}”。', '', 'online',
                        image_path=Path('online-test.png'))
        self.plugin.online = Mock()
        self.plugin.online.fetch.return_value = puzzle
        return puzzle

    def test_online_question_repeat_start_hints_and_correct_answer(self):
        puzzle = self.online_question()
        self.send('看图猜成语')
        game = self.plugin.games[ROOM]
        self.assertEqual(game.current, puzzle)
        self.assertEqual(len(game.deck), len(PUZZLES))
        self.assertEqual(self.ctx.sent[-1][2], puzzle.image_path)
        self.send('看图猜成语')
        self.plugin.online.fetch.assert_called_once_with(exclude=())
        self.send('提示')
        self.assertIn('首字是“一”', self.ctx.sent[-1][2])
        self.send('提示')
        self.assertIn('前两字是“一刀”', self.ctx.sent[-1][2])
        next_puzzle = Puzzle('online-next', '井底之蛙', '首字为井', '', 'online',
                             image_path=Path('online-next.png'))
        self.plugin.online.fetch.return_value = next_puzzle
        self.send('/猜 一刀两断')
        self.assertEqual(self.ctx.sent[-2][0], 'at')
        self.assertEqual(self.plugin.games[ROOM].current, next_puzzle)
        self.plugin.online.fetch.assert_called_with(exclude=('一刀两断',))

    def test_local_mode_does_not_access_online_source(self):
        self.plugin.online = Mock()
        self.send('看图猜成语')
        self.plugin.online.fetch.assert_not_called()

    def test_online_failure_uses_local_cooldown_then_recovers(self):
        online = self.online_question()
        self.plugin.online.fetch.side_effect = SourceError('unavailable')
        with self.assertLogs('PictureIdiomTest', level='WARNING'):
            self.send('看图猜成语')
        self.assertEqual(self.plugin.games[ROOM].current, PUZZLES[0])
        self.send('换一题')
        self.assertEqual(self.plugin.online.fetch.call_count, 1)
        self.now += 61
        self.plugin.online.fetch.side_effect = None
        self.send('换一题')
        self.assertEqual(self.plugin.games[ROOM].current, online)
        self.assertEqual(self.plugin.online.fetch.call_count, 2)

    def test_duplicate_online_retries_then_local_avoids_immediate_repeat(self):
        self.online_question(answer=PUZZLES[0].answer)
        self.send('看图猜成语')
        self.plugin.online.fetch.side_effect = RepeatedQuestion('duplicate')
        self.send('换一题')
        game = self.plugin.games[ROOM]
        self.assertEqual(self.plugin.online.fetch.call_count, 3)
        self.assertNotEqual(game.current.answer, PUZZLES[0].answer)
        self.assertIn(PUZZLES[0], game.deck)

    def test_online_send_failure_does_not_commit_round_or_history(self):
        self.send('看图猜成语')
        game = self.plugin.games[ROOM]
        original_deck, original_history = list(game.deck), list(game.recent_answers)
        self.online_question()
        self.ctx.image_success = False
        self.send('换一题')
        self.assertIsNone(game.current)
        self.assertEqual(game.deck, original_deck)
        self.assertEqual(list(game.recent_answers), original_history)
        self.ctx.image_success = True
        self.send('看图猜成语')
        self.assertEqual(list(game.recent_answers), [PUZZLES[0].answer, '一刀两断'])


class CardTests(unittest.TestCase):
    def test_bank_has_unique_four_character_answers(self):
        self.assertGreaterEqual(len(PUZZLES), 20)
        self.assertEqual(len({p.code for p in PUZZLES}), len(PUZZLES))
        self.assertEqual(len({p.answer for p in PUZZLES}), len(PUZZLES))
        self.assertTrue(all(FOUR_CHINESE.fullmatch(p.answer) and p.hint and p.explanation for p in PUZZLES))

    def test_all_cards_render_as_valid_png_without_answer_metadata(self):
        try:
            font = find_font()
        except ValueError:
            self.skipTest('当前系统没有中文字体')
        scratch = ROOT / '.runtime/tests'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as temporary:
            renderer = CardRenderer(temporary, str(font))
            for puzzle in PUZZLES:
                with self.subTest(puzzle=puzzle.code):
                    path = renderer.render(puzzle)
                    self.assertNotIn(puzzle.answer, path.name)
                    with Image.open(path) as card:
                        self.assertEqual(card.size, (720, 620))
                        self.assertEqual(card.format, 'PNG')
                        self.assertEqual(card.info, {})
                        self.assertIsNotNone(card.getbbox())
                    self.assertEqual(renderer.render(puzzle), path)


if __name__ == '__main__':
    unittest.main()
