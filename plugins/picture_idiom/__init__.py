"""按群隔离的看图猜成语；不用大模型，每轮只接受首个正确答案。"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
import random
import time

from wxbot_framework.members import SnapshotExpired
from wxbot_framework.personality import personality_text
from wxbot_framework.text import FOUR_CHINESE
from .bank import PUZZLES, Puzzle
from .render import CardRenderer
from .source import FFAPI, RepeatedQuestion, SourceError

START = frozenset({'看图猜成语', '/看图猜成语'})
COMMANDS = START | {'提示', '换一题', '公布答案', '退出猜成语'}


@dataclass
class Game:
    deck: list[Puzzle]
    current: Puzzle | None = None
    ready_at: float = 0
    hint_index: int = 0
    last_wrong: float = 0
    last_code: str = ''
    recent_answers: deque = field(default_factory=lambda: deque(maxlen=32))


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members
        self.games = {}
        self.session_id = None
        self.random = random.SystemRandom()
        cooldown = ctx.config.get('wrong_reply_cooldown_seconds', 3)
        if type(cooldown) not in (int, float) or not math.isfinite(cooldown) or not 0 <= cooldown <= 60:
            raise ValueError('wrong_reply_cooldown_seconds 应为 0–60 秒')
        self.cooldown = cooldown
        self.source = ctx.config.get('source', 'ffapi')
        if self.source not in ('ffapi', 'local'):
            raise ValueError('picture_idiom.source 应为 ffapi 或 local')
        self.next_online_attempt = 0
        root = Path(__file__).resolve().parents[2]
        font = ctx.config.get('font_file')
        if font is not None:
            if not isinstance(font, str) or not font.strip() or '\0' in font:
                raise ValueError('picture_idiom.font_file 应为本地中文字体路径')
            font = str(root / font)
        account = hashlib.sha256(self.members.account.encode('ascii')).hexdigest()[:16]
        directory = root / '.runtime/picture_idiom' / account
        self.renderer = CardRenderer(directory, font)
        self.online = FFAPI(directory / 'online', timeout_seconds=ctx.config.get('timeout_seconds', 8))

    def on_unload(self, ctx):
        getattr(self, 'games', {}).clear()

    def _deck(self, *, opening=False, previous=''):
        values = list(PUZZLES)
        self.random.shuffle(values)
        first = PUZZLES[0] if opening else next(value for value in values if value.code != previous)
        values.remove(first)
        return [first, *values]

    def _question(self, message, ctx, game):
        if not game.deck:
            game.deck = self._deck(previous=game.last_code)
        puzzle = None
        if self.source == 'ffapi' and time.monotonic() >= self.next_online_attempt:
            for _ in range(2):
                try:
                    puzzle = self.online.fetch(exclude=tuple(game.recent_answers))
                    break
                except RepeatedQuestion:
                    continue
                except (SourceError, OSError):
                    # 连续离线时不让每一轮都等待网络超时；一分钟后再试在线源。
                    self.next_online_attempt = time.monotonic() + 60
                    ctx.logger.warning('在线成语题源暂不可用，使用本地题库，一分钟后重试')
                    break
        local_index = None
        if puzzle is None:
            previous = game.recent_answers[-1] if game.recent_answers else ''
            local_index = next((index for index, value in enumerate(game.deck) if value.answer != previous), None)
            if local_index is None:
                game.deck = self._deck(previous=game.last_code)
                local_index = next(index for index, value in enumerate(game.deck) if value.answer != previous)
            puzzle = game.deck[local_index]
        try:
            path = puzzle.image_path or self.renderer.render(puzzle)
        except (OSError, ValueError):
            ctx.logger.exception('看图猜成语题图生成失败')
            ctx.reply(message, '题图暂时生成失败，请稍后再发送“看图猜成语”。')
            return False
        if not ctx.reply_image(message, path):
            ctx.reply(message, '题图发送未成功，请稍后再发送“看图猜成语”。')
            return False
        if local_index is not None:
            game.deck.pop(local_index)
        game.current, game.last_code = puzzle, puzzle.code
        game.recent_answers.append(puzzle.answer)
        game.ready_at, game.hint_index, game.last_wrong = time.monotonic(), 0, 0
        ctx.logger.info('看图猜成语已出题：room=%s', message.chat_id)
        return True

    @staticmethod
    def _feedback(puzzle, personality, *, correct=False):
        if correct:
            prefix = {'嘴臭': '总算猜对了：', '卑微': '爷，您猜对了：', '夸夸': '眼力真棒，答对了：'}.get(personality.name, '答对了：')
            return personality_text(prefix + puzzle.answer + '。', personality.name)
        prefix = {'嘴臭': '看清了，答案：', '卑微': '爷，小的揭晓答案：', '夸夸': '揭晓啦，答案：'}.get(personality.name, '答案：')
        text = prefix + puzzle.answer + '。'
        if personality.name != '嘴臭' and puzzle.explanation:
            text += '\n' + puzzle.explanation
        return personality_text(text, personality.name)

    def on_message(self, message, ctx):
        if (message.type != 1 or not message.is_group or message.chat_id not in self.members.groups or
                message.data.get('bot_generated') or message.data.get('is_at_self')):
            return
        service = getattr(ctx, '_service', None)
        if service is not None and not service.session_is_current(message.session_id):
            return
        if self.session_id != message.session_id:
            self.games.clear()
            self.session_id = message.session_id
        text = message.text.strip()
        if text == '/help' and not getattr(ctx, 'plugin_is_active', lambda name: False)('idiom_chain'):
            ctx.reply(message, '1. 看图猜成语\n\n2. /help')
            return
        if text == '成语接龙' and getattr(ctx, 'plugin_is_active', lambda name: False)('idiom_chain'):
            self.games.pop(message.chat_id, None)
            return
        game = self.games.get(message.chat_id)
        if text in START:
            if game is None:
                game = Game(self._deck(opening=True))
                if self._question(message, ctx, game):
                    self.games[message.chat_id] = game
            elif game.current is None:
                self._question(message, ctx, game)
            else:
                ctx.reply_image(message, game.current.image_path or self.renderer.render(game.current))
            return
        if text == '退出猜成语':
            if self.games.pop(message.chat_id, None) is not None:
                ctx.reply(message, '已退出看图猜成语。')
            return
        if game is None:
            return
        if text == '换一题':
            if game.current is not None:
                personality = ctx.personality(message)
                try:
                    sent = ctx.reply(message, self._feedback(game.current, personality),
                                     delivery_guard=lambda: ctx.personality_guard(personality))
                except SnapshotExpired:
                    return
                if not sent:
                    return
                game.current = None
            self._question(message, ctx, game)
            return
        if game.current is None:
            return
        personality = ctx.personality(message)
        guard = lambda: ctx.personality_guard(personality)
        try:
            if text == '提示':
                second = (f'答案前两字是“{game.current.answer[:2]}”。' if game.current.kind == 'online' else
                          f'答案首字是“{game.current.answer[0]}”。')
                hints = (game.current.hint, second)
                index = min(game.hint_index, len(hints) - 1)
                if ctx.reply(message, hints[index], delivery_guard=guard):
                    game.hint_index += 1
                return
            if text == '公布答案':
                if ctx.reply(message, self._feedback(game.current, personality), delivery_guard=guard):
                    game.current = None
                    self._question(message, ctx, game)
                return
            explicit = text.split(maxsplit=1)
            if explicit and explicit[0] == '/猜':
                candidate = explicit[1].strip(' 。！!？?，,') if len(explicit) == 2 else ''
                if not FOUR_CHINESE.fullmatch(candidate):
                    ctx.reply(message, '用法：/猜 四字成语', delivery_guard=guard)
                    return
            else:
                candidate = text
            if not FOUR_CHINESE.fullmatch(candidate) or message.received_at <= game.ready_at:
                return
            if candidate != game.current.answer:
                if time.monotonic() - game.last_wrong >= self.cooldown:
                    feedback = {'嘴臭': '这都没猜中？再看图。', '卑微': '爷，是小的图没画好，再瞧瞧？',
                                '夸夸': '思路不错，再观察一下！'}.get(personality.name, '还没猜中，再看看图；也可以发送“提示”。')
                    if ctx.reply_at(message, feedback, delivery_guard=guard):
                        game.last_wrong = time.monotonic()
                return
            if ctx.reply_at(message, self._feedback(game.current, personality, correct=True), delivery_guard=guard):
                game.current = None
                self._question(message, ctx, game)
        except SnapshotExpired:
            ctx.logger.info('看图猜成语回复已取消：人格已切换')
