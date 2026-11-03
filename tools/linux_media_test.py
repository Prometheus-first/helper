"""Linux 受控媒体测试：文件传输助手发送，以及指定测试账号的媒体元数据接收。"""
from __future__ import annotations

import argparse
import hashlib
import json
import queue
import threading
import os
from pathlib import Path
import socket
import struct
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
import zlib

from wxbot_framework.linux_backend import LINUX_WECHAT_SHA256
from wxbot_framework.linux_sender import LinuxNativeSender
from wxbot_framework.linux_image_downloader import LinuxImageDownloader
from wxbot_framework.image_metadata import parse_image_metadata
from wxbot_framework.linux_media import parse_test_media, find_cached_file, read_cached_image
from wxbot_framework.linux_wire import LocalEventListener, acknowledge_text_event, read_bridge_ready, read_message_event, LinuxWireError

ARM = "WX_LINUX_MEDIA_ARM_11"
FILE_NAME = "WX_LINUX_FILE_RECV_01.txt"
FILE_TEXT = "WX_LINUX_FILE_RECV_01\n这是 Linux 文件接收测试。\n"


def parse_media(event):
    return parse_test_media(event, expected_filename=FILE_NAME)


def image_forward_xml(capture):
    root = ET.fromstring(capture["xml"])
    image = root.find("img")
    if image is None:
        raise ValueError("缺少图片元数据")
    if not image.get("filekey"):
        key = image.get("cdnmidimgurl") or image.get("cdnbigimgurl")
        if key:
            image.set("filekey", key)
    if not image.get("filekey") or not image.get("aeskey"):
        raise ValueError("图片未包含可下载的 CDN 元数据")
    return ET.tostring(root, encoding="unicode")


def make_image(path):
    width, height = 240, 160
    rows = []
    for y in range(height):
        rgb = (45, 110, 210) if y < height // 2 else (245, 165, 55)
        rows.append(bytes([0]) + bytes(rgb) * width)
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data) & 0xffffffff)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0)) +
                     chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


