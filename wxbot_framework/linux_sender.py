"""Linux 实验性原生发送；文本及本地图片、文件已完成受控实机验证。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time


class LinuxSendError(RuntimeError):
    pass


class LinuxSendIndeterminate(LinuxSendError):
    """请求可能已经发送，禁止自动重试。"""


class LinuxNativeSender:
    def __init__(self, *, allowed_targets=("filehelper",), directory=None, timeout=50.0):
        if not hasattr(socket, "SO_PEERCRED"):
            raise RuntimeError("Linux 原生发送需要 SO_PEERCRED")
        if isinstance(allowed_targets, (str, bytes)):
            raise ValueError("发送会话白名单必须是会话列表")
        targets = frozenset(allowed_targets)
        if not targets or any(not isinstance(t, str) or not t or "\0" in t for t in targets):
            raise ValueError("发送会话白名单无效")
        self.allowed_targets = targets
        self._directory = Path(tempfile.mkdtemp(prefix="helper-send-", dir=directory))
        self._directory.chmod(0o700)
        self.path = self._directory / "send.sock"
        if len(os.fsencode(self.path)) >= 108:
            self._directory.rmdir()
            raise ValueError("本地发送 socket 路径过长")
        self.expected_pid = None
        self._lock = threading.RLock()
        self._closed = False
        self._context_ready = False
        self.features = frozenset()
        self.image_account = None
        self.image_context_status = {}
        self.timeout = max(0.1, float(timeout))

    def environment(self):
        return {"HELPER_LINUX_SEND_BRIDGE": "1", "HELPER_LINUX_SEND_SOCKET": str(self.path),
                "HELPER_LINUX_CONTROLLER_PID": str(os.getpid())}

    def bind_pid(self, pid):
        if type(pid) is not int or pid <= 0:
            raise ValueError("目标 PID 无效")
        if self.expected_pid is not None and self.expected_pid != pid:
            raise RuntimeError("发送连接已经绑定微信进程")
        self.expected_pid = pid

    def _request(self, command, body=b""):
        if self._closed or self.expected_pid is None:
            raise LinuxSendError("Linux 原生发送连接未运行")
        attempted = False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.settimeout(self.timeout)
                conn.connect(str(self.path))
                pid, uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if pid != self.expected_pid or uid != os.geteuid():
                    raise PermissionError("拒绝非目标微信进程的发送连接")
                attempted = command in (b"S", b"G", b"I", b"F", b"D", b"A", b"K")
                conn.sendall(command + (struct.pack("!I", len(body)) + body if command in (b"S", b"G", b"I", b"F", b"D", b"A", b"K") else b""))
                chunks = []
                count = 0
                while True:
                    part = conn.recv(4096)
                    if not part:
                        raise EOFError("原生发送回调连接中断")
                    chunks.append(part)
                    count += len(part)
                    if count > 65536:
                        raise ValueError("原生发送回调格式异常")
                    if b"\n" in part:
                        break
                response = json.loads(b"".join(chunks))
                if not isinstance(response, dict):
                    raise ValueError("原生发送回调格式异常")
                return response
        except (OSError, EOFError, ValueError) as error:
            if attempted:
                raise LinuxSendIndeterminate("图片下载结果未知，不会自动重试" if command == b"D" else "发送结果未知，消息可能已送出；不会自动重试") from error
            raise

    def wait_ready(self, *, timeout=5.0, process=None):
        deadline = time.monotonic() + timeout
        while True:
            if process is not None and process.poll() is not None:
                raise LinuxSendError("Linux 微信在发送桥接就绪前退出")
            try:
                with self._lock:
                    response = self._request(b"H")
                if response.get("ready") is True and response.get("protocol") == 1:
                    self.features = frozenset(name for name in ("generic_messages", "local_media", "image_download", "image_context") if response.get(name) is True)
                    return True
                raise LinuxSendError("原生发送桥接握手不匹配")
            except (FileNotFoundError, ConnectionRefusedError, socket.timeout):
                if time.monotonic() >= deadline:
                    raise LinuxSendError("Linux 原生发送桥接未就绪") from None
                time.sleep(0.05)

    @staticmethod
    def _result(response, *, sending=False):
        error_type = LinuxSendIndeterminate if sending else LinuxSendError
        if response.get("pending") or response.get("callback") is not True:
            raise error_type(response.get("error") or "原生回调未完成；不会自动重试")
        if response.get("error"):
            raise LinuxSendError(str(response["error"]))
        try:
            result = json.loads(response.get("result", ""))
        except (TypeError, ValueError) as error:
            raise error_type("原生发送回调内容无效；不会自动重试") from error
        if not isinstance(result, dict) or result.get("success") is not True:
            raise LinuxSendError(result.get("error_message", "微信拒绝发送") if isinstance(result, dict) else "原生回调内容无效")
        return result

    def ensure_context(self):
        with self._lock:
            result = self._result(self._request(b"N"))
            if result.get("native_coroutine") is not True:
                raise LinuxSendError("尚未确认微信原生协程上下文")
            self._context_ready = True
            return True

    def send_text(self, chat_id, text):
        if not isinstance(chat_id, str) or chat_id not in self.allowed_targets:
            raise LinuxSendError("该会话尚未获准使用 Linux 实验性文本发送")
        if not isinstance(text, str) or not text or "\0" in text:
            raise ValueError("文本必须非空且不能包含 NUL")
        body = json.dumps({"chat_id": chat_id, "content": text}, ensure_ascii=False).encode("utf-8")
        if len(body) > 0xffffffff:
            raise ValueError("发送请求超过本地传输协议的长度范围")
        with self._lock:
            if not self._context_ready:
                self.ensure_context()
            self._result(self._request(b"S", body), sending=True)
            return True

    def send_generic(self, payload):
        """实验入口：使用微信内置通用消息接口，不在普通插件中默认公开。"""
        if "generic_messages" not in self.features:
            raise LinuxSendError("当前原生桥接未开放通用消息接口")
        if not isinstance(payload, dict) or payload.get("chat_id") not in self.allowed_targets:
            raise LinuxSendError("该通用消息会话未获授权")
        if type(payload.get("message_type")) is not int:
            raise ValueError("message_type 必须是整数")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        if len(body) > 0xffffffff:
            raise ValueError("请求超过传输协议长度范围")
        with self._lock:
            if not self._context_ready:
                self.ensure_context()
            return self._result(self._request(b"G", body), sending=True)

    def _send_media(self, command, chat_id, path):
        if "local_media" not in self.features:
            raise LinuxSendError("当前原生桥接未开放本地媒体发送")
        if not isinstance(chat_id, str) or chat_id not in self.allowed_targets:
            raise LinuxSendError("该媒体会话未获授权")
        from .text import validate_target
        validate_target(chat_id)
        file = Path(path).resolve(strict=True)
        if not file.is_file() or file.stat().st_size <= 0:
            raise ValueError("媒体文件必须是非空的普通文件")
        body = (chat_id + "\n" + os.fsencode(file).hex()).encode("ascii")
        if len(body) > 0xffffffff:
            raise ValueError("请求超过传输协议长度范围")
        with self._lock:
            if not self._context_ready:
                self.ensure_context()
            self._result(self._request(command, body), sending=True)
            return True

    def send_image(self, chat_id, path):
        return self._send_media(b"I", chat_id, path)

    def send_file(self, chat_id, path):
        return self._send_media(b"F", chat_id, path)

    def bind_image_scope(self, peer, account):
        from .account import validate_account
        if "image_context" not in self.features:
            raise LinuxSendError("当前桥接不支持消息级图片下载")
        validate_account(peer);validate_account(account)
        with self._lock:
            if not self._context_ready:
                self.ensure_context()
            self._result(self._request(b"A", (peer + "\n" + account).encode("ascii")))
            self.image_account = account
        return True

    def image_context_available(self, md5):
        import re
        if "image_context" not in self.features:
            raise LinuxSendError("当前桥接不支持图片消息上下文")
        if not isinstance(md5, str) or re.fullmatch(r"[0-9a-f]{32}", md5) is None:
            raise ValueError("图片消息校验值无效")
        with self._lock:
            if not self._context_ready:
                self.ensure_context()
            result = self._result(self._request(b"K", md5.encode("ascii")))
            self.image_context_status = {key: result[key] for key in ("available", "scoped_seen", "cached") if key in result}
            return result.get("available") is True

    def wait_image_context(self, md5, *, timeout=10, is_current=lambda: True):
        deadline = time.monotonic() + timeout
        while is_current():
            if self.image_context_available(md5):
                return True
            if time.monotonic() >= deadline:
                raise TimeoutError("尚未取得匹配的原生图片消息上下文")
            time.sleep(.1)
        return False

    def download_message_image(self, md5):
        import re
        if "image_context" not in self.features or not self.image_account:
            raise LinuxSendError("消息级图片下载范围尚未绑定")
        if not isinstance(md5, str) or re.fullmatch(r"[0-9a-f]{32}", md5) is None:
            raise ValueError("图片消息校验值无效")
        with self._lock:
            result = self._result(self._request(b"D", md5.encode("ascii")), sending=True)
            if result.get("native_completed") is not True:
                raise LinuxSendError("微信没有返回消息级图片下载完成回执")
        return True

    def close(self):
        with self._lock:
            if self._closed:
                return
            try:
                if self.expected_pid is not None and self.path.exists():
                    self._request(b"Q")
            except (OSError, EOFError, ValueError, LinuxSendError):
                pass
            finally:
                self._closed = True
                self._context_ready = False
            # 原生服务关闭自己的 socket；这里仅尝试移除已空的私有目录。
            try:
                self._directory.rmdir()
            except OSError:
                pass
