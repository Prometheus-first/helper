"""接收线程先切换，本插件负责显示确认。"""
from wxbot_framework.members import SnapshotExpired
from wxbot_framework.personality import switch_ack


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members

    def on_message(self, message, ctx):
        if not message.is_group or message.chat_id not in self.members.groups or message.type != 1:
            return
        if message.data.get("personality_error"):
            ctx.reply(message, message.data["personality_error"])
            return True
        state = message.data.get("personality_switch")
        if state is None:
            return
        try:
            ctx.reply(message, switch_ack(state.name), delivery_guard=lambda: ctx.personality_guard(state))
            ctx.logger.info("人格已切换：room=%s name=%s", state.room, state.name)
        except SnapshotExpired:
            pass
        return True
