"""手动启用的微信更新保护；也可在启动微信前运行命令行工具。"""
from .guard import GuardError, protect


class Plugin:
    def on_load(self, ctx):
        try:
            result = protect(ctx.project_root)
            ctx.logger.info('微信更新保护已检查：%s', result)
        except GuardError as error:
            raise RuntimeError(str(error)) from None

    def on_message(self, message, ctx):
        pass
