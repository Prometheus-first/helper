"""Linux 原生文本事件的本地传输协议。"""
from __future__ import annotations

import os
from pathlib import Path
import socket
import struct
import tempfile

_HEADER = struct.Struct("!4sQIII")
_MAGIC = b"HLT1"
_MEDIA_MAGIC = b"HLM1"
_MAX_FIELD = (1 << 32) - 1


class LinuxWireError(ValueError):
    pass


def encode_text_event(sender: str, receiver: str, text: str, *, sequence: int = 1) -> bytes:
    if type(sequence) is not int or not 0 < sequence < (1 << 64):
        raise LinuxWireError("消息序号超出协议范围")
    if not all(isinstance(x, str) for x in (sender, receiver, text)):
        raise LinuxWireError("消息字段必须是字符串")
    fields = [x.encode("utf-8") for x in (sender, receiver, text)]
    if any(not field or len(field) > _MAX_FIELD for field in fields):
        raise LinuxWireError("消息字段为空或超出协议范围")
    return _HEADER.pack(_MAGIC, sequence, *(len(field) for field in fields)) + b"".join(fields)


def encode_media_event(sender: str, receiver: str, xml: str, *, msg_type: int, sequence: int = 1) -> bytes:
    """媒体只传递类型与 XML 元数据，不在本地通道传输图片、文件原始字节。"""
    if type(msg_type) is not int or msg_type not in (3, 49):
        raise LinuxWireError("尚未适配此媒体消息类型")
    frame = encode_text_event(sender, receiver, xml, sequence=sequence)
    return _MEDIA_MAGIC + frame[4:_HEADER.size] + struct.pack("!I", msg_type) + frame[_HEADER.size:]


def _read_exact(conn: socket.socket, length: int) -> bytes:
    parts = []
    remaining = length
    while remaining:
        part = conn.recv(min(remaining, 65536))
        if not part:
            raise EOFError("本地消息连接中断")
        parts.append(part)
        remaining -= len(part)
    return b"".join(parts)


def read_bridge_ready(conn: socket.socket):
    """确认原生桥接已经完成版本校验与 Hook 安装。"""
    if _read_exact(conn, 4) != b"HLR1":
        raise LinuxWireError("原生桥接握手不匹配")

def read_text_event(conn: socket.socket) -> dict:
    header = _read_exact(conn, _HEADER.size)
    magic, sequence, sender_len, receiver_len, text_len = _HEADER.unpack(header)
    if magic not in (_MAGIC, _MEDIA_MAGIC) or not sequence or not sender_len or not receiver_len or not text_len:
        raise LinuxWireError("无效的 Linux 文本事件帧")
    msg_type = struct.unpack("!I", _read_exact(conn, 4))[0] if magic == _MEDIA_MAGIC else 1
    if magic == _MEDIA_MAGIC and msg_type not in (3, 49):
        raise LinuxWireError("媒体事件类型无效")
    # 字段长度使用协议内的无符号 32 位长度；不截断正文。
    try:
        sender = _read_exact(conn, sender_len).decode("utf-8", errors="strict")
        receiver = _read_exact(conn, receiver_len).decode("utf-8", errors="strict")
        text = _read_exact(conn, text_len).decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise LinuxWireError("文本事件不是有效的 UTF-8") from error
    return {"sender": sender, "receiver": receiver, "text": text, "sequence": sequence, "type": msg_type}


def acknowledge_text_event(conn: socket.socket, sequence: int, *, stop: bool = False):
    """仅在事件已进入本地处理队列后确认，断线重试可用序号去重。"""
    if type(sequence) is not int or not 0 < sequence < (1 << 64):
        raise LinuxWireError("确认序号无效")
    if type(stop) is not bool:
        raise LinuxWireError("停止标记必须是布尔值")
    conn.sendall(struct.pack("!Q", (~sequence & ((1 << 64) - 1)) if stop else sequence))

class LocalEventListener:
    """仅接受指定本机 PID 和 UID 的 Unix socket，目录只对当前用户开放。"""

    def __init__(self, expected_pid: int | None = None, *, directory=None):
        if not hasattr(socket, "SO_PEERCRED"):
            raise RuntimeError("Linux 本地连接需要 SO_PEERCRED")
        if expected_pid is not None and (type(expected_pid) is not int or expected_pid <= 0):
            raise ValueError("目标 PID 无效")
        self.expected_pid = expected_pid
        self.expected_uid = os.geteuid()
        self._dir = Path(tempfile.mkdtemp(prefix="helper-linux-", dir=directory))
        self.path = self._dir / "events.sock"
        self._socket = None
        try:
            if len(os.fsencode(self.path)) >= 108:
                raise ValueError("本地 socket 路径过长")
            self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self._socket.bind(os.fspath(self.path))
            os.chmod(self.path, 0o600)
            self._inode = self.path.lstat().st_ino
            self._socket.listen(1)
        except Exception:
            if self._socket is not None:
                self._socket.close()
            if self.path.exists():
                self.path.unlink()
            self._dir.rmdir()
            raise

    def bind_pid(self, pid: int):
        if type(pid) is not int or pid <= 0:
            raise ValueError("目标 PID 无效")
        if self.expected_pid is not None and self.expected_pid != pid:
            raise RuntimeError("已绑定目标进程，不能切换")
        self.expected_pid = pid

    def accept(self, *, timeout: float | None = None) -> socket.socket:
        if self.expected_pid is None:
            raise RuntimeError("尚未绑定目标微信进程")
        self._socket.settimeout(timeout)
        conn, _ = self._socket.accept()
        try:
            pid, uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if pid != self.expected_pid or uid != self.expected_uid:
                raise PermissionError("拒绝非目标微信进程的本地连接")
            return conn
        except BaseException:
            conn.close()
            raise

    def close(self):
        if self._socket is not None:
            self._socket.close()
            self._socket = None
        try:
            if self.path.lstat().st_ino == self._inode:
                self.path.unlink()
        except FileNotFoundError:
            pass
        try:
            self._dir.rmdir()
        except FileNotFoundError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()






# 新调用方可使用较准确的名称；旧文本调用入口保持兼容。
read_message_event = read_text_event
