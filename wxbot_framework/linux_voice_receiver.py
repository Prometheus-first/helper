"""Linux 入站 SILK 私有缓存；支持精确消息的本地语音库，不发送消息。"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field, replace
import math
import os
from pathlib import Path
import queue
import stat
import tempfile
import threading
import time
import uuid

from .account import validate_account
from .recall import message_id
from .sdk import Message
from .text import validate_target
from .voice_receiver import MAX_RECEIVED_VOICE_BYTES, MAX_VOICE_XML_BYTES, _voice_data


@dataclass(frozen=True, slots=True)
class LinuxVoicePayload:
    xml: str = field(repr=False)
    body: bytes = field(repr=False)


class LinuxVoiceReceiver:
    """每个机器人连接单独拥有队列、去重记录和有限磁盘缓存。

    submit 返回清理后的初始状态；完成回调可能先于 submit 调用方投递。
    调用方必须像图片接收一样提供 pending gate，不在 emitter 调用中持锁。
    """

    def __init__(self, *, account, session_id, cache_root, is_current,
                 allowed_targets=(), callback=None, queue_size=16,
                 max_files=128, max_cache_bytes=32 * 1024 * 1024, source=None):
        self.account = validate_account(account)
        if source is not None and not callable(getattr(source, 'lookup', None)):
            raise ValueError('Linux 语音数据源无效')
        self._source = source
        if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
            raise ValueError('Linux 语音接收需要有效会话')
        if not callable(is_current):
            raise ValueError('Linux 语音接收需要会话检查')
        for value, maximum in ((queue_size, 128), (max_files, 1000),
                               (max_cache_bytes, 256 * 1024 * 1024)):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError('Linux 语音缓存预算无效')
        self.allowed = frozenset(validate_target(target) for target in allowed_targets)
        self.session_id, self._current = session_id, is_current
        self.max_files, self.max_cache_bytes = max_files, max_cache_bytes
        self._callback = None
        self.set_callback(callback)
        self._stop, self._lock = threading.Event(), threading.RLock()
        self._queue = queue.Queue(queue_size)
        self._seen, self._saved = OrderedDict(), OrderedDict()
        root = Path(cache_root).expanduser().absolute()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._private(root)
        self._root = root
        self._workspace = Path(tempfile.mkdtemp(prefix='voice-', dir=root))
        self._worker = threading.Thread(target=self._run, name='linux-voice-receiver', daemon=True)
        self._worker.start()

    @staticmethod
    def _private(path):
        info = path.lstat()
        if (path.resolve(strict=True) != path or not stat.S_ISDIR(info.st_mode) or
                info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError('Linux 语音缓存必须是当前用户的无链接私有目录')

    def set_callback(self, callback):
        if callback is not None and not callable(callback):
            raise ValueError('Linux 语音回调必须可调用')
        self._callback = callback

    @property
    def is_alive(self):
        return self._worker.is_alive()

    def _active(self):
        if self._stop.is_set():
            return False
        try:
            return bool(self._current()) and not self._stop.is_set()
        except Exception:
            return False

    def _clean(self, message):
        data = dict(native_message_id=message_id(message.data.get('native_message_id')),
                    self_wxid=self.account, monitor_only=True, media_cache_only=True, dispatch_to_plugins=False,
                    dispatch_to_recall_plugins=True,
                    bot_generated=bool(message.data.get('bot_generated')))
        role = message.data.get('sender_role')
        data['sender_role'] = role if isinstance(role, str) and role in ('owner', 'admin', 'member') else 'unknown'
        return replace(message, text='', data=data)

    @staticmethod
    def _status(message, status, *, path='', duration=0, size=0, error=''):
        return replace(message, data=dict(message.data, voice_status=status, voice_path=str(path),
            voice_duration_ms=duration, voice_size=size, voice_error=error))

    @staticmethod
    def _key(message):
        return message.chat_id, message.data['native_message_id']

    def _remember(self, key, message):
        self._seen[key] = message
        self._seen.move_to_end(key)
        while len(self._seen) > 1000:
            self._seen.popitem(last=False)

    def submit(self, message):
        if not isinstance(message, Message):
            raise ValueError('Linux 语音接收需要 Message')
        if message.type != 34:
            return message
        clean = self._clean(message)
        if (message.session_id != self.session_id or not self._active() or
                message.data.get('self_wxid') != self.account or
                (self.allowed and message.chat_id not in self.allowed) or
                message.data.get('bot_generated')):
            return self._status(clean, 'unavailable', error='语音消息不属于当前接收范围')
        try:
            validate_target(message.chat_id)
            if not clean.data['native_message_id']:
                raise ValueError('缺少消息编号')
            payload = message.data.get('_linux_voice')
            if payload is None or (self._source is None and isinstance(payload, LinuxVoicePayload) and
                                   isinstance(payload.body, bytes) and not payload.body):
                return self._status(clean, 'unavailable', error='消息未携带完整语音，不能恢复原音频')
            if (not isinstance(payload, LinuxVoicePayload) or not isinstance(payload.body, bytes) or
                    (payload.body and not 12 < len(payload.body) <= min(MAX_RECEIVED_VOICE_BYTES, self.max_cache_bytes)) or
                    not isinstance(payload.xml, str) or '\0' in payload.xml or
                    len(payload.xml.encode('utf-8')) > MAX_VOICE_XML_BYTES):
                raise ValueError('缺少完整音频')
            # 前缀只接受已路由的本条作者；不让共享解析器接受另一个作者的 XML。
            xml = payload.xml.strip()
            if not xml.startswith('<'):
                prefix = message.sender_id + ':\n'
                if not message.is_group or not message.sender_id or not xml.startswith(prefix):
                    raise ValueError('语音作者前缀无效')
                xml = xml[len(prefix):]
            payload = LinuxVoicePayload(xml, payload.body)
        except (ValueError, TypeError, UnicodeError):
            return self._status(clean, 'failed', error='语音音频或元数据不可用，不能完整补发')
        pending, key = self._status(clean, 'pending'), self._key(clean)
        with self._lock:
            if self._stop.is_set():
                return self._status(clean, 'unavailable', error='语音接收会话已结束')
            if key in self._seen:
                return self._seen[key]
            try:
                self._queue.put_nowait((key, pending, payload))
            except queue.Full:
                return self._status(clean, 'failed', error='语音接收队列已满')
            self._remember(key, pending)
        return pending

    @staticmethod
    def _signature(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

    def _remove(self, path, signature):
        try:
            self._private(self._root)
            self._private(self._workspace)
            info = path.lstat()
            if (path.parent == self._workspace and path.resolve(strict=True) == path and
                    stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid() and
                    info.st_nlink == 1 and self._signature(info) == signature):
                path.unlink()
        except (OSError, ValueError, RuntimeError):
            pass

    def _save(self, body, key):
        if not self._active():
            return None
        with self._lock:
            if self._stop.is_set():
                return None
            self._private(self._root)
            self._private(self._workspace)
            total = sum(size for size, _, _ in self._saved.values())
            while self._saved and (len(self._saved) >= self.max_files or total + len(body) > self.max_cache_bytes):
                old, (size, signature, old_key) = self._saved.popitem(last=False)
                self._remove(old, signature)
                total -= size
                if old_key in self._seen:
                    self._seen[old_key] = self._status(self._seen[old_key], 'unavailable', error='语音缓存已淘汰')
            if len(body) > self.max_cache_bytes:
                raise ValueError('语音超出缓存预算')
            path = self._workspace / (uuid.uuid4().hex + '.silk')
            signature = None
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                with os.fdopen(descriptor, 'wb', buffering=0) as output:
                    signature = self._signature(os.fstat(output.fileno()))
                    try:
                        if output.write(body) != len(body):
                            raise OSError('语音缓存写入不完整')
                        output.flush()
                        os.fsync(output.fileno())
                    finally:
                        signature = self._signature(os.fstat(output.fileno()))
                if path.resolve(strict=True) != path or self._signature(path.lstat()) != signature:
                    raise ValueError('语音缓存已变化')
                self._saved[path] = (len(body), signature, key)
                return path
            except BaseException:
                if signature is not None:
                    self._remove(path, signature)
                raise

    def _process(self, key, pending, payload):
        if not self._active():
            return
        try:
            if not payload.body and self._source is not None:
                deadline = time.monotonic() + 5
                while self._active():
                    body = self._source.lookup(*key)
                    if body:
                        payload = LinuxVoicePayload(payload.xml, body)
                        break
                    if time.monotonic() >= deadline or self._stop.wait(.2):
                        break
            if not self._active():
                return
            body, duration = _voice_data(payload.body, payload.xml)
            path = self._save(body, key)
            if path is None:
                return
            completed = self._status(pending, 'ready', path=path, duration=duration, size=len(body))
        except Exception:
            completed = self._status(pending, 'failed', error='语音不完整、格式不支持或缓存失败，不能完整补发')
        if not self._active():
            return
        with self._lock:
            if self._stop.is_set():
                return
            self._remember(key, completed)
        callback = self._callback
        if callback is not None and self._active():
            try:
                callback(completed)
            except Exception:
                pass

    def _cleanup(self):
        if self._source is not None:
            self._source.close()
        with self._lock:
            for path, (_, signature, _) in self._saved.items():
                self._remove(path, signature)
            self._saved.clear()
            self._seen.clear()
        try:
            self._private(self._workspace)
            self._workspace.rmdir()
        except (OSError, ValueError, RuntimeError):
            pass

    def _run(self):
        try:
            while not self._stop.is_set():
                try:
                    job = self._queue.get(timeout=.05)
                except queue.Empty:
                    continue
                try:
                    self._process(*job)
                finally:
                    self._queue.task_done()
        finally:
            self._cleanup()

    def close(self, timeout=1):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError('语音接收关闭等待时间无效')
        self._stop.set()
        with self._lock:
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    break
        if threading.current_thread() is not self._worker:
            self._worker.join(min(timeout, 60))
