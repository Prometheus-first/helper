"""附件防撤回的公共缓存逻辑；插件各自持有状态，不依赖文本插件或网页。"""
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from .file_metadata import FileMetadataError, parse_file_metadata
from .files import inspect_file, prepare_file, safe_file_name
from .images import validate_image_path
from .recall import message_id


def is_file_attachment(message):
    if message.type != 49:
        return False
    kind = message.data.get('app_type')
    if kind is not None:
        return kind == 6
    if message.data.get('file_status') in ('pending', 'ready', 'failed'):
        return True
    try:
        # 下载完成前只识别附件类型，不保留或记录 XML 中的 CDN 凭据。
        return parse_file_metadata(message.data.get('raw_content')) is not None
    except (FileMetadataError, ValueError, TypeError):
        return False


@dataclass(frozen=True)
class CachedMedia:
    path: Path
    signature: tuple
    name: str


class MediaRecallPlugin:
    media = ''
    label = ''

    def on_load(self, ctx):
        self.originals = OrderedDict()
        self.recalled = OrderedDict()

    @staticmethod
    def _put(cache, key, value):
        cache[key] = value
        cache.move_to_end(key)
        if len(cache) > 1000:
            cache.popitem(last=False)

    @staticmethod
    def _signature(path):
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    def _cache(self, message):
        if message.data.get(self.media + '_status') != 'ready':
            return None
        raw = message.data.get(self.media + '_path')
        if not raw:
            return None
        path = validate_image_path(raw) if self.media == 'image' else inspect_file(raw).path
        name = safe_file_name(message.data.get('file_name') or path.name)
        return CachedMedia(path, self._signature(path), name)

    def _reply(self, key, message, ctx):
        entry = self.originals.get(key)
        if entry is None or self.recalled.get(key) is not False:
            return
        # 先消费通知；重复回调、下载完成事件和发送结果不确定都不能重复补发。
        self.recalled[key] = True
        try:
            if self._signature(entry.path) != entry.signature:
                raise ValueError('缓存附件已被替换')
            if self.media == 'image':
                sent = ctx.reply_image(message, entry.path)
            else:
                # 接收缓存的路径带随机前缀；发送快照恢复原文件名，不改接收文件。
                with prepare_file(entry.path, entry.path.parent) as snapshot:
                    if self._signature(entry.path) != entry.signature:
                        raise ValueError('缓存附件在准备期间被替换')
                    named = snapshot.path.with_name(entry.name)
                    if named != snapshot.path:
                        snapshot.path.rename(named)
                    sent = ctx.reply_file(message, named)
            if sent is False:
                ctx.logger.warning('撤回%s补发未获微信确认，不重复发送', self.label)
        except (OSError, ValueError, RuntimeError):
            # 不记录本地路径、附件名或底层带凭据的异常。
            ctx.logger.warning('撤回%s未补发：缓存不可用或原连接已结束', self.label)

    def on_message(self, message, ctx):
        if not message.session_id or message.data.get('bot_generated'):
            return
        notice = message.data.get('recall')
        if message.type in (10000, 10002) and isinstance(notice, dict):
            identifier = message_id(notice.get('message_id'))
            if not identifier or notice.get('chat_id') != message.chat_id:
                return
            key = message.session_id, message.chat_id, identifier
            if key not in self.recalled:
                self._put(self.recalled, key, False)
                self._reply(key, message, ctx)
            return
        if not ((self.media == 'image' and message.type == 3) or
                (self.media == 'file' and is_file_attachment(message))):
            return
        identifier = message_id(message.data.get('native_message_id'))
        if not identifier:
            return
        key = message.session_id, message.chat_id, identifier
        if self.recalled.get(key) is True:
            return
        try:
            entry = self._cache(message)
        except (OSError, ValueError, RuntimeError):
            ctx.logger.warning('%s接收缓存不可用，跳过防撤回缓存', self.label)
            return
        if entry is not None:
            self._put(self.originals, key, entry)
            self._reply(key, message, ctx)

    def on_unload(self, ctx):
        # 附件由接收器管理，本插件只清空引用，不删除其他功能共用的接收缓存。
        self.originals.clear()
        self.recalled.clear()
