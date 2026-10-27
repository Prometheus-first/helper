"""保存入站消息自带的完整 SILK 语音；不连接微信、网络或模型。"""
from __future__ import annotations

from collections import OrderedDict
import math
import os
from pathlib import Path
import queue
import re
import stat
import threading
import uuid
import xml.etree.ElementTree as ET

from .account import validate_account
from .recall import message_id
from .text import validate_target
from .voices import (STANDARD_SILK_HEADER, TENCENT_SILK_HEADER,
                     _check_local_path, _inspect_silk, inspect_voice_file)

MAX_RECEIVED_VOICE_BYTES = 262_144
MAX_VOICE_XML_BYTES = 16_384
_EVENT_FIELDS = frozenset({
    'wx_type', 'chat_id', 'room_wxid', 'from_wxid', 'sender_id', 'native_sender',
    'native_receiver', 'self_wxid', 'is_self', 'sequence', 'native_thread_id',
    'native_message_id', 'sender_wechat_name', 'dispatch_to_plugins', 'bot_generated',
    'is_at_self', 'monitor_only',
})


def _safe_directory(path):
    if not path.is_absolute() or path.resolve() != path:
        raise ValueError('语音缓存目录必须是无链接的绝对本地路径')
    _check_local_path(path)
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and
                getattr(part.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('语音缓存目录不能使用链接')


def _voice_data(payload, xml):
    if (not isinstance(payload, bytes) or not 12 < len(payload) <= MAX_RECEIVED_VOICE_BYTES or
            not payload.startswith((TENCENT_SILK_HEADER, STANDARD_SILK_HEADER))):
        raise ValueError('完整 SILK 音频不可用')
    if not isinstance(xml, str) or '\0' in xml or len(xml.encode('utf-8')) > MAX_VOICE_XML_BYTES:
        raise ValueError('语音元数据无效')
    # 群消息的作者前缀属于消息路由，不属于 XML。
    content = xml.strip()
    if not content.startswith('<') and ':\n' in content:
        prefix, content = content.split(':\n', 1)
        validate_account(prefix)
        content = content.strip()
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', content, re.IGNORECASE):
        raise ValueError('语音元数据不支持实体声明')
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        raise ValueError('语音元数据无效') from None
    stack, total = [(root, 1)], 0
    while stack:
        element, depth = stack.pop()
        total += 1
        if depth > 8 or total > 128 or len(element.attrib) > 64:
            raise ValueError('语音元数据结构过于复杂')
        stack.extend((child, depth + 1) for child in element)
    nodes = [root] if root.tag == 'voicemsg' else root.findall('voicemsg') if root.tag == 'msg' else []
    if len(nodes) != 1:
        raise ValueError('语音元数据缺少唯一的音频信息')
    node = nodes[0]
    if len(node) or len(node.attrib) > 64:
        raise ValueError('语音元数据结构无效')
    if node.get('endflag', '1') != '1' or node.get('cancelflag', '0') != '0':
        raise ValueError('语音尚未接收完整')
    duration = node.get('voicelength', '')
    if not re.fullmatch(r'[1-9][0-9]{0,4}', duration) or int(duration) > 60_000:
        raise ValueError('语音时长无效')
    declared = node.get('length')
    if declared is not None and (not re.fullmatch(r'[1-9][0-9]{0,6}', declared) or int(declared) != len(payload)):
        raise ValueError('语音声明长度与音频不一致')
    parsed = _inspect_silk(payload)
    # 实机微信语音可标注 2980 ms 而携带 148 个完整帧（2960 ms）。
    # endflag、声明字节数和完整包边界均已校验，容许一帧的计时差。
    if abs(parsed.duration_ms - int(duration)) > 20:
        raise ValueError('语音声明时长与音频不一致')
    return parsed.silk, parsed.duration_ms


class VoiceReceiver:
    """每连接一个受限队列，保存自有音频副本后投递 ready/failed。"""

    def __init__(self, directory, account, deliver, is_current, queue_size=16):
        validate_account(account)
        if account in ('.', '..'):
            raise ValueError('语音缓存账号无效')
        if not callable(deliver) or not callable(is_current):
            raise ValueError('语音接收回调必须可调用')
        if type(queue_size) is not int or not 1 <= queue_size <= 1024:
            raise ValueError('语音接收队列容量必须为 1 到 1024')
        self.root = Path(directory)
        self.directory = self.root / account
        _safe_directory(self.root)
        _safe_directory(self.directory)
        if self.directory.parent != self.root:
            raise ValueError('语音账号目录超出缓存范围')
        self.account = account
        self._callback, self._is_current = deliver, is_current
        self._queue = queue.Queue(maxsize=queue_size)
        self._enqueue_lock = threading.Lock()
        self._stop = threading.Event()
        self._seen = OrderedDict()
        self._worker = threading.Thread(target=self._run, name='wechat-voice-receiver', daemon=True)
        self._worker.start()

    @property
    def is_alive(self):
        return self._worker.is_alive()

    def _active(self):
        if self._stop.is_set():
            return False
        try:
            return bool(self._is_current()) and not self._stop.is_set()
        except Exception:
            return False

    @staticmethod
    def _snapshot(data):
        result = {name: value for name, value in data.items()
                  if name in _EVENT_FIELDS and isinstance(value, (str, int, bool))}
        result.update(msg='', content='', voice_status='pending', voice_path='',
                      voice_duration_ms=0, voice_size=0, voice_error='')
        return result

    def _deliver(self, data):
        if not self._active():
            return False
        try:
            self._callback(data)
            return True
        except Exception:
            return False

    def _failed(self, data, reason):
        data.update(voice_status='failed', voice_path='', voice_duration_ms=0,
                    voice_size=0, voice_error=reason)
        self._deliver(data)

    def submit(self, data: dict, payload: bytes) -> bool:
        if not isinstance(data, dict):
            raise ValueError('语音事件必须为字典')
        if not self._active():
            return False
        snapshot = self._snapshot(data)
        try:
            chat = data.get('chat_id', '')
            validate_target(chat)
            identifier = message_id(data.get('native_message_id'))
            if not identifier or data.get('self_wxid', self.account) != self.account:
                raise ValueError('语音所属账号或消息编号无效')
            if data.get('wx_type', 34) != 34 or not isinstance(payload, bytes) or not 12 < len(payload) <= MAX_RECEIVED_VOICE_BYTES:
                raise ValueError('语音音频缺失或过长')
            xml = data.get('raw_content')
            if not isinstance(xml, str) or len(xml.encode('utf-8')) > MAX_VOICE_XML_BYTES:
                raise ValueError('语音元数据缺失或过长')
        except (ValueError, TypeError, UnicodeError):
            self._failed(snapshot, '语音音频或元数据不可用，不能完整补发')
            return False
        full = False
        with self._enqueue_lock:
            if self._stop.is_set():
                return False
            key = (chat, identifier)
            if key in self._seen:
                return True
            try:
                self._queue.put_nowait((snapshot, payload, xml))
            except queue.Full:
                full = True
            else:
                self._seen[key] = None
                if len(self._seen) > 1000:
                    self._seen.popitem(last=False)
        if full:
            self._failed(snapshot, '语音接收队列已满')
            return False
        return True

    def _remove_owned(self, path):
        try:
            _safe_directory(self.directory)
            if path.parent == self.directory and path.resolve(strict=True) == path:
                path.unlink()
        except (OSError, ValueError, RuntimeError):
            pass

    def _save(self, body):
        _safe_directory(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        _safe_directory(self.directory)
        self.directory.mkdir(exist_ok=True)
        _safe_directory(self.directory)
        destination = self.directory / (uuid.uuid4().hex + '.silk')
        _check_local_path(destination)
        created = False
        try:
            with destination.open('xb') as output:
                created = True
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            if destination.is_symlink() or destination.resolve(strict=True) != destination:
                raise ValueError('语音缓存文件被替换')
            if inspect_voice_file(destination).duration_ms <= 0:
                raise ValueError('语音缓存文件无效')
            return destination
        except BaseException:
            if created:
                self._remove_owned(destination)
            raise

    def _process(self, data, payload, xml):
        destination = None
        try:
            if not self._active():
                return
            body, duration = _voice_data(payload, xml)
            if not self._active():
                return
            destination = self._save(body)
            if not self._active():
                self._remove_owned(destination)
                return
            data.update(voice_status='ready', voice_path=str(destination), voice_duration_ms=duration,
                        voice_size=len(body), voice_error='')
            if not self._deliver(data):
                self._remove_owned(destination)
        except Exception:
            if destination is not None:
                self._remove_owned(destination)
            self._failed(data, '语音不完整、格式不支持或缓存失败，不能完整补发')

    def _run(self):
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=.05)
            except queue.Empty:
                continue
            try:
                self._process(*item)
            finally:
                self._queue.task_done()

    def close(self, timeout=1):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError('语音接收器关闭等待时间必须为有限非负数')
        self._stop.set()
        with self._enqueue_lock:
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
                self._queue.task_done()
            self._seen.clear()
        if threading.current_thread() is not self._worker:
            self._worker.join(timeout=min(timeout, 60))
