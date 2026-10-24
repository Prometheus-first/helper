"""群命令：/总结 昵称，或 /总结群聊。"""
from wxbot_framework.llm import LLMError
from wxbot_framework.members import MemberError, SnapshotExpired
from wxbot_framework.personality import personality_text
from wxbot_framework.summary import ChatSummarizer, conversation_rows, split_text


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members
        if not self.members.history_enabled or not self.members.daily_clear:
            raise ValueError("聊天总结需要成员配置 history_enabled 和 daily_clear 都为 true")
        self.summarizer = ChatSummarizer(ctx.llm, chunk_chars=ctx.config.get("chunk_chars", 16000))

    def on_message(self, message, ctx):
        if message.type != 1 or not message.is_group or message.chat_id not in self.members.groups:
            return
        words = message.text.strip().split(maxsplit=1)
        if not words or words[0] not in ("/总结", "/总结群聊"):
            return
        group = words[0] == "/总结群聊"
        if len(words) != (1 if group else 2):
            ctx.reply(message, "用法：/总结群聊" if group else "用法：/总结 昵称")
            return
        try:
            snapshot = (self.members.today_group_snapshot(message.chat_id) if group else
                        self.members.today_snapshot(words[1], message.chat_id))
        except MemberError as error:
            ctx.reply(message, str(error) + ("。" if group else "。请本人先发送 /绑定 昵称。"))
            return
        rows = conversation_rows(snapshot)
        personality = ctx.personality(message)
        guard = lambda: ctx.response_guard(snapshot, personality)
        try:
            if not rows:
                name = "本群" if group else snapshot.member.name
                ctx.reply(message, f"{name}今天还没有可总结的聊天记录。", delivery_guard=guard)
                return
            self.reply_summary(message, ctx, snapshot, rows, personality, group, guard)
            ctx.logger.info("聊天总结完成：room=%s scope=%s member=%s records=%d", message.chat_id,
                            "group" if group else "member", "本群" if group else snapshot.member.name, len(rows))
        except SnapshotExpired:
            ctx.logger.info("聊天总结已取消：记录或人格状态已变更，room=%s", message.chat_id)
        except LLMError as error:
            ctx.logger.warning("聊天总结失败：%s", error)
            try:
                ctx.reply(message, "暂时无法生成聊天总结，请稍后重试。", delivery_guard=guard)
            except SnapshotExpired:
                pass

    def reply_summary(self, message, ctx, snapshot, rows, personality, group, delivery_guard):
        """当天记录已准备好，在这里生成总结、组织文案并回复。"""
        summarizer = ChatSummarizer(ctx.llm_for(message, personality=personality),
                                   chunk_chars=self.summarizer.chunk_chars)
        summary = summarizer.summarize(snapshot, rows,
            current=lambda: self.members.snapshot_is_current(snapshot))
        if group:
            participants = len({row["sender_id"] for row in rows})
            title = f"今日群聊总结（{snapshot.day}，{len(rows)}条，{participants}人）\n"
        else:
            title = f"{snapshot.member.name} 今日聊天（{snapshot.day}，{len(rows)}条）\n"
        content = personality_text(summary, personality.name) if personality.name == "嘴臭" else title + summary
        for part in split_text(content):
            if not ctx.reply(message, part, delivery_guard=delivery_guard):
                raise RuntimeError("原生文本发送未成功")
