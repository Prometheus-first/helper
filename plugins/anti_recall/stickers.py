"""撤回表情包的有界内存缓存；原始 URL 不记录日志、不投递给模型。"""
from collections import OrderedDict
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import queue
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from xml.etree import ElementTree as ET

from wxbot_framework import emoticons

MAX_SOURCE_BYTES = 20 * 1024 * 1024
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_CACHE_ITEMS = 128
CDN_HOSTS = frozenset({'vweixinf.tc.qq.com', 'vweixin.tc.qq.com',
                       'wxapp.tc.qq.com', 'emoji.qpic.cn'})


class StickerError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Sticker:
    md5: str
    length: int | None = None
    url: str = field(default='', repr=False)


def sticker_url(value):
    if (not isinstance(value, str) or not value or len(value) > 4096 or '\\' in value or
            any(ord(ch) <= 32 or ord(ch) == 127 for ch in value)):
        raise StickerError('表情包下载地址无效')
    try:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or parsed.hostname not in CDN_HOSTS or
                parsed.username or parsed.password or parsed.fragment or not parsed.path.startswith('/') or
                parsed.port not in (None, 80 if parsed.scheme == 'http' else 443)):
            raise ValueError()
    except ValueError:
        raise StickerError('表情包下载地址不受支持') from None
    # 某些微信旧 CDN 仅提供 HTTP；保留消息给出的协议，下载后必须校验 MD5。
    return value


def parse_sticker(content):
    if not isinstance(content, str) or not content or len(content) > 16384 or '\0' in content:
        raise StickerError('表情包元数据无效')
    try:
        if len(content.encode('utf-8')) > 16384:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise StickerError('表情包元数据过长或编码无效') from None
    if re.search(r'<!\s*(DOCTYPE|ENTITY)\b', content, re.I):
        raise StickerError('表情包元数据不允许实体声明')
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        raise StickerError('表情包元数据格式无效') from None
    pending, count = [(root, 1)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > 128 or depth > 16 or len(node.attrib) > 64:
            raise StickerError('表情包元数据结构过大')
        pending.extend((child, depth + 1) for child in node)
    nodes = list(root.iter('emoji'))
    if root.tag != 'msg' or len(nodes) != 1 or nodes[0] not in list(root):
        raise StickerError('表情包元数据缺少唯一 emoji 节点')
    attributes = nodes[0].attrib
    md5 = attributes.get('md5', '')
    if not re.fullmatch(r'[a-fA-F0-9]{32}', md5):
        raise StickerError('表情包缺少内容校验信息')
    length = attributes.get('len', '')
    if length and (not re.fullmatch(r'[0-9]{1,10}', length) or int(length) > MAX_SOURCE_BYTES):
        raise StickerError('表情包声明大小超过支持范围')
    length = (int(length) or None) if length else None
    return Sticker(md5.lower(), length, sticker_url(attributes.get('cdnurl', '')))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def download_sticker(sticker, is_current):
    if not is_current():
        raise StickerError('表情包连接已结束')
    url = sticker_url(sticker.url)
    request = urllib.request.Request(url, headers={'User-Agent': 'helper-sticker-recall',
                                                  'Accept': 'image/gif,image/png,application/octet-stream'})
    try:
        deadline = time.monotonic() + 15
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=5) as response:
            length = response.headers.get('Content-Length')
            if length and (not length.isdecimal() or int(length) > MAX_SOURCE_BYTES):
                raise StickerError('表情包响应超过大小限制')
            chunks, size = [], 0
            while True:
                if not is_current() or time.monotonic() > deadline:
                    raise StickerError('表情包下载已取消或超时')
                part = response.read(min(65536, MAX_SOURCE_BYTES + 1 - size))
                if not part:
                    break
                size += len(part)
                if size > MAX_SOURCE_BYTES:
                    raise StickerError('表情包响应超过大小限制')
                chunks.append(part)
    except (OSError, urllib.error.URLError, ValueError):
        raise StickerError('表情包下载失败或超过支持范围') from None
    data = b''.join(chunks)
    if (not is_current() or not data or (sticker.length is not None and len(data) != sticker.length) or
            hashlib.md5(data).hexdigest() != sticker.md5):
        raise StickerError('表情包内容校验失败或连接已结束')
    return data


