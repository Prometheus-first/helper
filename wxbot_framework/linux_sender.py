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

MAX_RETAINED_FILE_COUNT = 64
MAX_RETAINED_FILE_BYTES = 64 * 1024 * 1024


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
        self.image_scope_generation = None
        self.image_context_status = {}
        self._bound_account = None
        self._pending_emoticon = None
        self._pending_voice = None
        self._retained_files = {}
        self._retained_file_bytes = 0
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
                attempted = command in (b"S", b"M", b"G", b"I", b"F", b"O", b"W", b"D", b"U", b"A", b"B", b"C", b"X", b"K")
                conn.sendall(command + (struct.pack("!I", len(body)) + body if command in (b"S", b"M", b"G", b"I", b"F", b"O", b"W", b"D", b"A", b"B", b"C", b"X", b"K", b"E") else b""))
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
                raise LinuxSendIndeterminate("图片下载结果未知，不会自动重试" if command in (b"D", b"U") else "发送结果未知，消息可能已送出；不会自动重试") from error
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
                    self.features = frozenset(name for name in ("generic_messages", "local_media", "local_emoticons", "local_voice", "image_download", "image_context", "image_scope_groups", "group_mentions") if response.get(name) is True)
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

    def current_profile(self):
        """从已校验的微信进程读取登录状态，不依赖收到消息。"""
        from .account import validate_account
        with self._lock:
            result = self._request(b"P")
        if result.get("logged_in") is not True:
            return {"logged_in": False, "wxid": "", "name": ""}
        account = validate_account(result.get("wxid"))
        name = result.get("name", "")
        if not isinstance(name, str) or len(name) > 128 or any(ord(ch) < 32 for ch in name):
            name = ""
        return {"logged_in": True, "wxid": account, "name": name}

    def bind_account(self, account):
        from .account import validate_account
        account = validate_account(account)
        with self._lock:
            self._bound_account = None
            self._result(self._request(b"E", account.encode("ascii")))
            self._bound_account = account
            self._context_ready = False
        return True

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

    def send_at_text(self, chat_id, text, at_list):
        from .text import validate_mentions
        members = validate_mentions(chat_id, at_list)
        if 'notify@all' in members:
            raise ValueError('Linux 群艾特仅支持指定成员，不支持艾特所有人')
        if chat_id not in self.allowed_targets:
            raise LinuxSendError("该群尚未获准发送消息")
        if "group_mentions" not in self.features:
            raise LinuxSendError("当前 Linux 桥接不支持群艾特，请重新编译并重启微信会话")
        if not isinstance(text, str) or not text or "\0" in text:
            raise ValueError("文本必须非空且不能包含 NUL")
        encoded = text.encode("utf-8")
        if len(encoded) > 1023:
            raise ValueError("单段艾特文本最多 1023 个 UTF-8 字节，请通过框架发送长文本")
        body = chat_id.encode("ascii") + b"\n" + ",".join(members).encode("ascii") + b"\n" + encoded
        with self._lock:
            if not self._context_ready:
                self.ensure_context()
            self._result(self._request(b"M", body), sending=True)
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
        """Retain the exact named input until the native process has exited.

        The message-manager result is not an upload-completion receipt. Its
        asynchronous file stages may still open the original path afterward.
        Never evict a submitted snapshot to make space for another request.
        """
        from .emoticons import _read_bounded
        from .files import inspect_file
        from .text import validate_target
        validate_target(chat_id)
        with self._lock:
            if self._closed:
                raise LinuxSendError('Linux 原生发送连接未运行')
            if 'local_media' not in self.features or chat_id not in self.allowed_targets:
                raise LinuxSendError('该会话尚未获准使用原生文件发送')
            if len(self._retained_files) >= MAX_RETAINED_FILE_COUNT:
                raise LinuxSendError('待确认文件快照达到 64 条上限，不会清除可能仍在上传的文件')
            remaining = MAX_RETAINED_FILE_BYTES - self._retained_file_bytes
            # Preserve the existing missing-path error of local media sends.
            source = inspect_file(Path(path).resolve(strict=True))
            if not 0 < source.size <= remaining:
                raise LinuxSendError('待确认文件快照超过 64 MiB 总预算，不会清除可能仍在上传的文件')
            body = _read_bounded(source.path, remaining)
            directory = Path(tempfile.mkdtemp(prefix='file-', dir=self._directory))
            directory.chmod(0o700)
            snapshot = directory / source.path.name
            keep = False
            try:
                fd = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(body)
                if not self._context_ready:
                    self.ensure_context()
                # Even a failed manager stage does not prove that every native
                # file task has released its path. Once submitted, retain it.
                keep = True
                return self._send_media(b'F', chat_id, snapshot)
            finally:
                if keep:
                    self._retained_files[snapshot] = len(body)
                    self._retained_file_bytes += len(body)
                else:
                    snapshot.unlink(missing_ok=True)
                    directory.rmdir()

    def cleanup_retained_files(self):
        """Clean only this sender's snapshots after its bound PID has exited."""
        with self._lock:
            return self._cleanup_file_snapshots()

    def _cleanup_file_snapshots(self):
        # Q closes only our socket; it does not cancel WeChat uploads. A live
        # (or uncheckable) PID is not sufficient evidence to unlink their input.
        if self.expected_pid is None:
            return False
        try:
            os.kill(self.expected_pid, 0)
            return False
        except ProcessLookupError:
            pass
        except OSError:
            return False
        for snapshot, size in tuple(self._retained_files.items()):
            try:
                if (snapshot.parent.parent != self._directory or
                        snapshot.parent.resolve(strict=True) != snapshot.parent):
                    continue
                snapshot.unlink(missing_ok=True)
                snapshot.parent.rmdir()
            except OSError:
                continue
            del self._retained_files[snapshot]
            self._retained_file_bytes -= size
        try:
            self._directory.rmdir()
        except OSError:
            pass
        return not self._retained_files

    def send_emoticon(self, chat_id, path):
        """Send a bounded GIF as native type 47, preserving its original bytes.

        The native operation waits for the message-manager future. If its reply
        is lost, keep one private snapshot and refuse more stickers on this
        sender: neither retry nor unlink a file the coroutine may still use.
        """
        from .emoticons import MAX_EMOTICON_BYTES, _read_bounded, validate_emoticon_path
        from .text import validate_target
        validate_target(chat_id)
        with self._lock:
            if self._closed:
                raise LinuxSendError("Linux 原生发送连接未运行")
            if "local_emoticons" not in self.features:
                raise LinuxSendError("当前 Linux 桥接不支持本地表情，请重新编译并重启微信会话")
            if chat_id not in self.allowed_targets:
                raise LinuxSendError("该表情会话未获授权")
            if not self._bound_account:
                raise LinuxSendError("发送表情前必须绑定当前登录账号")
            if self._pending_emoticon is not None:
                raise LinuxSendIndeterminate("上次表情发送结果未知；本会话不会重试或继续发送表情")
            source = validate_emoticon_path(path)
            data = _read_bounded(source, MAX_EMOTICON_BYTES)
            fd, name = tempfile.mkstemp(prefix="emoticon-", suffix=".gif", dir=self._directory)
            snapshot = Path(name)
            keep = False
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                # Validate the exact immutable copy sent, even if source changed.
                validate_emoticon_path(snapshot)
                if not self._context_ready:
                    self.ensure_context()
                body = (chat_id + "\n" + os.fsencode(snapshot).hex()).encode("ascii")
                try:
                    self._result(self._request(b"O", body), sending=True)
                except LinuxSendIndeterminate:
                    self._pending_emoticon = snapshot
                    keep = True
                    raise
                return True
            finally:
                if not keep:
                    snapshot.unlink(missing_ok=True)

    def send_voice(self, chat_id, path):
        from .emoticons import _read_bounded
        from .voices import prepare_voice, _inspect_silk
        from .voice_receiver import MAX_RECEIVED_VOICE_BYTES
        from .text import validate_target
        validate_target(chat_id)
        with self._lock:
            if self._closed or 'local_voice' not in self.features:
                raise LinuxSendError('当前 Linux 桥接不支持语音发送，请更新并重启微信')
            if chat_id not in self.allowed_targets or not self._bound_account:
                raise LinuxSendError('语音发送需要已绑定账号和明确会话授权')
            if self._pending_voice is not None:
                raise LinuxSendIndeterminate('上次语音发送结果未知，本连接不会重试或继续发送语音')
            if (len(self._retained_files) >= MAX_RETAINED_FILE_COUNT or
                    self._retained_file_bytes + MAX_RECEIVED_VOICE_BYTES > MAX_RETAINED_FILE_BYTES):
                raise LinuxSendError('发送快照预算已满')
            directory = Path(tempfile.mkdtemp(prefix='voice-', dir=self._directory))
            snapshot = directory / 'audio.silk'
            keep = False
            try:
                with prepare_voice(path, directory) as prepared:
                    data = _read_bounded(prepared.path, MAX_RECEIVED_VOICE_BYTES)
                parsed = _inspect_silk(data)
                fd = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(data)
                if not self._context_ready:
                    self.ensure_context()
                body = (chat_id + '\n' + str(parsed.duration_ms) + '\n' + os.fsencode(snapshot).hex()).encode('ascii')
                try:
                    self._result(self._request(b'W', body), sending=True)
                except LinuxSendIndeterminate:
                    self._pending_voice = snapshot
                    self._retained_files[snapshot] = len(data)
                    self._retained_file_bytes += len(data)
                    keep = True
                    raise
                return True
            finally:
                if not keep:
                    snapshot.unlink(missing_ok=True)
                    directory.rmdir()

    def bind_image_scope(self, peer, account):
        from .account import validate_account
        if "image_context" not in self.features:
            raise LinuxSendError("当前桥接不支持消息级图片下载")
        validate_account(peer);validate_account(account)
        with self._lock:
            if not self._context_ready:
                self.ensure_context()
            result = self._result(self._request(b"A", (peer + "\n" + account).encode("ascii")))
            self.image_account = account
            self.image_scope_generation = result.get('scope_generation')
        return True

    def bind_image_scopes(self, targets, account):
        """Arm exact incoming peers/rooms before reception; no wildcard scope."""
        from .account import validate_account
        from .text import validate_target
        account = validate_account(account)
        if isinstance(targets, (str, bytes)):
            raise ValueError('图片范围必须是 1–64 个明确会话')
        targets = tuple(targets)
        if not 1 <= len(targets) <= 64 or len(set(targets)) != len(targets):
            raise ValueError('图片范围必须是 1–64 个不重复的明确会话')
        for target in targets:
            validate_target(target)
            if '@' in target and (not target.endswith('@chatroom') or target.count('@') != 1 or target == '@chatroom'):
                raise ValueError('图片范围中的群 ID 无效')
        with self._lock:
            if 'image_scope_groups' not in self.features:
                raise LinuxSendError('当前桥接不支持群图片下载范围，请重新编译并重启微信会话')
            if not self._context_ready:
                self.ensure_context()
            body = ('\n'.join((account, *sorted(targets)))).encode('ascii')
            result = self._result(self._request(b'C', body))
            generation = result.get('scope_generation')
            if type(generation) is not int or not 0 < generation <= 2**64-1:
                raise LinuxSendError('图片范围回执缺少有效代号')
            self.image_account = account
            self.image_scope_generation = generation
            return generation

    def bind_all_image_scopes(self, account):
        """Arm regular incoming conversations when the configured allowlist is empty."""
        from .account import validate_account
        account = validate_account(account)
        with self._lock:
            if 'image_scope_groups' not in self.features:
                raise LinuxSendError('当前桥接不支持群图片下载范围，请重新编译并重启微信会话')
            if not self._context_ready:
                self.ensure_context()
            result = self._result(self._request(b'C', (account + '\n*').encode('ascii')))
            generation = result.get('scope_generation')
            if type(generation) is not int or not 0 < generation <= 2**64-1:
                raise LinuxSendError('图片范围回执缺少有效代号')
            self.image_account = account
            self.image_scope_generation = generation
            return generation

    def clear_image_scope(self, scope_generation):
        """Only cancel the lease that owns this generation, never a newer one."""
        if type(scope_generation) is not int or not 0 < scope_generation <= 2**64-1:
            raise ValueError('图片范围代号无效')
        with self._lock:
            if 'image_scope_groups' not in self.features:
                raise LinuxSendError('当前桥接不支持清除图片下载范围')
            result = self._result(self._request(b'X', str(scope_generation).encode('ascii')))
            if self.image_scope_generation == scope_generation:
                self.image_scope_generation = None
                self.image_account = None
                self.image_context_status = {}
            return result.get('cleared') is True

    def bind_voice_test_scope(self, peer, account):
        """同时保留该测试账号的第一条语音上下文，仅供受控验证。"""
        from .account import validate_account
        validate_account(peer); validate_account(account)
        if "image_context" not in self.features:
            raise LinuxSendError("当前桥接不支持消息上下文")
        with self._lock:
            if not self._context_ready:
                self.ensure_context()
            result = self._result(self._request(b"B", (peer + "\n" + account).encode("ascii")))
            self.image_account = account
            self.image_scope_generation = result.get('scope_generation')
        return True

    def voice_test_context_available(self):
        with self._lock:
            return self._result(self._request(b"T")).get("available") is True

    def download_test_voice(self):
        if not self.image_account:
            raise LinuxSendError("测试会话尚未绑定")
        with self._lock:
            result = self._result(self._request(b"U"), sending=True)
            if result.get("native_completed") is not True:
                raise LinuxSendError("微信没有返回语音下载完成回执")
        return True

    def _image_query(self, md5, scope_generation):
        import re
        if not isinstance(md5, str) or re.fullmatch(r'[0-9a-f]{32}', md5) is None:
            raise ValueError('图片消息校验值无效')
        if scope_generation is None:
            scope_generation = self.image_scope_generation
        if scope_generation is not None:
            if type(scope_generation) is not int or not 0 < scope_generation <= 2**64-1:
                raise ValueError('图片范围代号无效')
            if scope_generation != self.image_scope_generation:
                raise LinuxSendError('图片下载范围已变化')
            return f'{scope_generation}\n{md5}'.encode('ascii')
        return md5.encode('ascii')

    def image_context_available(self, md5, *, scope_generation=None):
        import re
        if "image_context" not in self.features:
            raise LinuxSendError("当前桥接不支持图片消息上下文")
        if not isinstance(md5, str) or re.fullmatch(r"[0-9a-f]{32}", md5) is None:
            raise ValueError("图片消息校验值无效")
        with self._lock:
            body = self._image_query(md5, scope_generation)
            if not self._context_ready:
                self.ensure_context()
            result = self._result(self._request(b"K", body))
            self.image_context_status = {key: result[key] for key in ("available", "scoped_seen", "cached") if key in result}
            return result.get("available") is True

    def wait_image_context(self, md5, *, timeout=10, is_current=lambda: True, scope_generation=None):
        with self._lock:
            if scope_generation is None:
                scope_generation = self.image_scope_generation
        deadline = time.monotonic() + timeout
        while is_current():
            if self.image_context_available(md5, scope_generation=scope_generation):
                return True
            if time.monotonic() >= deadline:
                raise TimeoutError("尚未取得匹配的原生图片消息上下文")
            time.sleep(.1)
        return False

    def download_message_image(self, md5, *, scope_generation=None):
        import re
        if "image_context" not in self.features or not self.image_account:
            raise LinuxSendError("消息级图片下载范围尚未绑定")
        if not isinstance(md5, str) or re.fullmatch(r"[0-9a-f]{32}", md5) is None:
            raise ValueError("图片消息校验值无效")
        with self._lock:
            result = self._result(self._request(b"D", self._image_query(md5, scope_generation)), sending=True)
            if result.get("native_completed") is not True:
                raise LinuxSendError("微信没有返回消息级图片下载完成回执")
        return True

    def close(self):
        with self._lock:
            if self._closed:
                self._cleanup_file_snapshots()
                return
            try:
                if self.expected_pid is not None and self.path.exists():
                    self._request(b"Q")
            except (OSError, EOFError, ValueError, LinuxSendError):
                pass
            finally:
                self._closed = True
                self._context_ready = False
            self._cleanup_file_snapshots()
            # 原生服务关闭自己的 socket；这里仅尝试移除已空的私有目录。
            try:
                self._directory.rmdir()
            except OSError:
                pass
