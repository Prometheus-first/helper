"""独立的文件防撤回插件。"""
from wxbot_framework.media_recall import MediaRecallPlugin


class Plugin(MediaRecallPlugin):
    media = 'file'
    label = '文件'

    def send_recalled(self, message, ctx, path):
        """path 是已恢复原文件名的附件，在这里决定如何补发。"""
        return ctx.reply_file(message, path)
