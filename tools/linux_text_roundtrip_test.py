"""Linux 私聊与群聊的受控原生收发测试，不加载日常插件。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import time
import uuid

from wxbot_framework.linux_backend import LINUX_WECHAT_SHA256
from wxbot_framework.linux_messages import LinuxTextAdapter
from wxbot_framework.linux_sender import LinuxNativeSender, LinuxSendError
from wxbot_framework.linux_wire import LocalEventListener, acknowledge_text_event, read_bridge_ready, read_text_event, LinuxWireError
from wxbot_framework.sdk import PluginContext, ServiceFacade

TARGETS = {False: ("WX_LINUX_BRIDGE_07_SEND", "WX_LINUX_FRIEND_REPLY_01"),
           True: ("WX_LINUX_BRIDGE_08_SEND", "WX_LINUX_GROUP_REPLY_01")}


class RoundtripPlugin:
    """只回复每类首次收到的固定标记，未知结果不自动重试。"""
    def __init__(self, sender, *, report=print):
        self.sender = sender
        self.session_id = uuid.uuid4().hex
        self.adapter = None
        self.attempted = set()
        self.succeeded = set()
        self.errors = {}
        self._report = report
        self.service = ServiceFacade(text_delay={"enabled": False})
        self.service.bind(self)
        self.context = PluginContext("linux_roundtrip_test", {}, self.service,
                                     {"text.receive", "text.send"})

    def send_text(self, chat_id, text):
        return self.sender.send_text(chat_id, text)

    def stop(self):
        self.sender.close()

    def on_event(self, event):
        body = event.get("text", "")
        if not isinstance(body, str) or not any(marker in body for marker, _ in TARGETS.values()):
            return False
        if self.adapter is None:
            # 仅用当前测试消息中已验证的接收者字段建立本轮会话，不读取目录推测登录账号。
            candidate = LinuxTextAdapter(event["receiver"], self.session_id)
            message = candidate.convert(event)
        else:
            message = self.adapter.convert(event)
        if message is None or message.is_self:
            return False
        marker, reply = TARGETS[message.is_group]
        if message.text != marker or message.chat_id == "filehelper" or message.is_group in self.attempted:
            return False
        if self.adapter is None:
            self.adapter = candidate
        kind = message.is_group
        self.attempted.add(kind)
        # 只授权这条固定测试消息所属的会话；其他会话不会自动获得发送权限。
        self.sender.allowed_targets = self.sender.allowed_targets | {message.chat_id}
        label = "群聊" if kind else "私聊"
        self._report(f"{label}标记已进入框架，正在通过插件 reply() 回复。")
        try:
            if self.on_message(message, self.context) is not True:
                raise LinuxSendError("原生发送未返回成功")
        except Exception as error:
            self.errors[kind] = error
            self._report(f"{label}回复未确认成功，不会自动重试。")
        else:
            self.succeeded.add(kind)
            self._report(f"{label}原生回复成功：{reply}")
        return True

    def on_message(self, message, ctx):
        return ctx.reply(message, TARGETS[message.is_group][1])

    def status(self):
        return {"account_observed": self.adapter is not None,
                "friend_attempted": False in self.attempted, "group_attempted": True in self.attempted,
                "friend_callback_success": False in self.succeeded, "group_callback_success": True in self.succeeded,
                "error_types": {"group" if k else "friend": type(v).__name__ for k, v in self.errors.items()}}


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wechat", type=Path, default=Path("/opt/wechat/wechat"))
    parser.add_argument("--receive-bridge", type=Path, default=root / "native/linux/build/text_bridge.so")
    parser.add_argument("--send-bridge", type=Path, default=root / "native/linux/build/send_bridge.so")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--state-file", type=Path, default=root / ".runtime/linux_roundtrip_test.json")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("超时必须为正数")
    wechat = args.wechat.resolve(strict=True)
    receive, send = args.receive_bridge.resolve(strict=True), args.send_bridge.resolve(strict=True)
    with wechat.open("rb") as f:
        if hashlib.file_digest(f, "sha256").hexdigest() != LINUX_WECHAT_SHA256:
            raise SystemExit("Linux 微信版本指纹不匹配，未加载桥接。")
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            if (p / "exe").resolve() == wechat and b"--type" not in (p / "cmdline").read_bytes():
                raise SystemExit("此 Linux 微信仍在运行，请先正常退出；PID=" + p.name)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    sender = LinuxNativeSender()
    plugin = RoundtripPlugin(sender, report=lambda value: print(value, flush=True))
    state_file = args.state_file.resolve()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    stop_read, stop_write = os.pipe()
    control_dir = Path(tempfile.mkdtemp(prefix="helper-roundtrip-"))
    control_path = control_dir / "control.sock"
    control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    control.bind(str(control_path));control_path.chmod(0o600);control.listen(4);control.setblocking(False)
    child = None
    conn = None
    stopped = False
    handled = set()
    def publish():
        # 只保留测试结果与控制路径，不持久化账号、群 ID、正文。
        state = {"controller_pid": os.getpid(), "wechat_pid": child.pid if child else None,
                 "control": str(control_path), "stopped": stopped, **plugin.status()}
        fd = os.open(state_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf8") as output:
            json.dump(state, output)
    try:
        with LocalEventListener() as listener:
            env = os.environ.copy()
            env.update(sender.environment(), LD_PRELOAD=os.pathsep.join((str(receive), str(send))),
                       HELPER_LINUX_EVENT_SOCKET=str(listener.path), HELPER_LINUX_TEST_MARKERS="1",
                       HELPER_LINUX_STOP_FD=str(stop_read))
            child = subprocess.Popen([str(wechat)], env=env, pass_fds=(stop_read,),
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                     start_new_session=True)
            os.close(stop_read);stop_read = -1
            listener.bind_pid(child.pid);sender.bind_pid(child.pid)
            sender.wait_ready(process=child)
            print("Linux 微信已启动，请完成登录；收发桥接已加载。", flush=True)
            publish()
            deadline = time.monotonic() + args.timeout
            greeted = False
            while time.monotonic() < deadline and child.poll() is None and not stopped:
                try:
                    client, _ = control.accept()
                except BlockingIOError:
                    client = None
                if client is not None:
                    with client:
                        client.settimeout(1)
                        peer_uid = int.from_bytes(client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)[4:8], "little")
                        if peer_uid == os.geteuid():
                            op = client.recv(1)
                            if op == b"Q":
                                stopped = True
                            client.sendall(json.dumps(plugin.status()).encode() + b"\n")
                    if stopped:
                        break
                if conn is None:
                    try:
                        conn = listener.accept(timeout=0.2)
                        conn.settimeout(0.5)
                        read_bridge_ready(conn)
                    except (socket.timeout, PermissionError, OSError, EOFError, LinuxWireError):
                        if conn is not None:
                            conn.close();conn = None
                        continue
                    if not greeted:
                        print("测试桥接已就绪，等待私聊和群聊标记。", flush=True)
                        greeted = True
                try:
                    event = read_text_event(conn)
                except socket.timeout:
                    continue
                except (OSError, EOFError, LinuxWireError):
                    conn.close();conn = None
                    continue
                seq = event["sequence"]
                acknowledge_text_event(conn, seq)
                if seq in handled:
                    continue
                handled.add(seq)
                plugin.on_event(event)
                publish()
    except KeyboardInterrupt:
        pass
    finally:
        stopped = True
        if stop_read >= 0:
            os.close(stop_read)
        os.close(stop_write)
        if conn is not None:
            conn.close()
        plugin.service.close()
        control.close();control_path.unlink(missing_ok=True);control_dir.rmdir()
        publish()
        print("收发测试已停用，微信保持打开。", flush=True)


if __name__ == "__main__":
    main()
