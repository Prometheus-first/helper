"""独立的文件防撤回插件。"""
from wxbot_framework.media_recall import MediaRecallPlugin


class Plugin(MediaRecallPlugin):
    media = 'file'
    label = '文件'
