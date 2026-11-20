"""Random prompt draws using the existing image generation service."""
from plugins.image_generation import Plugin as ImageGenerationPlugin
from plugins.image_generation.client import GenerationCancelled, GenerationError
from .catalog import PromptCatalog


class Plugin(ImageGenerationPlugin):
    plugin_name = 'image_gacha'

    def on_load(self, ctx):
        self.catalog = PromptCatalog()
        super().on_load(ctx)

    def on_message(self, message, ctx):
        if (message.type != 1 or message.is_self or message.data.get('bot_generated') or
                message.text.strip() != '/抽卡' or not self._current(message, ctx)):
            return
        if not self.slots.acquire(blocking=False):
            ctx.reply(message, '抽卡任务较多，请稍后再试。',
                      delivery_guard=lambda: self._guard(message, ctx))
            return True
        try:
            if not self._current(message, ctx):
                self.slots.release()
                return True
            self.jobs.put_nowait((message, None))
        except BaseException:
            self.slots.release()
            raise
        return True

    def _generate(self, message, _prompt, ctx):
        try:
            prompt = self.catalog.pick(current=lambda: self._current(message, ctx))
        except GenerationCancelled:
            return
        except GenerationError:
            ctx.logger.warning('抽卡目录暂不可用')
            if self._current(message, ctx):
                ctx.reply(message, '抽卡暂时失败，请稍后再试。',
                          delivery_guard=lambda: self._guard(message, ctx))
            return
        if self._current(message, ctx):
            super()._generate(message, prompt, ctx)
