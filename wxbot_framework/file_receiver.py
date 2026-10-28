"""受控文件接收队列；下载器由调用方提供，不执行或自动打开收到的文件。"""
from __future__ import annotations

import math
from pathlib import Path
import queue
import shutil
import tempfile
import threading

from .file_metadata import FileMetadata, FileMetadataError, FileStore, parse_file_metadata
from .files import _check_local_path, safe_file_name


class FileReceiver:
    """单线程异步下载与原子保存；仅向仍然有效的账号会话投递结果。

    downloader(metadata, destination) 在返回前关闭文件，成功返回 True。
    声明的 size/md5 必须匹配；未声明 md5 只能验证大小，不能证明内容来源。
    """

    def __init__(self, store: FileStore, downloader, deliver, is_current, queue_size=16):
        if not isinstance(store, FileStore):
            raise ValueError("文件接收器需要 FileStore")
        if not all(callable(value) for value in (downloader, deliver, is_current)):
            raise ValueError("文件下载、投递和会话检查必须为可调用对象")
        if type(queue_size) is not int or not 1 <= queue_size <= 1024:
            raise ValueError("文件接收队列容量必须为 1 到 1024 的整数")
        self.store = store
        self._root = Path(store.root)
        self._directory = Path(store.directory)
        if not self._root.is_absolute() or self._directory.parent != self._root:
            raise ValueError("文件账号目录必须是保存根目录的直接子目录")
        self._downloader = downloader
        self._deliver_callback = deliver
        self._is_current = is_current
        self._queue = queue.Queue(maxsize=queue_size)
        self._enqueue_lock = threading.Lock()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._run, name="wechat-file-receiver", daemon=True)
        self._worker.start()

    @property
    def is_alive(self):
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
                return False
            return True
        return False

    def _failed(self, data, message):
        data.update(file_status="failed", file_path="", file_error=message)
        self._deliver(data)

    @staticmethod
    def _snapshot(data):
        snapshot = dict(data)
        # 文件 XML 内带 CDN 凭据。投递给插件的事件不再携带原始 XML；如正文
        # 本身也是 XML 也清空，避免日志/插件把密钥扩散到其他处理链。
        raw = snapshot.pop("raw_content", None)
        # 文件事件的正文由 file_name/file_path 表达，不传播任何旧 XML 副本。
        for name in ("content", "msg"):
            if name in snapshot:
                snapshot[name] = ""
        snapshot.update(file_path="", file_error="", file_name="", file_size=None,
                        file_metadata={})
        return snapshot, raw

    def submit(self, data: dict) -> bool:
        """非阻塞入队；非法元数据或满队列会投递不含秘密的 failed 事件。"""
        if not isinstance(data, dict):
            raise ValueError("文件事件必须为字典")
        if not self._active():
            return False
        snapshot, raw = self._snapshot(data)
        try:
            metadata = parse_file_metadata(raw)
            if metadata is None:
                return False
            name = safe_file_name(metadata.title)
        except (FileMetadataError, ValueError):
            self._failed(snapshot, "文件元数据无效或超出支持范围")
            return False
        snapshot.update(file_name=name, file_size=metadata.size,
                        file_metadata={"title": name, "size": metadata.size,
                                       "md5": metadata.md5})
        full = False
        with self._enqueue_lock:
            if self._stop.is_set():
                return False
            try:
                self._queue.put_nowait((snapshot, metadata))
            except queue.Full:
                full = True
        if full:
            self._failed(snapshot, "文件接收队列已满")
            return False
        return True

    def _new_workspace(self):
        self._root.mkdir(parents=True, exist_ok=True)
        self._directory.mkdir(exist_ok=True)
        if (self._root.resolve(strict=True) != self._root or
                self._directory.resolve(strict=True) != self._directory or
                not self._directory.is_dir()):
            raise ValueError("文件账号目录不能通过链接重定向")
        workspace = Path(tempfile.mkdtemp(prefix=".receive-", dir=self._directory))
        try:
            if workspace.parent != self._directory or workspace.resolve(strict=True) != workspace:
                raise ValueError("文件下载临时目录不在当前账号目录内")
            _check_local_path(workspace / "file.bin")
            return workspace
        except BaseException:
            self._remove_workspace(workspace)
            raise

    def _remove_workspace(self, workspace):
        if (workspace.parent == self._directory and workspace.is_absolute() and
                workspace.exists() and workspace.resolve(strict=True) == workspace):
            shutil.rmtree(workspace)

    def _download(self, metadata: FileMetadata, data):
        workspace = self._new_workspace()
        try:
            destination = workspace / "file.bin"
            if destination.exists() or destination.is_symlink():
                raise ValueError("文件下载目标必须尚不存在")
            if not self._active():
                return None
            if self._downloader(metadata, destination) is not True:
                raise ValueError("文件下载未成功")
            if not self._active():
                return None
            return self.store.save_download(destination, name=data["file_name"], sequence=data.get("sequence"),
                                            expected_size=metadata.size, expected_md5=metadata.md5,
                                            is_current=self._active)
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
            result = self._download(metadata, data)
            if result is None:
                return
            path, size = result
            if not self._active():
                self._remove_saved(path)
                return
            data.update(file_status="ready", file_path=str(path), file_size=size, file_error="")
            if not self._deliver(data):
                self._remove_saved(path)
        except Exception:
            # 下载器/文件系统异常可能含 URL、密钥或原 XML，禁止直接转发。
            self._failed(data, "文件下载、校验或保存失败")

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
        """取消待处理事件；有限等待中若下载器未返回，is_alive 仍为 True。"""
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or
                not math.isfinite(timeout) or timeout < 0):
            raise ValueError("文件接收器关闭等待时间必须为有限非负数")
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
