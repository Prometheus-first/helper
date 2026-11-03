"""Linux 私聊/群聊文本通道实机测试，只接收固定测试标记。"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import time

from wxbot_framework.linux_messages import LinuxTextAdapter
from wxbot_framework.linux_wire import LocalEventListener, LinuxWireError, acknowledge_text_event, read_bridge_ready, read_text_event

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = "2749535f299ba67feb8ef80cab898bb0531fa8f376b16f8a72db706811c84132"
TARGETS = {"WX_LINUX_BRIDGE_07": False, "WX_LINUX_BRIDGE_08": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wechat", default="/opt/wechat/wechat")
    parser.add_argument("--bridge", default=str(ROOT / "native/linux/build/text_bridge.so"))
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    wechat, bridge = Path(args.wechat).resolve(strict=True), Path(args.bridge).resolve(strict=True)
    if args.timeout < 1:
        raise SystemExit("测试超时必须为正数")
    with wechat.open("rb") as f:
        if hashlib.file_digest(f, "sha256").hexdigest() != EXPECTED:
            raise SystemExit("Linux 微信版本指纹不匹配，拒绝加载测试桥接")
    # 只观察受控测试，不让原有 LD_PRELOAD 影响本次独立验证。
    with LocalEventListener() as listener:
        env = os.environ.copy()
        env.update(LD_PRELOAD=os.fspath(bridge), HELPER_LINUX_EVENT_SOCKET=os.fspath(listener.path),
                   HELPER_LINUX_CONTROLLER_PID=str(os.getpid()), HELPER_LINUX_TEST_MARKERS="1")
        child = subprocess.Popen([os.fspath(wechat)], env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        listener.bind_pid(child.pid)
        print("Linux 微信已启动，请完成登录，正在等待测试桥接就绪。", flush=True)
        adapter = None
        received = set()
        handled = set()
        bridge_ready = False
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise SystemExit("Linux 微信已退出，本次测试结束")
            try:
                conn = listener.accept(timeout=1)
            except (socket.timeout, PermissionError):
                continue
            with conn:
                conn.settimeout(3)
                try:
                    read_bridge_ready(conn)
                except (socket.timeout, EOFError, ConnectionError, LinuxWireError):
                    continue
                if not bridge_ready:
                    print("测试桥接已就绪，等待私聊和群聊测试消息。", flush=True)
                    bridge_ready = True
                while time.monotonic() < deadline:
                    try:
                        event = read_text_event(conn)
                    except (socket.timeout, EOFError, ConnectionError):
                        break
                    seq = event["sequence"]
                    if seq not in handled:
                        if adapter is None:
                            adapter = LinuxTextAdapter(event["receiver"], f"test-{child.pid}")
                        message = adapter.convert(event)
                        if message is not None and TARGETS.get(message.text) is message.is_group:
                            received.add(message.text)
                            kind = "群聊" if message.is_group else "私聊"
                            print(f"{kind}测试消息已进入框架，正文与发送者字段通过校验。", flush=True)
                        handled.add(seq)
                    finished = len(received) == len(TARGETS)
                    acknowledge_text_event(conn, seq, stop=finished)
                    if finished:
                        print("两条测试均通过，测试接收已停用；微信保持打开。", flush=True)
                        return
        if not bridge_ready:
            raise SystemExit("测试桥接未就绪；微信保持打开。")
        raise SystemExit("等待测试消息超时，测试进程结束；微信保持打开。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("测试已停止；微信保持打开。", flush=True)
