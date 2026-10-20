"""只处理框架确认的成员变化，不响应群里的同名文字。"""
from string import Formatter


def _template(value, field):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 700 or "\0" in value:
        raise ValueError(f"{field} 应为非空文本，最多 700 个 UTF-8 字节")
    try:
        for _, name, spec, conversion in Formatter().parse(value):
            if name is not None and (name != "nickname" or spec or conversion):
                raise ValueError("只支持 {nickname} 占位符")
    except ValueError as error:
        raise ValueError(f"{field} 模板格式错误：{error}") from None
    return value


class Plugin:
    def on_load(self, ctx):
        self.welcome = ctx.config.get("welcome", True)
        self.leave_notice = ctx.config.get("leave_notice", True)
        if type(self.welcome) is not bool or type(self.leave_notice) is not bool:
            raise ValueError("welcome / leave_notice 必须是布尔值")
        self.welcome_template = _template(ctx.config.get("welcome_template", "欢迎 {nickname} 加入群聊！"),
                                         "welcome_template")
        self.leave_template = _template(ctx.config.get("leave_template", "{nickname} 离开了群聊。"),
                                       "leave_template")

    def on_message(self, message, ctx):
        event = message.data.get("group_event", {})
        if message.type != 10002 or not message.is_group or event.get("room_id") != message.chat_id:
            return
        kind = event.get("kind")
        if not event.get("member_id") or kind not in ("join", "leave"):
            return
        if (kind == "join" and not self.welcome) or (kind == "leave" and not self.leave_notice):
            return
        nickname = event.get("nickname") or "群友"
        if getattr(ctx, "_members", None) is not None:
            # /绑定 只在本群有效；退出时仍能使用保存过的群昵称。
            nickname = ctx.members.display_name(event["member_id"], message.chat_id) or nickname
        template = self.welcome_template if kind == "join" else self.leave_template
        ctx.reply(message, template.format(nickname=nickname[:64]))
