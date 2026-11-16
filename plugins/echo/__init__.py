"""独立回声插件示例：echo 你好 → 你好。"""


class Plugin:
    def on_load(self, ctx):
        self.prefix = ctx.config.get("prefix", "echo ")
        if not isinstance(self.prefix, str) or not self.prefix:
            raise ValueError("echo.prefix 必须是非空字符串")

    def on_message(self, message, ctx):
        if message.type == 1 and message.text.startswith(self.prefix):
            text = message.text[len(self.prefix):]
            if text.strip():
                ctx.reply(message, text)
                return True
