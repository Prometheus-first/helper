"""独立处理清空，避免排在耗时的模型请求后面。"""
class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members

    def on_message(self, message, ctx):
        if message.type != 1 or not message.is_group or message.chat_id not in self.members.groups:
            return
        words = message.text.strip().split(maxsplit=1)
        if not words or words[0] != "/清空":
            return
        if len(words) != 1:
            ctx.reply(message, "用法：/清空")
            return True
        self.members.clear(message.chat_id)
        ctx.reply(message, "已清空本群聊天记录。")
        return True
