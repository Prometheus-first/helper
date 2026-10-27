"""按连接、会话及微信原始消息编号补发撤回内容，不依赖网页监控。"""
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
import tempfile
import threading

from wxbot_framework.recall import message_id
from wxbot_framework.media_recall import is_file_attachment
from wxbot_framework.summary import split_text
from .stickers import StickerCache, StickerError, parse_sticker


@dataclass
class Original:
    content: str
    delegate: str = ''
    sticker_key: str = ''
    pending: bool = False
    message: object = field(default=None, repr=False)


class Plugin:
    def on_load(self, ctx):
        self.originals = OrderedDict()
        self.recalled = OrderedDict()
        self._lock = threading.RLock()
        self._active = True
        self.stickers = StickerCache(lambda session: self._active and ctx._service.session_is_current(session),
                                     lambda key, entry, ready: self._sticker_ready(key, entry, ready, ctx))

    @staticmethod
    def _put(cache, key, value):
        cache[key] = value
        cache.move_to_end(key)
        if len(cache) > 1000:
            cache.popitem(last=False)

    def _reply(self, key, entry, message, ctx):
        with self._lock:
            if not self._active or not entry or entry.pending or self.recalled.get(key) is not False:
                return
            # 在发送前消费通知；重复系统回调或媒体下载完成不会再次补发。
            self.recalled[key] = True
            data = self.stickers.get(entry.sticker_key) if entry.sticker_key else None
        if entry.delegate and ctx.plugin_is_active(entry.delegate):
            return
        if data is not None:
            # 内存缓存不依赖微信文件是否被撤回清理；临时 GIF 只活到原生发送返回。
            try:
                with tempfile.TemporaryDirectory(prefix='helper-recall-sticker-send-') as temporary:
                    path = Path(temporary) / 'sticker.gif'
                    path.write_bytes(data)
                    if ctx.reply_emoticon(message, path, delivery_guard=self._delivery_guard) is False:
                        ctx.logger.warning('撤回表情包补发未获确认，不重复发送')
            except (OSError, ValueError, RuntimeError):
                ctx.logger.warning('撤回表情包补发未完成，不重复发送')
            return
        for part in split_text(self.format_recall(entry.content), byte_limit=1023):
            if ctx.reply(message, part, delivery_guard=self._delivery_guard) is False:
                break

    def format_recall(self, content):
        """content 是被撤回的原文，返回要补发的文字。"""
        return '撤回的消息是：' + content

    @contextmanager
    def _delivery_guard(self):
        with self._lock:
            if not self._active:
                raise RuntimeError('防撤回插件已停止')
            yield

    def _sticker_ready(self, key, entry, ready, ctx):
        with self._lock:
            if not self._active or self.originals.get(key) is not entry:
                return
            entry.pending = False
            if not ready:
                entry.sticker_key = ''
        # 仅在已经收到撤回通知时补发；普通收表情只缓存，不回复。
        self._reply(key, entry, entry.message, ctx)

    def on_message(self, message, ctx):
        if not message.session_id or message.data.get('bot_generated'):
            return
        recall = message.data.get('recall')
        if message.type in (10000, 10002) and isinstance(recall, dict):
            identifier = message_id(recall.get('message_id'))
            if not identifier or recall.get('chat_id') != message.chat_id:
                return
            key = (message.session_id, message.chat_id, identifier)
            with self._lock:
                if key in self.recalled:
                    return
                self._put(self.recalled, key, False)
                entry = self.originals.get(key)
            self._reply(key, entry, message, ctx)
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
                    'anti_recall_file' if is_file_attachment(message) else
                    'anti_recall_voice' if message.type == 34 else '')
        entry = Original(content[:2000], delegate)
        sticker = None
        if message.type == 47 and 'emoticon.send' in ctx.capabilities:
            try:
                sticker = parse_sticker(message.data.get('raw_content') or message.data.get('content') or
                                        message.data.get('msg') or '')
            except StickerError:
                pass
            if sticker is not None:
                entry.sticker_key, entry.pending = sticker.md5, True
                entry.message = replace(message, data={}, text='')
        with self._lock:
            if not self._active or self.recalled.get(key) is True or (message.type == 47 and key in self.originals):
                return
            self._put(self.originals, key, entry)
        if sticker is not None and not self.stickers.submit(key, entry, sticker):
            entry.pending, entry.sticker_key = False, ''
        self._reply(key, entry, message, ctx)

    def on_unload(self, ctx):
        with self._lock:
            self._active = False
            self.originals.clear()
            self.recalled.clear()
        self.stickers.close()
