"""独立的图片防撤回插件。"""
from wxbot_framework.media_recall import MediaRecallPlugin


class Plugin(MediaRecallPlugin):
    media = 'image'
    label = '图片'

    def send_recalled(self, message, ctx, path):
        """path 是框架保留的原图片，在这里决定如何补发。"""
        return ctx.reply_image(message, path)
