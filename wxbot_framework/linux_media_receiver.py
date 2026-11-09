"""Linux 消息指定媒体的有界缓存接收；图片下载由当前会话授权，不保存 CDN 凭据。"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
import hashlib
import math
import os
from pathlib import Path
import queue
import re
import stat
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

from .account import validate_account
from .file_metadata import FileMetadata, FileStore, parse_file_metadata
from .files import MAX_FILE_BYTES, safe_file_name
from .image_metadata import ImageMetadata, ImageStore, parse_image_metadata
from .images import MAX_IMAGE_BYTES
from .linux_media import decode_cached_image
from .recall import message_id
from .sdk import Message


@dataclass(frozen=True, slots=True)
class LinuxMediaPayload:
    kind: int
    metadata: ImageMetadata | FileMetadata
    app_type: int = 0


def parse_linux_media_payload(kind, content, *, sender_id='', is_group=False):
    """仅返回缓存定位所需字段；返回值不含原 XML、地址、下载令牌或 AES 密钥。

    type 74 的附件布局与 type 6 一样，复用同一严格解析器后保留原类型。
    非附件分享返回 None。未知/无效大小或校验值不允许成为缓存命中。
    """
    if kind not in (3, 49):
        return None
    if not isinstance(content, str):
        raise ValueError('Linux 媒体元数据无效')
    if is_group and sender_id:
        for prefix in (sender_id + ':\n', sender_id + ':\r\n'):
            if content.startswith(prefix):
                content = content[len(prefix):]
                break
    if kind == 3:
        value = parse_image_metadata(content)
        lengths = [n for n in (value.length, value.hdlength) if n is not None and n > 0]
        if not value.md5 or not lengths or any(n > MAX_IMAGE_BYTES for n in lengths):
            raise ValueError('Linux 图片缺少有效大小或完整校验值')
        return LinuxMediaPayload(3, ImageMetadata(md5=value.md5, length=value.length,
            hdlength=value.hdlength, width=value.width, height=value.height))
    value = parse_file_metadata(content)
    app_type = 6
    if value is None:
        # 第一遍已验证 XML 长度、DTD、结构深度与唯一的 appmsg/type。
        root = ET.fromstring(content)
        kind_node = root.find('appmsg/type')
        if kind_node is None or kind_node.text != '74':
            return None
        kind_node.text = '6'
        value = parse_file_metadata(ET.tostring(root, encoding='unicode'))
        app_type = 74
    if (value is None or not value.md5 or value.size is None or value.size <= 0 or
            not value.title or value.title in ('.', '..') or
            any(char in value.title for char in ('/', '\\', '\0'))):
        raise ValueError('Linux 附件缺少有效文件名、大小或完整校验值')
    return LinuxMediaPayload(49, FileMetadata(title=value.title, size=value.size,
        md5=value.md5, extension=value.extension), app_type)


class LinuxMediaReceiver:
    """一账号、一机器人会话的缓存队列。

    submit 返回干净的 pending/failed 消息，callback 接收同一 session 的最终状态。
    调用方应将同步投递与 callback 串行化，以保证 pending 先于 ready。
    storage_root 必须是受管微信 HOME 下的 xwechat_files；cache_root 是私有保存目录。
    只处理已收到的消息，按完整 MD5 和大小核对已落盘图片/附件；可调用预绑定
    会话的图片下载器。缓存未就绪就失败，不把缩略图或原生接口受理当作完整图片。
    """

    def __init__(self, *, account, session_id, pid, storage_root, cache_root, is_current,
                 callback=None, request_image=None, timeout=12.0, queue_size=16, max_files=128,
                 max_cache_bytes=256 * 1024 * 1024, scan_paths=4096,
                 scan_bytes=64 * 1024 * 1024, worker_count=4):
        self.account = validate_account(account)
        if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
            raise ValueError('Linux 媒体接收需要有效会话')
        if type(pid) is not int or pid <= 0 or not callable(is_current):
            raise ValueError('Linux 媒体接收需要有效进程及会话检查')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError('Linux 媒体等待时间必须在 0 到 300 秒之间')
        for value, minimum, maximum in ((queue_size, 1, 128), (max_files, 1, 1000),
                (max_cache_bytes, 1, 1024 * 1024 * 1024), (scan_paths, 1, 100000),
                (scan_bytes, 1, 1024 * 1024 * 1024), (worker_count, 1, 8)):
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError('Linux 媒体缓存预算无效')
        self.session_id, self.pid = session_id, pid
        self.storage_root = Path(storage_root).expanduser().absolute()
        if self.storage_root.exists() and self.storage_root.resolve() != self.storage_root:
            raise ValueError('Linux 媒体源目录不能经过链接')
        self._current, self.timeout = is_current, float(timeout)
        if request_image is not None and not callable(request_image):
            raise ValueError('Linux 图片下载请求必须可调用')
        self._request_image = request_image
        self._callback = None
        self.set_callback(callback)
        self.max_files, self.max_cache_bytes = max_files, max_cache_bytes
        self.scan_paths, self.scan_bytes = scan_paths, scan_bytes
        self._stop, self._lock = threading.Event(), threading.RLock()
        self._queue = queue.Queue(queue_size)
        self._seen, self._saved = OrderedDict(), OrderedDict()
        root = Path(cache_root).expanduser().absolute()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._private(root)
        self._root = root
        self._workspace = Path(tempfile.mkdtemp(prefix='receive-', dir=root))
        self._stores = {}
        for media, store in (('image', ImageStore), ('file', FileStore)):
            directory = self._workspace / media
            directory.mkdir(mode=0o700)
            obj = store(directory, account)
            obj.directory.mkdir(mode=0o700)
            self._stores[media] = obj
        self._workers = [threading.Thread(target=self._run, name=f'linux-media-receiver-{i}', daemon=True)
                         for i in range(worker_count)]
        self._worker = self._workers[0]  # 兼容已有调用方
        self._running_workers = worker_count
        for worker in self._workers:
            worker.start()

    @staticmethod
    def _private(path):
        info = path.lstat()
        if (path.resolve(strict=True) != path or not stat.S_ISDIR(info.st_mode) or
                info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError('Linux 媒体保存目录必须是当前用户的无链接私有目录')

    def set_callback(self, callback):
        if callback is not None and not callable(callback):
            raise ValueError('Linux 媒体回调必须可调用')
        self._callback = callback

    @property
    def is_alive(self):
        return any(worker.is_alive() for worker in self._workers)

    def _active(self):
        if self._stop.is_set():
            return False
        try:
            return bool(self._current()) and not self._stop.is_set()
        except Exception:
            return False

    def _clean(self, message):
        values = {'native_message_id':message_id(message.data.get('native_message_id')),
            'sender_role':message.data.get('sender_role') if message.data.get('sender_role') in
                ('owner','admin','member') else 'unknown',
            'bot_generated':bool(message.data.get('bot_generated'))}
        values.update(self_wxid=self.account, monitor_only=True,
            dispatch_to_plugins=False, dispatch_to_recall_plugins=True, media_cache_only=True)
        return replace(message, text='', data=values)

    @staticmethod
    def _status(message, status, *, path='', error=''):
        media = 'image' if message.type == 3 else 'file'
        data = dict(message.data)
        data.update({media+'_status':status, media+'_path':str(path), media+'_error':error})
        return replace(message, data=data)

    def submit(self, message, raw_content=None):
        if not isinstance(message, Message):
            raise ValueError('Linux 媒体接收需要 Message')
        if message.type not in (3, 49):
            return message
        clean = self._clean(message)
        if (message.session_id != self.session_id or not self._active() or
                message.data.get('self_wxid') != self.account or
                message.is_self or message.data.get('bot_generated')):
            return self._status(clean, 'unavailable', error='媒体消息不属于当前接收会话')
        identifier = message_id(message.data.get('native_message_id'))
        if not identifier:
            return self._status(clean, 'unavailable', error='媒体消息缺少原生消息标识')
        key = (message.chat_id, identifier, message.type)
        with self._lock:
            if key in self._seen:
                return self._seen[key]
        try:
            payload = message.data.get('_linux_media')
            if raw_content is not None:
                payload = parse_linux_media_payload(message.type, raw_content,
                    sender_id=message.sender_id, is_group=message.is_group)
            if payload is None:
                return self._status(clean, 'unavailable', error='媒体没有可验证的本地缓存元数据')
            if not isinstance(payload, LinuxMediaPayload) or payload.kind != message.type:
                raise ValueError('媒体元数据类型不符')
            metadata = payload.metadata
            if message.type == 3:
                if not isinstance(metadata, ImageMetadata):
                    raise ValueError('图片元数据类型不符')
                lengths = {n for n in (metadata.length, metadata.hdlength) if type(n) is int and n > 0}
                if not lengths or max(lengths) > min(MAX_IMAGE_BYTES, self.max_cache_bytes):
                    raise ValueError('图片超出缓存预算')
                clean = replace(clean, data=dict(clean.data, image_metadata={'md5':metadata.md5,
                    'length':metadata.length, 'hdlength':metadata.hdlength}, image_quality='unknown'))
            else:
                if (not isinstance(metadata, FileMetadata) or type(metadata.size) is not int or
                        not 0 < metadata.size <= min(MAX_FILE_BYTES, self.max_cache_bytes) or
                        not metadata.title or metadata.title in ('.','..') or
                        any(c in metadata.title for c in ('/','\\','\0'))):
                    raise ValueError('附件超出缓存预算')
                clean = replace(clean, data=dict(clean.data, app_type=6, native_app_type=payload.app_type,
                    file_name=safe_file_name(metadata.title), file_size=metadata.size,
                    file_metadata={'md5':metadata.md5, 'size':metadata.size}))
            if not isinstance(metadata.md5, str) or not re.fullmatch('[0-9a-f]{32}', metadata.md5):
                raise ValueError('媒体校验值无效')
        except (ValueError, TypeError):
            return self._status(clean, 'failed', error='媒体元数据无效或超过缓存预算')
        pending = self._status(clean, 'pending')
        if not self._active():
            return self._status(clean, 'unavailable', error='媒体接收会话已结束')
        with self._lock:
            if self._stop.is_set():
                return self._status(clean, 'unavailable', error='媒体接收会话已结束')
            if key in self._seen:
                return self._seen[key]
            try:
                self._queue.put_nowait((key, pending, payload))
            except queue.Full:
                return self._status(clean, 'failed', error='媒体接收队列已满')
            self._remember(key, pending)
        return pending

    def _remember(self, key, message):
        self._seen[key] = message
        self._seen.move_to_end(key)
        while len(self._seen) > 2048:
            self._seen.popitem(last=False)

    def owns_ready(self, message):
        """仅确认当前连接仍拥有的完整缓存；不重新下载或扩大读取范围。"""
        media = 'image' if message.type == 3 else 'file' if message.type == 49 else None
        if (media is None or message.session_id != self.session_id or
                message.data.get('self_wxid') != self.account or
                message.data.get(media + '_status') != 'ready' or not self._active()):
            return False
        try:
            path = Path(message.data.get(media + '_path', ''))
            with self._lock:
                saved = self._saved.get(path)
            if (saved is None or saved[0] != (message.chat_id, message.data.get('native_message_id'), message.type) or
                    not path.is_absolute() or not path.is_relative_to(self._workspace)):
                return False
            self._private(self._workspace)
            info = path.lstat()
            return bool(path.resolve(strict=True) == path and stat.S_ISREG(info.st_mode) and
                info.st_uid == os.geteuid() and info.st_nlink == 1 and
                stat.S_IMODE(info.st_mode) == 0o600 and self._signature(info) == saved[1])
        except (OSError, ValueError, TypeError, RuntimeError):
            return False

    @staticmethod
    def _signature(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_nlink

    @staticmethod
    def _image_hardlink_path(path, profile):
        try:
            parts = path.relative_to(profile / 'msg' / 'attach').parts
            return (len(parts) == 4 and re.fullmatch(r'[0-9a-f]{32}', parts[0]) is not None and
                re.fullmatch(r'[0-9]{4}-[0-9]{2}', parts[1]) is not None and parts[2] == 'Img' and
                re.fullmatch(r'[0-9a-f]{32}_h\.dat', parts[3]) is not None)
        except ValueError:
            return False

    def _profiles(self):
        root = self.storage_root
        if not root.is_dir() or root.resolve() != root or root.stat().st_uid != os.geteuid():
            return
        with os.scandir(root) as entries:
            for index, entry in enumerate(entries):
                if index >= 128:
                    raise ValueError('账号目录枚举超出预算')
                if not self._active():
                    return
                if (entry.is_symlink() or not entry.is_dir(follow_symlinks=False) or
                        not re.fullmatch(re.escape(self.account) + r'(?:_[0-9a-f]{4,16})?', entry.name)):
                    continue
                profile = root / entry.name
                if profile.resolve() == profile and profile.stat().st_uid == os.geteuid():
                    yield profile

    def _candidates(self, payload, deadline):
        count = 0
        for profile in self._profiles():
            linked = {}
            root = profile / 'msg'
            if not root.is_dir() or root.resolve() != root:
                continue
            if payload.kind == 49:
                root /= 'file'
                if not root.is_dir() or root.resolve() != root:
                    continue
            for directory, dirs, files in os.walk(root, followlinks=False):
                if not self._active() or time.monotonic() >= deadline:
                    return
                count += 1 + len(dirs) + len(files)
                if count > self.scan_paths:
                    raise ValueError('媒体路径枚举超出预算')
                current = Path(directory)
                dirs[:] = [name for name in dirs if not (current / name).is_symlink()]
                if payload.kind == 49:
                    # WeChat appends (N) before the last suffix on collisions.
                    # Candidates still require the exact size and full MD5.
                    dirs[:] = [name for name in dirs if current == root and re.fullmatch(r'[0-9]{4}-[0-9]{2}', name)]
                    if current.parent != root:
                        continue
                    title = payload.metadata.title
                    named = Path(title)
                    collision = re.compile(re.escape(named.stem) + r'\([1-9][0-9]*\)' + re.escape(named.suffix))
                    files = [name for name in files if name == title or collision.fullmatch(name)]
                for name in files:
                    if not self._active() or time.monotonic() >= deadline:
                        return
                    path = current / name
                    try:
                        info = path.lstat()
                        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
                                path.resolve(strict=True) != path):
                            continue
                        expected = {payload.metadata.size} if payload.kind == 49 else {
                            n+extra for n in (payload.metadata.length, payload.metadata.hdlength)
                            if type(n) is int and n > 0 for extra in (0,31)}
                        if info.st_size in expected:
                            if info.st_nlink == 1:
                                yield path, info, None
                            elif (payload.kind == 3 and info.st_nlink == 2 and
                                  self._image_hardlink_path(path, profile)):
                                identity = (info.st_dev, info.st_ino)
                                if identity in linked:
                                    previous, previous_info = linked.pop(identity)
                                    if self._signature(previous_info) == self._signature(info):
                                        yield previous, previous_info, path
                                else:
                                    linked[identity] = (path, info)
                    except OSError:
                        continue

    def _read(self, path, info, peer=None):
        def peer_valid():
            if peer is None:
                return info.st_nlink == 1
            peer_info = peer.lstat()
            return (stat.S_ISREG(peer_info.st_mode) and peer_info.st_uid == os.geteuid() and
                peer.resolve(strict=True) == peer and self._signature(peer_info) == self._signature(info))

        if not peer_valid():
            return None
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, 'rb') as source:
            if self._signature(os.fstat(source.fileno())) != self._signature(info):
                return None
            body = source.read(info.st_size + 1)
            if (len(body) != info.st_size or self._signature(os.fstat(source.fileno())) != self._signature(info) or
                    self._signature(path.lstat()) != self._signature(info) or path.resolve() != path or
                    not peer_valid()):
                return None
        return body

    def _key(self):
        # 身份检查包围密钥读取；密钥只在单次扫描栈中存在。
        from .linux_backend import LINUX_WECHAT_SHA256
        from .linux_contacts import VerifiedLinuxProcess
        from .linux_media import _read_cache_key
        with VerifiedLinuxProcess(self.pid, self.account, LINUX_WECHAT_SHA256) as process:
            key = _read_cache_key(self.pid)
            process.verify()
            return key

    def _find(self, payload, deadline):
        scanned, key = 0, None
        for path, info, peer in self._candidates(payload, deadline):
            if scanned + info.st_size > self.scan_bytes:
                raise ValueError('媒体读取超出预算')
            scanned += info.st_size
            try:
                body = self._read(path, info, peer)
                if body is None:
                    continue
                if hashlib.md5(body).hexdigest() == payload.metadata.md5:
                    return body
                if payload.kind == 3 and body.startswith(b'\x07\x08V2\x08\x07'):
                    if key is None:
                        if not self._active():
                            return None
                        key = self._key()
                    decoded = decode_cached_image(body, key, payload.metadata.md5)
                    if decoded is not None:
                        return decoded
            except OSError:
                continue
        return None

    def _remove(self, path, signature):
        try:
            if (path.parent in {store.directory for store in self._stores.values()} and
                    path.resolve(strict=True) == path and self._signature(path.lstat()) == signature):
                path.unlink()
        except OSError:
            pass

    def _save(self, body, message, key):
        media = 'image' if message.type == 3 else 'file'
        store = self._stores[media]
        self._private(self._root)
        self._private(self._workspace)
        self._private(store.root)
        self._private(store.directory)
        if not self._active():
            return None
        with self._lock:
            total = sum(item[1][2] for item in self._saved.values())
            while self._saved and (len(self._saved) >= self.max_files or total + len(body) > self.max_cache_bytes):
                old, (old_key, signature) = self._saved.popitem(last=False)
                self._remove(old, signature)
                total -= signature[2]
                if old_key in self._seen:
                    self._seen[old_key] = self._status(self._seen[old_key], 'unavailable', error='媒体缓存已到期')
            if self._stop.is_set():
                return None
            path = store.save(body, sequence=message.sequence,
                **({'name':message.data['file_name']} if media == 'file' else {}))
            signature = self._signature(path.stat())
            self._saved[path] = (key, signature)
        if not self._active():
            with self._lock:
                self._remove(path, signature)
                self._saved.pop(path, None)
            return None
        return path

    def _deliver(self, key, message):
        if not self._active():
            return
        with self._lock:
            if self._stop.is_set():
                return
            self._remember(key, message)
        callback = self._callback
        if callback is not None and self._active():
            try:
                callback(message)
            except Exception:
                pass

    def _process(self, key, message, payload):
        deadline = time.monotonic() + self.timeout
        try:
            requested = False
            while self._active() and time.monotonic() < deadline:
                body = self._find(payload, deadline)
                if (body is None and not requested and payload.kind == 3 and
                        self._request_image is not None and self._active()):
                    # 下载器必须预先绑定此会话。每条消息至多请求一次；下载回执
                    # 不代表内容完整，仍须在当前账号目录核对实际字节及 MD5。
                    requested = True
                    try:
                        self._request_image(message, payload.metadata)
                    except (OSError, ValueError, RuntimeError):
                        pass
                    deadline = time.monotonic() + self.timeout
                    continue
                if body is not None and self._active():
                    expected = {payload.metadata.size} if payload.kind == 49 else {
                        n for n in (payload.metadata.length, payload.metadata.hdlength) if type(n) is int and n > 0}
                    if len(body) not in expected or hashlib.md5(body).hexdigest() != payload.metadata.md5:
                        raise ValueError('媒体完整校验失败')
                    path = self._save(body, message, key)
                    if path is not None:
                        ready = self._status(message, 'ready', path=path)
                        if payload.kind == 3:
                            ready = replace(ready, data=dict(ready.data, image_integrity='md5_verified'))
                        self._deliver(key, ready)
                    return
                self._stop.wait(min(.25, max(0, deadline-time.monotonic())))
            if self._active():
                self._deliver(key, self._status(message, 'failed', error='未取得通过完整校验的媒体缓存'))
        except Exception:
            # 不转发异常正文；文件名、源路径和缓存密钥可能包含在底层异常里。
            if self._active():
                self._deliver(key, self._status(message, 'failed', error='媒体缓存读取、校验或保存失败'))

    def _cleanup(self):
        with self._lock:
            for path, (_, signature) in self._saved.items():
                self._remove(path, signature)
            self._saved.clear()
            self._seen.clear()
        for store in self._stores.values():
            for directory in (store.directory, store.root):
                try:
                    self._private(directory)
                    directory.rmdir()
                except (OSError, ValueError):
                    pass
        try:
            self._private(self._workspace)
            self._workspace.rmdir()
        except (OSError, ValueError):
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
            with self._lock:
                self._running_workers -= 1
                last = self._running_workers == 0
            if last:
                self._cleanup()

    def close(self, timeout=1):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError('媒体接收关闭等待时间无效')
        self._stop.set()
        with self._lock:
            while True:
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except queue.Empty:
                    break
        deadline = time.monotonic() + min(timeout, 60)
        for worker in self._workers:
            if threading.current_thread() is not worker:
                worker.join(max(0, deadline - time.monotonic()))
