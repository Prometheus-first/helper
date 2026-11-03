"""调用微信消息级图片下载，并以完整消息 MD5 验证本地结果。"""
from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path
import re
import threading
import time

from .image_metadata import ImageMetadata
from .linux_media import read_cached_image


class LinuxImageDownloader:
    def __init__(self, sender, *, timeout=45.0, is_current=lambda: True):
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("图片下载等待时间必须为正数")
        if not callable(is_current):
            raise ValueError("图片会话检查必须可调用")
        self.sender, self.timeout, self.is_current = sender, float(timeout), is_current
        self._stop = threading.Event()

    def _active(self):
        return not self._stop.is_set() and self.is_current()

    def __call__(self, metadata, destination):
        if not isinstance(metadata, ImageMetadata) or re.fullmatch(r"[0-9a-f]{32}", metadata.md5) is None:
            raise ValueError("图片下载需要有效的完整消息元数据")
        path = Path(destination).absolute()
        if path.exists() or path.is_symlink():
            raise ValueError("图片下载目标必须尚不存在")
        parent = path.parent.resolve(strict=True)
        if parent != path.parent or parent.stat().st_uid != os.geteuid() or parent.stat().st_mode & 0o777 != 0o700:
            raise ValueError("图片下载需要当前用户的私有目录")
        if not self._active():
            return False
        if not self.sender.image_account:
            raise RuntimeError("图片下载范围尚未绑定")
        if not self.sender.wait_image_context(metadata.md5, timeout=min(10, self.timeout), is_current=self._active):
            return False
        self.sender.download_message_image(metadata.md5)
        capture = {"attributes": {name: str(value) for name in ("md5", "length", "hdlength") if (value := getattr(metadata, name)) is not None}}
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline and self._active():
            body = read_cached_image(capture, self.sender.image_account, self.sender.expected_pid)
            if body is not None and hashlib.md5(body).hexdigest() == metadata.md5:
                if not self._active():
                    return False
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as output:
                    output.write(body)
                return True
            self._stop.wait(.1)
        if not self._active():
            return False
        raise TimeoutError("微信未在规定时间内提供校验通过的完整图片")

    def close(self):
        self._stop.set()
