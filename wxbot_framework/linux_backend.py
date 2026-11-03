"""实验性 Linux 文本后端；尚未接入正式配置或网页管理台。"""
from __future__ import annotations

from collections import deque
import hashlib
import logging
import os
from pathlib import Path
import socket
import subprocess
import threading
import time
import uuid

from .linux_messages import LinuxTextAdapter
from .linux_wire import LocalEventListener, LinuxWireError, acknowledge_text_event, read_bridge_ready, read_text_event

logger = logging.getLogger("LinuxBackend")
LINUX_WECHAT_SHA256 = "2749535f299ba67feb8ef80cab898bb0531fa8f376b16f8a72db706811c84132"


class LinuxNativeTextBackend:
    """启动 Linux 微信收发文本，发送范围由显式会话白名单控制。"""

    capabilities = frozenset({"text.receive", "friend.text.receive", "group.text.receive"})
    scope = "实验性文本接收"

    def __init__(self, wechat_path, bridge_path, account, *, test_markers_only=True, send_bridge_path=None, send_targets=("filehelper",),
                 _expected_executable_sha256=LINUX_WECHAT_SHA256, _ready_timeout=5.0):
        self.wechat_path = Path(wechat_path).resolve(strict=True)
        self.bridge_path = Path(bridge_path).resolve(strict=True)
        self.send_bridge_path = Path(send_bridge_path).resolve(strict=True) if send_bridge_path else None
        from .text import validate_target
        if isinstance(send_targets, (str, bytes)):
            raise ValueError("发送会话白名单必须是会话列表")
        self.send_targets = frozenset(validate_target(target) for target in send_targets)
        if not self.send_targets:
            raise ValueError("发送会话白名单不能为空")
        self._sender = None
        if self.send_bridge_path is not None:
            sending = {"text.send"}
            if any(target.endswith("@chatroom") for target in self.send_targets):
                sending.add("group.text.send")
            if any(target != "filehelper" and not target.endswith("@chatroom") for target in self.send_targets):
                sending.add("friend.text.send")
            self.capabilities = type(self).capabilities | frozenset(sending)
            self.scope = "实验性文本收发（按会话白名单）"
        self.session_id = uuid.uuid4().hex
        self.adapter = LinuxTextAdapter(account, self.session_id)
        self.test_markers_only = bool(test_markers_only)
        self.expected_hash = _expected_executable_sha256
        self._ready_timeout = max(0.1, float(_ready_timeout))
        self.last_error = None
        self.process = None
        self._listener = None
        self._conn = None
        self._stop_fd = None
        self._thread = None
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._seen = set()
        self._seen_order = deque()

    @property
    def pid(self):
        return self.process.pid if self.process else None

    def start(self, emit):
        if self._stopping.is_set() or self.process is not None or self._thread is not None:
            raise RuntimeError("Linux 后端已启动")
        if not callable(emit):
            raise TypeError("消息回调必须可调用")
        with self.wechat_path.open("rb") as f:
            if hashlib.file_digest(f, "sha256").hexdigest() != self.expected_hash:
                raise RuntimeError("Linux 微信版本指纹不匹配，拒绝加载桥接")
        listener = LocalEventListener()
        read_fd, write_fd = os.pipe()
        try:
            env = os.environ.copy()
            env.update(LD_PRELOAD=os.fspath(self.bridge_path),
                       HELPER_LINUX_EVENT_SOCKET=os.fspath(listener.path),
                       HELPER_LINUX_CONTROLLER_PID=str(os.getpid()),
                       HELPER_LINUX_STOP_FD=str(read_fd))
            if self.send_bridge_path is not None:
                from .linux_sender import LinuxNativeSender
                self._sender = LinuxNativeSender(allowed_targets=self.send_targets)
                env.update(self._sender.environment())
                env["LD_PRELOAD"] = os.pathsep.join((os.fspath(self.bridge_path), os.fspath(self.send_bridge_path)))
            if self.test_markers_only:
                env["HELPER_LINUX_TEST_MARKERS"] = "1"
            else:
                env.pop("HELPER_LINUX_TEST_MARKERS", None)
            child = subprocess.Popen([os.fspath(self.wechat_path)], env=env, pass_fds=(read_fd,),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=None, start_new_session=True)
            listener.bind_pid(child.pid)
            if self._sender is not None:
                self._sender.bind_pid(child.pid)
        except BaseException:
            if self._sender is not None:
                self._sender.close()
                self._sender = None
            os.close(write_fd)
            listener.close()
            raise
        finally:
            os.close(read_fd)
        self.process = child
        self._listener = listener
        self._stop_fd = write_fd
        # 只有来自目标进程的桥接完成握手，才允许上层报告连接成功。
        ready_conn = None
        deadline = time.monotonic() + self._ready_timeout
        try:
            while ready_conn is None and time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError("Linux 微信进程在桥接就绪前退出")
                try:
                    candidate = listener.accept(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
                except (socket.timeout, PermissionError):
                    continue
                try:
                    candidate.settimeout(min(0.5, max(0.01, deadline - time.monotonic())))
                    read_bridge_ready(candidate)
                    ready_conn = candidate
                except (socket.timeout, EOFError, OSError, LinuxWireError):
                    candidate.close()
            if ready_conn is None:
                raise RuntimeError("Linux 微信桥接未就绪，无法接收消息")
            if self._sender is not None:
                self._sender.wait_ready(timeout=self._ready_timeout, process=child)
            with self._lock:
                self._conn = ready_conn
            self._thread = threading.Thread(target=self._receive, args=(emit, ready_conn),
                                            name="LinuxTextReceiver", daemon=True)
            self._thread.start()
        except BaseException as error:
            self.last_error = error
            if self._thread is not None and self._thread.ident is None:
                self._thread = None
            self.stop()
            if ready_conn is not None:
                ready_conn.close()
            raise
        return True

    def _remember(self, sequence):
        self._seen.add(sequence)
        self._seen_order.append(sequence)
        if len(self._seen_order) > 4096:
            self._seen.discard(self._seen_order.popleft())

    def _receive(self, emit, initial_conn):
        ready_conn = initial_conn
        while not self._stopping.is_set():
            if self.process.poll() is not None:
                self.last_error = RuntimeError("Linux 微信进程已退出")
                return
            greeted = ready_conn is not None
            if greeted:
                conn, ready_conn = ready_conn, None
            else:
                try:
                    conn = self._listener.accept(timeout=0.2)
                except socket.timeout:
                    continue
                except PermissionError:
                    logger.warning("拒绝了非目标微信进程的本地连接")
                    continue
                except OSError as error:
                    if not self._stopping.is_set():
                        self.last_error = error
                    return
            with self._lock:
                self._conn = conn
            try:
                conn.settimeout(0.5)
                if not greeted:
                    try:
                        read_bridge_ready(conn)
                    except (socket.timeout, EOFError, ConnectionError):
                        continue
                    except LinuxWireError as error:
                        self.last_error = error
                        return
                while not self._stopping.is_set():
                    try:
                        event = read_text_event(conn)
                    except (socket.timeout, EOFError, ConnectionError):
                        break
                    except (OSError, LinuxWireError) as error:
                        if not self._stopping.is_set():
                            self.last_error = error
                        return
                    try:
                        seq = event["sequence"]
                        if seq not in self._seen:
                            message = self.adapter.convert(event)
                            if message is not None:
                                emit(message)
                            # ACK 发送失败后会重试同一帧；在 ACK 之前记住已派发的序号。
                            self._remember(seq)
                    except Exception as error:
                        if not self._stopping.is_set():
                            self.last_error = error
                        return
                    try:
                        acknowledge_text_event(conn, seq)
                    except OSError:
                        # 微信会重新连接并重传；保留去重状态。
                        break
            finally:
                with self._lock:
                    self._conn = None
                conn.close()
    def send_text(self, chat_id, text):
        if self._sender is None or self._stopping.is_set() or self.process is None or self.process.poll() is not None:
            raise RuntimeError("Linux 原生发送桥接没有运行")
        return self._sender.send_text(chat_id, text)

    def stop(self):
        self._stopping.set()
        if self._sender is not None:
            self._sender.close()
            self._sender = None
        if self._stop_fd is not None:
            os.close(self._stop_fd)
            self._stop_fd = None
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=3)
            if self._thread.is_alive():
                raise RuntimeError("Linux 接收线程未能正常停止")
            self._thread = None
        if self._listener is not None:
            self._listener.close()
            self._listener = None
        # 微信由用户决定何时关闭；关闭控制管道会禁用后续采集。
        return {"capture_stop_requested": True, "wechat_left_running": bool(self.process and self.process.poll() is None)}
