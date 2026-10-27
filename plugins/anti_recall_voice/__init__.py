"""独立语音防撤回：只补发已完整缓存的原 SILK 语音。"""
from wxbot_framework.media_recall import MediaRecallPlugin


class Plugin(MediaRecallPlugin):
    media = 'voice'
    label = '语音'

    def send_recalled(self, message, ctx, path):
        """path 是框架保留的原语音副本，不调用模型或语音合成。"""
        return ctx.reply_voice(message, path)