def _gif_payload(data):
    """按 GIF 块结构找到结束符，去掉微信表情附在结束符后的标记。

    不能用 rfind(';')：压缩图像数据和尾部标记本身也可能含这个字节。
    提取后仍须完整解码验证全部帧。
    """
    if len(data) < 14:
        raise StickerError('表情 GIF 数据不完整')
    position = 13 + (3 * (1 << ((data[10] & 7) + 1)) if data[10] & 128 else 0)
    images = 0
    def blocks(position):
        while position < len(data):
            size = data[position]
            position += 1
            if size == 0:
                return position
            position += size
            if position > len(data):
                break
        raise StickerError('表情 GIF 数据块不完整')
    for _ in range(100000):
        if position >= len(data):
            break
        kind = data[position]
        position += 1
        if kind == 0x3B and images:
            return data[:position]
        if kind == 0x21:
            position = blocks(position + 1)
        elif kind == 0x2C and position + 9 <= len(data):
            packed = data[position + 8]
            position += 9 + (3 * (1 << ((packed & 7) + 1)) if packed & 128 else 0)
            position = blocks(position + 1)  # LZW minimum code size 后为数据子块。
            images += 1
        else:
            break
    raise StickerError('表情 GIF 结构无效或缺少结束符')


def gif_bytes(data, is_current):
    """保留可发送 GIF 的原始字节；PNG/WXGF 沿用已有的受限转换器。"""
    if not is_current() or not 0 < len(data) <= MAX_SOURCE_BYTES:
        raise StickerError('表情包转换已取消或大小无效')
    if data.startswith((b'GIF87a', b'GIF89a')):
        data = _gif_payload(data)
    if data.startswith(b'wxgf'):
        with tempfile.TemporaryDirectory(prefix='helper-sticker-decode-') as temporary:
            frames, durations = emoticons._decode_wxgf_frames(data, Path(temporary), is_current)
            return emoticons._gif_bytes(frames, durations, is_current)
    kind, width, height, _, _, _ = emoticons._raster_info(data)
    if (kind == 'GIF' and len(data) <= emoticons.MAX_EMOTICON_BYTES and
            max(width, height) <= emoticons.MAX_EMOTICON_DIMENSION):
        return data
    _, _, _, _, frames, durations = emoticons._raster_info(data, keep_frames=True)
    return emoticons._gif_bytes(frames, durations, is_current)


class StickerCache:
    def __init__(self, is_current, deliver, *, fetch=None):
        self._is_current, self._deliver, self._fetch = is_current, deliver, fetch or download_sticker
        self._stop = threading.Event()
        self._queue = queue.Queue(maxsize=16)
        self._lock = threading.RLock()
        self._items = OrderedDict()
        self._bytes = 0
        self._worker = None

    def get(self, identifier):
        with self._lock:
            data = self._items.get(identifier)
            if data is not None:
                self._items.move_to_end(identifier)
            return data

    def submit(self, key, entry, sticker):
        with self._lock:
            if self._stop.is_set():
                return False
            try:
                self._queue.put_nowait((key, entry, sticker))
            except queue.Full:
                return False
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name='RecallStickerCache', daemon=True)
                self._worker.start()
            return True

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    key, entry, sticker = self._queue.get(timeout=.1)
                except queue.Empty:
                    continue
                current = lambda: not self._stop.is_set() and self._is_current(key[0])
                success = False
                try:
                    if current():
                        data = self.get(sticker.md5)
                        if data is None:
                            data = gif_bytes(self._fetch(sticker, current), current)
                            if not current():
                                raise StickerError('表情包连接已结束')
                            with self._lock:
                                self._items[sticker.md5] = data
                                self._bytes += len(data)
                                while len(self._items) > MAX_CACHE_ITEMS or self._bytes > MAX_CACHE_BYTES:
                                    _, old = self._items.popitem(last=False)
                                    self._bytes -= len(old)
                        success = True
                except Exception:
                    # 不输出 CDN 地址、凭据、原消息 XML 或解码器异常。
                    pass
                finally:
                    try:
                        if current():
                            self._deliver(key, entry, success)
                    except Exception:
                        pass
                    self._queue.task_done()
        finally:
            with self._lock:
                self._items.clear()
                self._bytes = 0

    def close(self):
        self._stop.set()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
            self._queue.task_done()
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout=1)
        with self._lock:
            self._items.clear()
            self._bytes = 0