class MediaTest:
    def __init__(self, sender, directory, *, auto_download=False):
        self.sender = sender
        self.directory = Path(directory)
        self.image_path = self.directory / "WX_LINUX_IMAGE_SEND_01.png"
        self.file_path = self.directory / "WX_LINUX_FILE_SEND_01.txt"
        make_image(self.image_path)
        self.file_path.write_text("WX_LINUX_FILE_SEND_01\n这是 Linux 文件发送测试。\n", encoding="utf8")
        self.peer = None
        self.receiver = None
        self.captures = {}
        self.results = {}
        self.errors = {}
        self.attempted = set()
        self.armed = False
        self.auto_download = auto_download
        self._download_updates = queue.Queue()
        self._download_started = False
        self._download_generation = 0
        self._downloader = LinuxImageDownloader(sender)
        self._download_thread = None

    def on_event(self, event):
        if event.get("type") == 1:
            if event.get("text") != ARM:
                return
            if self.peer is not None and event["sender"] != self.peer:
                return
            self.peer, self.receiver = event["sender"], event["receiver"]
            if self.auto_download:
                self.sender.bind_image_scope(self.peer, self.receiver)
                self.results["image_context_bound"] = True
            self.armed = True
            print("媒体测试已绑定当前测试账号，等待一张图片和一个测试文件。", flush=True)
            return
        if not self.armed or event.get("sender") != self.peer or event.get("receiver") != self.receiver:
            return
        capture = parse_media(event)
        if capture is not None:
            kind = capture["type"]
            self.captures[kind] = capture
            print("测试图片元数据已收到。" if kind == 3 else "测试文件元数据已收到。", flush=True)
            if kind == 3 and self.auto_download and not self._download_started:
                self._download_started = True
                self._download_thread = threading.Thread(target=self._download_image, args=(dict(capture),), daemon=True)
                self._download_thread.start()

    def _download_image(self, capture):
        try:
            metadata = parse_image_metadata(capture["xml"])
            destination = self.directory / f"WX_LINUX_AUTO_DOWNLOADED_IMAGE_{self._download_generation}.bin"
            self._download_updates.put(("download_started", True))
            if self._downloader(metadata, destination) is not True:
                raise RuntimeError("图片下载已取消")
            self._download_updates.put(("auto_download", True))
            print("测试图片已自动下载，完整 MD5 校验通过；正在发送到文件传输助手。", flush=True)
            self.sender.send_image("filehelper", destination)
            self._download_updates.put(("auto_download_send", True))
        except Exception as error:
            self._download_updates.put(("auto_download_error", type(error).__name__))
            print("测试图片自动下载未完成：" + type(error).__name__, flush=True)

    def close(self):
        self._downloader.close()
        if self._download_thread is not None:
            self._download_thread.join(timeout=3)

    def execute(self, operation, *, repeat=False):
        if operation == "status":
            return self.status()
        if operation in self.attempted and not repeat:
            raise RuntimeError("本项已经尝试，不自动重复发送")
        self.attempted.add(operation)
        if operation == "validate":
            self.sender.ensure_context()
            payload = {"chat_id": "filehelper", "message_type": 3, "content_xml": ""}
            raw = self.sender._request(b"G", json.dumps(payload).encode())
            result = json.loads(raw.get("result", "{}"))
            if raw.get("callback") is not True or result.get("success") is not False or "content_xml" not in result.get("error_message", ""):
                raise RuntimeError("通用接口没有返回预期的参数校验结果")
            self.results[operation] = True
        elif operation == "download_image":
            if self._download_thread is not None and self._download_thread.is_alive():
                raise RuntimeError("上一项图片下载尚未结束")
            capture = self.captures[3]
            self._download_generation += 1
            self.errors.pop("auto_download_error", None)
            for name in ("download_started", "auto_download", "auto_download_send"):
                self.results.pop(name, None)
            self._download_started = True
            self._download_thread = threading.Thread(target=self._download_image, args=(dict(capture),), daemon=True)
            self._download_thread.start()
        elif operation == "send_image":
            self.sender.send_image("filehelper", self.image_path)
            self.results[operation] = True
        elif operation == "send_file":
            self.sender.send_file("filehelper", self.file_path)
            self.results[operation] = True
        elif operation == "forward_image":
            capture = self.captures[3]
            pid = getattr(self.sender, "expected_pid", None)
            body = read_cached_image(capture, self.receiver, pid) if pid and self.receiver else None
            if body is not None:
                extension = ".png" if body.startswith(b"\x89PNG") else ".jpg" if body.startswith(b"\xff\xd8\xff") else ".bin"
                destination = self.directory / ("WX_LINUX_IMAGE_RECV_01" + extension)
                destination.write_bytes(body); destination.chmod(0o600)
                self.sender.send_image("filehelper", destination)
            else:
                self.sender.send_generic({"chat_id": "filehelper", "message_type": 3, "content_xml": image_forward_xml(capture)})
            self.results[operation] = True
        elif operation == "forward_file":
            capture = self.captures[49]
            cached = find_cached_file(capture, self.receiver) if self.receiver and capture.get("md5") and capture.get("attributes", {}).get("totallen") else None
            if cached is not None:
                self.sender.send_file("filehelper", cached)
            elif capture.get("app_type") == 74:
                raise RuntimeError("新版附件尚未在当前账号缓存中找到；主动下载接口仍待适配")
            else:
                self.sender.send_generic({"chat_id": "filehelper", "message_type": 49, "message_subtype": 6, "content_xml": capture["xml"]})
            self.results[operation] = True
        else:
            raise ValueError("未知测试操作")
        return {"success": True, "operation": operation}

    def status(self):
        while True:
            try: name, value = self._download_updates.get_nowait()
            except queue.Empty: break
            if name == "auto_download_error": self.errors[name] = RuntimeError(value)
            else: self.results[name] = value
        captures = {}
        for key, value in self.captures.items():
            attrs = value.get("attributes", {})
            captures["image" if key == 3 else "file"] = {"received": True, "field_names": sorted(attrs),
                "has_aeskey": bool(attrs.get("aeskey")), "has_cdn_token": bool(attrs.get("filekey") or attrs.get("cdnmidimgurl") or attrs.get("cdnattachurl")),
                "app_type": value.get("app_type"), "has_upload_token": bool(attrs.get("fileuploadtoken"))}
        return {"armed": self.armed, "captures": captures, "callback_results": dict(self.results),
                "error_types": {key: type(value).__name__ for key, value in self.errors.items()},
                "native_image_context": dict(getattr(self.sender, "image_context_status", {}))}


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wechat", type=Path, default=Path("/opt/wechat/wechat"))
    parser.add_argument("--receive-bridge", type=Path, default=root / "native/linux/build/text_bridge.so")
    parser.add_argument("--send-bridge", type=Path)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--auto-download-images", action="store_true", help="收到一张受控测试图片后自动下载并发往文件传输助手")
    parser.add_argument("--state-file", type=Path, default=root / ".runtime/linux_media_test.json")
    args = parser.parse_args()
    if args.send_bridge is None:
        args.send_bridge = root / "native/linux/build/send_bridge.so"
    wechat, receive, send = [p.resolve(strict=True) for p in (args.wechat, args.receive_bridge, args.send_bridge)]
    if args.timeout < 1:
        parser.error("超时必须为正数")
    with wechat.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != LINUX_WECHAT_SHA256:
            raise SystemExit("Linux 微信版本指纹不匹配")
    for p in Path("/proc").iterdir():
        if not p.name.isdigit():
            continue
        try:
            if (p / "exe").resolve() == wechat and b"--type" not in (p / "cmdline").read_bytes():
                raise SystemExit("Linux 微信仍在运行，请先退出；PID=" + p.name)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass
    work = Path(tempfile.mkdtemp(prefix="helper-media-test-"))
    control_path = work / "control.sock"
    control = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    control.bind(str(control_path));control_path.chmod(0o600);control.listen(4);control.setblocking(False)
    sender = LinuxNativeSender()
    test = MediaTest(sender, work, auto_download=args.auto_download_images)
    state_file = args.state_file.resolve();state_file.parent.mkdir(parents=True, exist_ok=True)
    stop_read, stop_write = os.pipe()
    child = None;conn = None;stopped = False;handled = set()
    def publish():
        value = {"controller_pid": os.getpid(), "wechat_pid": child.pid if child else None,
                 "control": str(control_path), "stopped": stopped, **test.status()}
        fd = os.open(state_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf8") as output:
            json.dump(value, output)
    try:
        with LocalEventListener() as listener:
            env = os.environ.copy();env.update(sender.environment(), LD_PRELOAD=os.pathsep.join((str(receive), str(send))),
                HELPER_LINUX_EVENT_SOCKET=str(listener.path), HELPER_LINUX_TEST_MARKERS="1", HELPER_LINUX_TEST_MEDIA="1",
                HELPER_LINUX_MEDIA_ARM_TEXT=ARM, HELPER_LINUX_STOP_FD=str(stop_read))
            child = subprocess.Popen([str(wechat)], env=env, pass_fds=(stop_read,), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            os.close(stop_read);stop_read = -1;listener.bind_pid(child.pid);sender.bind_pid(child.pid)
            sender.wait_ready(process=child)
            print("媒体测试接口已就绪，请完成 Linux 微信登录并保持运行。", flush=True)
            publish();deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline and child.poll() is None and not stopped:
                if not test._download_updates.empty(): publish()
                try:client, _ = control.accept()
                except BlockingIOError:client = None
                if client is not None:
                    with client:
                        client.settimeout(2)
                        peer_uid = struct.unpack("3i", client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
                        if peer_uid == os.geteuid():
                            request = b""
                            while b"\n" not in request and len(request) < 4096:
                                part = client.recv(4096)
                                if not part:break
                                request += part
                            action = json.loads(request)
                            op = action.get("operation")
                            try:
                                if op == "stop":stopped = True;result = {"stopped": True}
                                else:result = test.execute(op, repeat=action.get("repeat") is True)
                            except Exception as error:
                                test.errors[op] = error
                                result = {"success": False, "error_type": type(error).__name__, "error": str(error)}
                            client.sendall(json.dumps(result, ensure_ascii=False).encode() + b"\n")
                            publish()
                if stopped:break
                if conn is None:
                    try:
                        conn = listener.accept(timeout=.2);conn.settimeout(.3);read_bridge_ready(conn)
                    except (socket.timeout, PermissionError, OSError, EOFError, LinuxWireError):
                        if conn is not None:conn.close();conn = None
                        continue
                try:event = read_message_event(conn)
                except socket.timeout:continue
                except (OSError, EOFError, LinuxWireError):conn.close();conn = None;continue
                acknowledge_text_event(conn, event["sequence"])
                if event["sequence"] in handled:continue
                handled.add(event["sequence"])
                try:test.on_event(event)
                except (ValueError, ET.ParseError) as error:test.errors["receive"] = error
                publish()
    except KeyboardInterrupt:
        pass
    finally:
        stopped = True
        if stop_read >= 0:os.close(stop_read)
        os.close(stop_write)
        if conn is not None:conn.close()
        test.close();sender.close();control.close();control_path.unlink(missing_ok=True);publish()
        print("媒体测试已停用，微信保持打开。", flush=True)


if __name__ == "__main__":main()
