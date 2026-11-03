"""手动启动 Linux 微信，验证文件传输助手原生发送；不自动发送。"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import subprocess

from wxbot_framework.linux_backend import LINUX_WECHAT_SHA256
from wxbot_framework.linux_sender import LinuxNativeSender, LinuxSendError, LinuxSendIndeterminate


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wechat", type=Path, default=Path("/opt/wechat/wechat"))
    parser.add_argument("--bridge", type=Path, default=root / "native/linux/build/send_bridge.so")
    args = parser.parse_args()
    wechat = args.wechat.resolve(strict=True)
    bridge = args.bridge.resolve(strict=True)
    with wechat.open("rb") as f:
        if hashlib.file_digest(f, "sha256").hexdigest() != LINUX_WECHAT_SHA256:
            raise SystemExit("Linux 微信版本指纹不匹配，未加载发送桥接。")
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            if (p / "exe").resolve() == wechat and b"--type" not in (p / "cmdline").read_bytes():
                raise SystemExit("此 Linux 微信已运行，请先正常退出；PID=" + p.name)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            pass
    sender = LinuxNativeSender()
    child = None
    try:
        env = os.environ.copy()
        env.update(sender.environment(), LD_PRELOAD=str(bridge))
        child = subprocess.Popen([str(wechat)], env=env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, start_new_session=True)
        sender.bind_pid(child.pid)
        sender.wait_ready(process=child)
        print("原生发送接口已就绪。登录微信后，输入 send 发送测试文字，输入 quit 停用接口。", flush=True)
        while child.poll() is None:
            try:
                command = input("> ").strip().lower()
            except EOFError:
                break
            if command == "quit":
                break
            if command != "send":
                print("可用命令：send、quit", flush=True)
                continue
            try:
                sender.send_text("filehelper", "WX_LINUX_NATIVE_SEND_01")
                print("微信返回发送成功，请在手机文件传输助手确认。", flush=True)
            except LinuxSendIndeterminate as error:
                print(str(error), flush=True)
            except LinuxSendError as error:
                print("未完成发送：" + str(error), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        sender.close()
        print("发送接口已停用，微信由你决定何时关闭。", flush=True)


if __name__ == "__main__":
    main()
