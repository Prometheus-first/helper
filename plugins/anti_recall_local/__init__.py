"""仅在绑定账号的电脑微信中保留撤回原消息。"""


class Plugin:
    def on_load(self, ctx):
        self.owner = None
        self.owner = ctx.enable_local_recall()

    def on_message(self, message, ctx):
        # 拦截在微信本地处理前执行，插件只管理开关。
        pass

    def on_unload(self, ctx):
        ctx.disable_local_recall(self.owner)
