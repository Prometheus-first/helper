"""真正艾特机器人后，用本群人格回复当前发言者。"""
from wxbot_framework.dialogue import Dialogue
from wxbot_framework.llm import LLMError
from wxbot_framework.members import SnapshotExpired
from wxbot_framework.personality import personality_text
from wxbot_framework.speech import deliver_model_reply


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members
        if not self.members.history_enabled:
            raise ValueError("艾特上下文对话需要 history_enabled: true")
        ctx.llm  # 启动时核对配置，不请求接口。

    def on_message(self, message, ctx):
        if (message.type != 1 or not message.is_group or message.chat_id not in self.members.groups or
                "WXBOT_AT_CHAT_PROBE" in message.text or
                message.data.get("bot_generated") or not message.data.get("is_at_self") or
                self.members.account not in message.data.get("at_members", ())):
            return
        personality = ctx.personality(message)
        try:
            snapshot = self.members.conversation_snapshot(message)
            guard = lambda: ctx.response_guard(snapshot, personality)
            self.reply_to_member(message, ctx, snapshot, personality, guard)
            ctx.logger.info("艾特对话完成：room=%s persona=%s context=%d", message.chat_id,
                            personality.name, len(snapshot.messages))
        except SnapshotExpired:
            ctx.logger.info("艾特对话已取消：记录或人格状态已变更")
        except LLMError as error:
            ctx.logger.warning("艾特对话失败：%s", error)
            try:
                ctx.reply(message, "暂时无法回复，请稍后再艾特我。", delivery_guard=guard)
            except SnapshotExpired:
                pass

    def reply_to_member(self, message, ctx, snapshot, personality, delivery_guard):
        """上下文已准备好，在这里生成并发送给群友的回复。"""
        reply = personality_text(Dialogue(ctx.llm_for(message, personality=personality)).reply(snapshot),
                                 personality.name)
        display = self.members.display_name(message.sender_id, message.chat_id) or snapshot.speaker_name
        deliver_model_reply(ctx, message, reply, display_name=display, delivery_guard=delivery_guard)
