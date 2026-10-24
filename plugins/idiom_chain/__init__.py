"""群聊成语接龙；独立玩家与每轮首条抢答。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from wxbot_framework.llm import LLMError
from wxbot_framework.members import SnapshotExpired
from wxbot_framework.personality import game_reply, personality_text
from wxbot_framework.text import GROUP_CONTROL_COMMANDS, FOUR_CHINESE, validate_mentions
from .judge import IdiomJudge

HELP = "1. 成语接龙\n\n2. 看图猜成语\n\n3. /绑定 昵称\n\n4. /总结 昵称\n\n5. /总结群聊\n\n6. /切换人格 嘴臭|卑微|夸夸|取消\n\n7. 艾特机器人对话\n\n8. /查看音色\n\n9. /音色 名称|关闭\n\n10. /清空\n\n11. /来张图\n\n12. /help"


@dataclass
class Chain:
    current: str
    used: tuple
    ready_at: float = 0


@dataclass
class Game:
    opening: str
    latest: Chain
    started_at: float = field(default_factory=time.monotonic)
    mode: str = "independent"
    players: dict = field(default_factory=dict)
    shared: Chain | None = None


class Plugin:
    def on_load(self, ctx):
        self.judge = IdiomJudge(ctx.llm)
        self.games = {}
        self.session_id = None
        self.member_names = ctx.config.get("member_names", {})
        if not isinstance(self.member_names, dict):
            raise ValueError("idiom_chain.member_names 应为按群 ID 分类的昵称配置")
        for room, names in self.member_names.items():
            if not isinstance(names, dict):
                raise ValueError("群昵称配置必须是 JSON 对象")
            for member, name in names.items():
                validate_mentions(room, [member])
                if not isinstance(name, str) or not name.strip() or len(name) > 64 or any(ch in name for ch in "\n\r\0"):
                    raise ValueError("群昵称应为 1–64 字符的单行文本")

    def on_unload(self, ctx):
        self.games.clear()

    def on_message(self, message, ctx):
        if message.type != 1 or not message.is_group or message.data.get("is_at_self"):
            return
        text = message.text.strip()
        if message.is_self and text not in GROUP_CONTROL_COMMANDS and not FOUR_CHINESE.fullmatch(text):
            return
        personality = ctx.personality(message)
        try:
            self._handle(message, ctx, personality)
        except SnapshotExpired:
            ctx.logger.info("接龙回复已取消：人格已切换")

    def _handle(self, message, ctx, personality):
        text = message.text.strip()
        judge = IdiomJudge(ctx.llm_for(message, personality=personality))
        guard = lambda: ctx.personality_guard(personality)
        if self.session_id != message.session_id:
            self.games.clear()
            self.session_id = message.session_id
        if text == "/help":
            ctx.reply(message, HELP, delivery_guard=guard)
            return
        game = self.games.get(message.chat_id)
        if text in ('看图猜成语', '/看图猜成语') and getattr(ctx, 'plugin_is_active', lambda name: False)('picture_idiom'):
            self.games.pop(message.chat_id, None)
            return
        if text == "退出接龙":
            self.games.pop(message.chat_id, None)
            ctx.reply(message, "已退出接龙。", delivery_guard=guard)
            return
        if text == "成语接龙":
            if game:
                ctx.reply(message, game.latest.current, delivery_guard=guard)
                return
            try:
                opening = judge.opening()
            except LLMError as error:
                ctx.logger.warning("接龙开场失败：%s", error)
                ctx.reply(message, "暂时无法向大模型获取成语，请稍后再发“成语接龙”。", delivery_guard=guard)
                return
            if ctx.reply(message, opening, delivery_guard=guard):
                self.games[message.chat_id] = Game(opening, Chain(opening, (opening,), time.monotonic()))
            return
        if text == "合并接龙":
            if not game:
                ctx.reply(message, "请先发送“成语接龙”。", delivery_guard=guard)
                return
            if game.mode == "merged":
                ctx.reply(message, game.shared.current, delivery_guard=guard)
                return
            current = game.latest
            if ctx.reply(message, current.current, delivery_guard=guard):
                game.mode = "merged"
                game.shared = Chain(current.current, current.used, time.monotonic())
                game.players.clear()
            return
        if not game or not text or text.startswith("/"):
            return
        if game.mode == "merged":
            chain = game.shared
        else:
            # 新玩家共享开场词，后入场不受其他玩家最近出题时间影响。
            chain = game.players.get(message.sender_id) or Chain(game.opening, (game.opening,), game.started_at)
        if message.received_at <= chain.ready_at:
            return  # 同一轮等待判定/发题时收到的后续答案不当成下一轮答案。
        try:
            verdict = judge.evaluate(chain.current, text, chain.used)
        except LLMError as error:
            ctx.logger.warning("接龙判定失败：%s", error)
            response = (personality_text(f"暂时判不了，仍接{chain.current}。", personality.name)
                        if personality.name == "嘴臭" else
                        f"大模型暂时无法判定，请稍后重试。当前成语：{chain.current}")
            accepted = False
            following, used = chain.current, chain.used
        else:
            accepted = verdict.accepted
            following = verdict.next_idiom if accepted else chain.current
            used = chain.used + (text, following) if accepted else chain.used
            response = game_reply(personality.name, accepted, verdict.reason, chain.current, text, following)
        name = self.member_names.get(message.chat_id, {}).get(message.sender_id)
        sent = ctx.reply_at(message, response, display_name=name, delivery_guard=guard) if game.mode == "independent" else ctx.reply(message, response, delivery_guard=guard)
        if sent:
            updated = Chain(following, used, time.monotonic())
            if game.mode == "independent":
                game.players[message.sender_id] = updated
            else:
                game.shared = updated
            if accepted:
                game.latest = updated
