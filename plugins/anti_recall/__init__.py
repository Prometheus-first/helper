"""按连接、会话及微信原始消息编号补发撤回内容，不依赖网页监控。"""
from collections import OrderedDict

from wxbot_framework.recall import message_id
from wxbot_framework.media_recall import is_file_attachment
from wxbot_framework.summary import split_text


class Plugin:
    def on_load(self, ctx):
        self.originals = OrderedDict()
        self.recalled = OrderedDict()

    @staticmethod
    def _put(cache, key, value):
        cache[key] = value
        cache.move_to_end(key)
        if len(cache) > 1000:
            cache.popitem(last=False)

    def _reply(self, key, entry, message, ctx):
        if not entry or self.recalled.get(key) is not False:
            return
        content, delegate = entry
        # 在发送前消费通知；重复系统回调或媒体下载完成不会再次补发。
        self.recalled[key] = True
        if delegate and ctx.plugin_is_active(delegate):
            return
        for part in split_text('撤回的消息是：' + content, byte_limit=1023):
            if ctx.reply(message, part) is False:
                break

    def on_message(self, message, ctx):
        if not message.session_id or message.data.get('bot_generated'):
            return
        recall = message.data.get('recall')
        if message.type in (10000, 10002) and isinstance(recall, dict):
            identifier = message_id(recall.get('message_id'))
            if not identifier or recall.get('chat_id') != message.chat_id:
                return
            key = (message.session_id, message.chat_id, identifier)
            if key in self.recalled:
                return
            self._put(self.recalled, key, False)
            self._reply(key, self.originals.get(key), message, ctx)
            return
        identifier = message_id(message.data.get('native_message_id'))
        if not identifier:
            return
        if message.type == 1:
            content = message.text
        elif message.type == 49:
            if message.data.get('app_type') == 19:
                content = '[合并转发]'
            else:
                name = str(message.data.get('file_name') or '').replace('\\', '/').rsplit('/', 1)[-1]
                content = '[文件] ' + name if name else '[文件/分享]'
        else:
            content = {3:'[图片]', 34:'[语音]', 43:'[视频]', 47:'[表情]', 62:'[视频]'}.get(message.type, '')
        if not isinstance(content, str) or not content or '\0' in content:
            return
        key = (message.session_id, message.chat_id, identifier)
        delegate = ('anti_recall_image' if message.type == 3 else
                    'anti_recall_file' if is_file_attachment(message) else '')
        entry = content[:2000], delegate
        self._put(self.originals, key, entry)
        self._reply(key, entry, message, ctx)

    def on_unload(self, ctx):
        self.originals.clear()
        self.recalled.clear()
