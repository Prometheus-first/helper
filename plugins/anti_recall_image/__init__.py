"""独立的图片防撤回插件。"""
from wxbot_framework.media_recall import MediaRecallPlugin


class Plugin(MediaRecallPlugin):
    media = 'image'
    label = '图片'
