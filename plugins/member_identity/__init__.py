"""成员本人发送 /绑定 昵称，注册或更新称呼，供所有插件查询。"""
from wxbot_framework.members import MemberError


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members

    def on_message(self, message, ctx):
        if not message.is_group or message.type != 1 or message.chat_id not in self.members.groups:
            return
        parts = message.text.strip().split(maxsplit=1)
        if not parts or parts[0] != "/绑定":
            return
        if len(parts) != 2:
            ctx.reply(message, "用法：/绑定 昵称")
            return True
        name = parts[1].strip()
        try:
            member = self.members.register(name, message.chat_id, message.sender_id)
        except MemberError as error:
            ctx.reply(message, str(error))
            return True
        ctx.logger.info("成员已绑定：room=%s name=%s wxid=%s", member.room, member.name, member.wxid)
        ctx.reply(message, f"已绑定：{member.name}。")
        return True
