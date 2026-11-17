"""受控图片接收队列；下载能力由调用方提供，本模块不连接微信或网络。"""
from __future__ import annotations

import math
import os
from pathlib import Path
import queue
import shutil
import stat
import tempfile
import threading

from .image_metadata import (ImageMetadata, ImageMetadataError, ImageStore,
                             parse_image_metadata, select_image_download)
from .images import MAX_IMAGE_BYTES


_PUBLIC_FIELDS = (
    "md5", "width", "height", "length", "hdlength", "cdnthumbwidth", "cdnthumbheight",
    "cdnthumblength", "cdnmidwidth", "cdnmidheight", "encryver",
)


class ImageReceiver:
    """异步下载和保存图片，每个接收器只有一个下载工作线程。

    downloader(metadata, destination) 应在返回前关闭输出文件，成功返回 True。
    destination 是新建临时目录内尚不存在的文件，不能由 XML 内容决定。
    只有当前会话会收到 ready/failed 事件；原始事件复制后再填入图片状态。
    """

    def __init__(self, store: ImageStore, downloader, deliver, is_current, queue_size=16):
        if not isinstance(store, ImageStore):
            raise ValueError("图片接收器需要 ImageStore")
        if not all(callable(value) for value in (downloader, deliver, is_current)):
            raise ValueError("图片下载、投递和会话检查必须为可调用对象")
        if type(queue_size) is not int or not 1 <= queue_size <= 1024:
            raise ValueError("图片接收队列容量必须为 1 到 1024 的整数")
        self.store = store
        self._root = Path(store.root)
        self._directory = Path(store.directory)
        if not self._root.is_absolute() or self._directory.parent != self._root:
            raise ValueError("图片账号目录必须是保存根目录的直接子目录")
        self._downloader = downloader
        self._deliver_callback = deliver
        self._is_current = is_current
        self._queue = queue.Queue(maxsize=queue_size)
        self._enqueue_lock = threading.Lock()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="wechat-image-receiver", daemon=True)
        self._worker.start()

    @property
    def is_alive(self):
        """关闭超时后仍可能在等待 downloader；原生资源此时仍不能卸载。"""
        return self._worker.is_alive()

    def _active(self):
        if self._stop.is_set():
            return False
        try:
            current = bool(self._is_current())
        except Exception:
            return False
        return current and not self._stop.is_set()

    def _deliver(self, data):
        if self._active():
            try:
                self._deliver_callback(data)
            except Exception:
                # 回调错误不能让下载线程退出，也不能把带秘密的异常写入日志。
                return False
            return True
        return False

    def _failed(self, data, message):
        data.update(image_status="failed", image_path="", image_error=message)
        self._deliver(data)

    def submit(self, data: dict) -> bool:
        """非阻塞入队；元数据无效或队列已满时投递可观察失败并返回 False。"""
        if not isinstance(data, dict):
            raise ValueError("图片事件必须为字典")
        if not self._active():
            return False
        snapshot = dict(data)
        snapshot.update(image_path="", image_error="", image_metadata={}, image_quality="unknown")
        try:
            metadata = parse_image_metadata(snapshot.get("raw_content"))
        except ImageMetadataError:
            self._failed(snapshot, "图片元数据无效")
            return False
        snapshot["image_metadata"] = {name: getattr(metadata, name) for name in _PUBLIC_FIELDS}
        snapshot["image_quality"] = select_image_download(metadata)[3]
        full = False
        with self._enqueue_lock:
            if self._stop.is_set():
                return False
            try:
                self._queue.put_nowait((snapshot, metadata))
            except queue.Full:
                full = True
        if full:
            self._failed(snapshot, "图片接收队列已满")
            return False
        return True

    def _new_workspace(self):
        # 不调用 ImageStore 私有方法；下载临时目录同样先检查账号边界。
        self._root.mkdir(parents=True, exist_ok=True)
        self._directory.mkdir(exist_ok=True)
        if (self._root.resolve(strict=True) != self._root or
                self._directory.resolve(strict=True) != self._directory or
                not self._directory.is_dir()):
            raise ValueError("图片账号目录不能通过链接重定向")
        workspace = Path(tempfile.mkdtemp(prefix=".receive-", dir=self._directory))
        if workspace.parent != self._directory or workspace.resolve(strict=True) != workspace:
            raise ValueError("图片下载临时目录不在当前账号目录内")
        return workspace

    def _remove_workspace(self, workspace):
        # 删除前核对绝对路径，避免下载器改变目录链接后递归操作账号目录外的文件。
        if (workspace.parent == self._directory and workspace.is_absolute() and
                workspace.resolve(strict=True) == workspace):
            shutil.rmtree(workspace)

    @staticmethod
    def _read_download(destination):
        information = destination.lstat()
        if (not stat.S_ISREG(information.st_mode) or destination.resolve(strict=True) != destination or
                not 0 < information.st_size <= MAX_IMAGE_BYTES):
            raise ValueError("图片下载结果必须为范围内的普通文件")
        with destination.open("rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode) or not 0 < opened.st_size <= MAX_IMAGE_BYTES:
                raise ValueError("图片下载结果必须为范围内的普通文件")
            body = source.read(MAX_IMAGE_BYTES + 1)
        if not 0 < len(body) <= MAX_IMAGE_BYTES:
            raise ValueError("图片下载结果必须为非空且不超过 20 MiB 的文件")
        return body

    def _download(self, metadata: ImageMetadata):
        workspace = self._new_workspace()
        try:
            destination = workspace / "image.bin"
            if destination.exists() or destination.is_symlink():
                raise ValueError("图片下载目标必须尚不存在")
            if not self._active():
                return None
            if self._downloader(metadata, destination) is not True:
                raise ValueError("图片下载未成功")
            if not self._active():
                return None
            return self._read_download(destination)
        finally:
            self._remove_workspace(workspace)

    def _remove_saved(self, path):
        try:
            if path.parent == self._directory and path.resolve(strict=True) == path:
                path.unlink()
        except (OSError, RuntimeError):
            pass

    def _process(self, data, metadata):
        if not self._active():
            return
        try:
            body = self._download(metadata)
            if body is None or not self._active():
                return
            path = self.store.save(body, sequence=data.get("sequence"))
            if not self._active():
                self._remove_saved(path)
                return
            data.update(image_status="ready", image_path=str(path), image_error="")
            if not self._deliver(data):
                self._remove_saved(path)
        except Exception:
            # 下载器和文件系统异常可能包含 CDN URL、密钥或 XML，不能直接转发。
            reason = ("原图下载或保存失败，未自动切换普通图"
                      if data.get("image_quality") == "original" else "图片下载或保存失败")
            self._failed(data, reason)

    def _run(self):
        while not self._stop.is_set():
            try:
                data, metadata = self._queue.get(timeout=0.05)
            except queue.Empty:
                continue
            try:
                self._process(data, metadata)
            finally:
                self._queue.task_done()

    def close(self, timeout=1):
        """取消待处理事件，有限等待工作线程；下载器未返回时 is_alive 仍为 True。"""
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                not math.isfinite(timeout) or timeout < 0):
            raise ValueError("图片接收器关闭等待时间必须为有限非负数")
        self._stop.set()
        with self._enqueue_lock:
            while True:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    break
                self._queue.task_done()
        if threading.current_thread() is not self._worker:
            self._worker.join(timeout=min(timeout, 60))
