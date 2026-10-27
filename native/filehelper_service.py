"""受支持微信构建的原生消息通道与桥接生命周期管理。"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import logging
import threading
from pathlib import Path

import pefile

from run_selftest import DLL, TEXT_DLL, K, check, find_wechat, modules, remote_call, run
from wxbot_framework.bridge_lifecycle import BridgeGuard

logger = logging.getLogger("NativeFileHelper")
K.GetExitCodeProcess.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]


def loaded_project_bridges(pid):
    """先核对所有桥接路径，不能卸载其他项目副本的模块。"""
    loaded = modules(pid)
    result = []
    for dll in (DLL, TEXT_DLL):
        entry = loaded.get(dll.name.lower())
        if entry:
            if Path(entry[1]).resolve() != dll.resolve():
                raise RuntimeError("微信中加载了其他项目的桥接，请先退出该项目；当前项目不会卸载它")
            result.append(dll)
    return result


def cleanup_loaded_bridges(pid):
    results = []
    for dll in loaded_project_bridges(pid):
        result = run("--remove", pid=pid, bridge_path=dll, preserve_report=False)
        if result["code"] or not result.get("dll_unloaded"):
            raise RuntimeError(f"桥接尚有原生任务未结束，暂不卸载：code={result['code']}")
        results.append(result)
    return results


class ReceivedMessage(c.Structure):
    _fields_ = [("size", c.c_uint32), ("present", c.c_uint32),
                ("type", c.c_uint32), ("thread_id", c.c_uint32),
                ("dropped", c.c_uint32), ("reserved", c.c_uint32),
                ("sequence", c.c_uint64), ("sender", c.c_char * 128),
                ("receiver", c.c_char * 128), ("text", c.c_char * 1024)]


assert c.sizeof(ReceivedMessage) == 1312


class ReceivedTextMessage(ReceivedMessage):
    _fields_ = [("mention_source", c.c_char * 4096)]


assert c.sizeof(ReceivedTextMessage) == 5408


class ReceivedMediaMessage(ReceivedTextMessage):
    # V3 保留字段名及 ABI；承载图片、好友申请和 appmsg XML。
    _fields_ = [("image_xml", c.c_char * 16384)]


assert c.sizeof(ReceivedMediaMessage) == 21792


class ReceivedMediaMessageV4(ReceivedTextMessage):
    _fields_ = [("image_xml", c.c_char * 262144)]


assert c.sizeof(ReceivedMediaMessageV4) == 267552


class ReceivedMediaMessageV5(ReceivedMediaMessageV4):
    _fields_ = [("native_message_id", c.c_uint64)]


assert c.sizeof(ReceivedMediaMessageV5) == 267560


class ReceivedMediaMessageV6(ReceivedMediaMessageV5):
    _fields_ = [("voice_offset", c.c_uint32), ("voice_size", c.c_uint32)]


assert c.sizeof(ReceivedMediaMessageV6) == 267568


class ReceiveChannel:
    def __init__(self, pid, *, dll=DLL, poll_export=b"WxPollFileHelperMessage", message_type=ReceivedMessage):
        self.message_type = message_type
        self.process = None
        self.buffer = None
        self.pending = False
        loaded_modules = modules(pid)
        loaded = loaded_modules.get(dll.name.lower())
        if not loaded or Path(loaded[1]).resolve() != dll.resolve():
            raise RuntimeError("原生文本桥接模块未加载")
        pe = pefile.PE(str(dll))
        export = next((item.address for item in pe.DIRECTORY_ENTRY_EXPORT.symbols if item.name == poll_export), None)
        if export is None:
            pe.close()
            raise RuntimeError("当前桥接缺少所需接收接口，请停止机器人并更新桥接 DLL")
        self.address = loaded[0] + export
        pe.close()
        self.core_base = loaded_modules["weixin.dll"][0]
        self.process = check(K.OpenProcess(0x43a, False, pid), "打开接收通道")
        try:
            self.buffer = check(K.VirtualAllocEx(self.process, None, c.sizeof(self.message_type),
                                                 0x3000, 0x04), "分配接收缓冲区")
        except Exception:
            self.close()
            raise

    def poll(self):
        if self.pending:
            raise RuntimeError("上次接收线程超时，不能继续复用缓冲区")
        try:
            code = remote_call(self.process, self.address, self.buffer)
        except TimeoutError:
            self.pending = True
            raise
        if code:
            raise RuntimeError(f"接收队列读取失败：{code}")
        message = self.message_type()
        read = c.c_size_t()
        check(K.ReadProcessMemory(self.process, self.buffer, c.byref(message), c.sizeof(message),
                                  c.byref(read)), "读取原生消息副本")
        if read.value != c.sizeof(message) or message.size != c.sizeof(message):
            raise RuntimeError("接收结构大小不匹配")
        if message.dropped:
            raise RuntimeError(f"接收队列已溢出，丢弃了 {message.dropped} 条消息")
        return message if message.present else None

    def read_memory(self, address, size):
        buffer = c.create_string_buffer(size)
        read = c.c_size_t()
        check(K.ReadProcessMemory(self.process, address, buffer, size, c.byref(read)), "读取当前登录账号")
        if read.value != size:
            raise ValueError("账号字段读取不完整")
        return buffer.raw

    def close(self):
        if self.process:
            # 超时线程可能仍持有参数，保留缓冲区直到客户端退出。
            if self.buffer and not self.pending:
                K.VirtualFreeEx(self.process, self.buffer, 0, 0x8000)
            K.CloseHandle(self.process)
        self.process = self.buffer = None

    def process_exited(self):
        if not self.process:
            return False
        code = w.DWORD()
        check(K.GetExitCodeProcess(self.process, c.byref(code)), "检查微信进程状态")
        return code.value != 259


class NativeFileHelperService:
    """提供原项目 register_msg_handler / send_text 的基础接口。"""

    _dll = DLL
    _poll_export = b"WxPollFileHelperMessage"
    _message_type = ReceivedMessage
    _run = staticmethod(run)

    def __init__(self, *, pid=None, preserve_reports=True):
        self.pid = pid
        self.preserve_reports = preserve_reports
        self.msg_handlers = []
        self.last_result = None
        self.last_error = None
        self._channel = None
        self._thread = None
        self._stop = threading.Event()
        self._calls = threading.RLock()
        self._owns_bridge = False
        self._guard = None
        self._cleanup_result = None

    def register_msg_handler(self, handler):
        self.msg_handlers.append(handler)

    def start(self):
        if self._owns_bridge or self._guard is not None:
            raise RuntimeError("服务已经启动")
        self._stop.clear()
        self.last_error = None
        self._cleanup_result = None
        self.pid = self.pid or find_wechat()
        self._guard = BridgeGuard(self.pid)
        try:
            stale = loaded_project_bridges(self.pid)
            self._owns_bridge = bool(stale)
            self._guard.start_watchdog(Path(__file__).with_name("bridge_watchdog.py"))
            if stale:
                logger.info("检测到上次运行遗留的桥接，正在安全清理，PID=%s", self.pid)
                cleanup_loaded_bridges(self.pid)
            # 在进入原生初始化前记录清理责任，初始化中断也会走 stop。
            self._owns_bridge = True
            armed = self._run("--listen", pid=self.pid)
            self.pid = armed["pid"]
            if armed["code"]:
                raise RuntimeError(f"接收 Hook 启动失败：{armed['code']}")
            self._channel = ReceiveChannel(self.pid, dll=self._dll, poll_export=self._poll_export,
                                           message_type=self._message_type)
            self._prepare_channel()
            self._thread = threading.Thread(target=self._receive_loop, name="WxFileHelperReceiver", daemon=True)
            self._thread.start()
        except BaseException:
            try:
                self.stop()
            except Exception:
                logger.exception("启动中断后的桥接清理未完成，保留占用锁等待重试")
            raise
        return True

    def _receive_loop(self):
        try:
            while not self._stop.is_set():
                with self._calls:
                    self._before_poll()
                    message = self._channel.poll()
                if message is None:
                    self._stop.wait(0.3)
                    continue
                data = self._map_message(message)
                if data is None:
                    continue
                for handler in list(self.msg_handlers):
                    try:
                        handler(self.pid, 11046, data, self)
                    except Exception:
                        logger.exception("Python 消息处理器异常")
        except Exception as error:
            self.last_error = error
            self._stop.set()
            logger.exception("原生接收服务停止")

    def _prepare_channel(self):
        """子类在开始消息线程前读取必要的当前连接信息。"""

    def _before_poll(self):
        """子类检查当前登录状态；旧文件传输助手后端保持原行为。"""

    def _map_message(self, message):
        sender = message.sender.decode("utf-8")
        receiver = message.receiver.decode("utf-8")
        if "filehelper" not in (sender, receiver) or message.type != 1:
            raise RuntimeError("收到范围外的消息副本")
        text = message.text.decode("utf-8")
        return {"msg": text, "content": text, "from_wxid": "filehelper", "room_wxid": "",
                "native_sender": sender, "native_receiver": receiver,
                "sequence": message.sequence, "native_thread_id": message.thread_id}

    def send_text(self, to_wxid, content):
        if to_wxid != "filehelper":
            raise NotImplementedError("当前验证版只开放文件传输助手")
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                raise RuntimeError("接收服务没有运行")
            self.last_result = self._run("--dispatch", pid=self.pid, text=content, keep_loaded=True,
                                   preserve_report=self.preserve_reports)
            return self.last_result["success"]

    def send_payload(self, payload):
        if payload.get("type") != 11237:
            raise NotImplementedError("当前验证版只支持文本发送 11237")
        data = payload["data"]
        return self.send_text(data["to_wxid"], data["content"])

    def stop(self):
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=35)
            if self._thread.is_alive():
                raise RuntimeError("接收线程尚未退出，保留 DLL，稍后再尝试停止")
        with self._calls:
            result = self._cleanup_result
            process_exited = bool((self._guard and self._guard.target_exited()) or
                                  (self._channel and self._channel.process_exited()))
            if self._channel:
                self._channel.close()
                self._channel = None
            if self._owns_bridge:
                if process_exited:
                    result = {"code": 0, "pid": self.pid, "process_exited": True, "dll_unloaded": True}
                else:
                    removed = cleanup_loaded_bridges(self.pid)
                    result = removed[-1] if removed else {"code": 0, "pid": self.pid,
                                                           "dll_unloaded": True, "already_unloaded": True}
                self._owns_bridge = False
                self._cleanup_result = result
            if self._guard is not None:
                self._guard.close()
                self._guard = None
            return result

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()
