"""长期运行、连接监督与本地控制台命令。"""
from __future__ import annotations

import json
import logging
import queue
import shlex
import sys
import threading
import time
from dataclasses import asdict

from .backend import CAPABILITIES, TEXT_CAPABILITIES, NativeFileHelperBackend, NativeTextBackend
from .plugins import PluginManager
from .sdk import ServiceFacade
from .members import MemberStore
from .personality import prepare_switch

logger = logging.getLogger("Bot")


class BotApplication:
    def __init__(self, root, store, *, backend_factory=None):
        self.root, self.store = root, store
        self.config = store.load()
        self.service = ServiceFacade(text_delay=self.config.get("text_send_delay"))
        self.members = MemberStore.from_config(self.config, self.store.path.parent)
        generic = self.config.get("backend") == "native_text"
        capabilities = TEXT_CAPABILITIES if generic else CAPABILITIES
        self.plugins = PluginManager(root / "plugins", store, self.service, capabilities,
                                     self.config.get("plugin_queue_size", 128), member_store=self.members)
        backend_type = NativeTextBackend if generic else NativeFileHelperBackend
        self.backend_factory = backend_factory or (lambda: backend_type(root, self.config))
        self.backend = None
        self.stop_event = threading.Event()
        self.stopped_event = threading.Event()
        self._shutdown_complete = False
        self.commands = queue.Queue()
        self.last_cleanup = None
        self._connection_ready = False
        self._last_connection_log = 0
        self._last_connection_error = ""

    def _log_connection_error(self, error):
        text = f"{type(error).__name__}: {error}"
        now = time.monotonic()
        if text != self._last_connection_error or now - self._last_connection_log >= 30:
            logger.warning("等待微信连接：%s", text)
            self._last_connection_error, self._last_connection_log = text, now

    def _connect(self):
        try:
            candidate = self.backend_factory()
            self.backend = candidate
            self.service.bind(candidate)
            candidate.start(self._emit)
            self._connection_ready = True
            self._last_connection_error = ""
            logger.info("微信已连接，PID=%s；收发范围：%s", candidate.pid,
                        getattr(candidate, "scope", "文本"))
        except Exception as error:
            self._connection_ready = False
            self._log_connection_error(error)
            self._disconnect()

    def _emit(self, message):
        # 先显示会话 ID，未加入白名单的新群也能被发现；群正文不进入消息日志。
        if message.is_group or message.chat_id.endswith("@chatroom"):
            logger.info("群消息 | 群ID=%s", message.chat_id)
        elif message.type == 3:
            logger.info("图片消息 | 会话ID=%s | 状态=%s | 文件=%s", message.chat_id,
                        message.data.get("image_status", "pending"), message.image_path or "")
        else:
            logger.info("私聊消息 | 发送者微信ID=%s | 内容=%s",
                        message.sender_id or message.chat_id,
                        json.dumps(message.text, ensure_ascii=False))
        allowed = self.config.get("chat_allowlist", [])
        if not allowed or message.chat_id in allowed:
            if self.members is not None:
                try:
                    self.members.record(message)
                except Exception:
                    logger.exception("群文本保存失败，消息继续分发到插件")
            if message.data.get("dispatch_to_plugins", True):
                if self.plugins.is_active("personality"):
                    message = prepare_switch(message, self.members)
                self.plugins.dispatch(message)

    def _disconnect(self):
        self._connection_ready = False
        try:
            result = self.service.close()
            if result is not None:
                self.last_cleanup = result
            self.backend = None
            return True
        except Exception as error:
            # 清理忙时保留实例，只重试清理，不覆盖 DLL、不再次注入。
            self._log_connection_error(error)
            return False

    def execute(self, command):
        words = shlex.split(command)
        if not words:
            return
        action = words[0].lower()
        if action in ("quit", "exit", "stop") and len(words) == 1:
            self.stop_event.set()
        elif action == "help" and len(words) == 1:
            logger.info("命令：status / chats / members [群ID] / history 称呼 [条数] / send-image 会话ID \"图片路径\" / send-voice 会话ID \"语音路径\" / export-emoticons [raw] / send-emoticon 会话ID \"GIF路径\" / plugins / enable 名称 / disable 名称 / reload [名称] / quit")
        elif action == "status" and len(words) == 1:
            logger.info("状态：%s", json.dumps({"connected": self._connection_ready,
                                               "pid": self.backend.pid if self.backend else None,
                                               "native": self.backend.diagnostics() if self.backend and hasattr(self.backend, "diagnostics") else {},
                                               "plugins": self.plugins.status()}, ensure_ascii=False))
        elif action == "chats" and len(words) == 1:
            rows = self.backend.chats() if self.backend and hasattr(self.backend, "chats") else []
            logger.info("本次连接已接收的会话：%s", json.dumps(rows, ensure_ascii=False))
        elif action == "send-image" and len(words) == 3:
            if not self.service.send_image(words[1], words[2]):
                raise RuntimeError("微信未接受图片发送请求，请查看原生结果")
            logger.info("图片发送请求已提交：会话ID=%s", words[1])
        elif action == "send-voice" and len(words) == 3:
            if not self.service.send_voice(words[1], words[2]):
                raise RuntimeError("微信未接受语音发送请求，请查看原生结果")
            logger.info("语音发送请求已提交：会话ID=%s", words[1])
        elif action == "send-emoticon" and len(words) == 3:
            if not self.service.send_emoticon(words[1], words[2]):
                raise RuntimeError("微信未接受表情发送请求，请查看原生结果")
            logger.info("表情发送请求已提交：会话ID=%s", words[1])
        elif action == "export-emoticons" and (len(words) == 1 or words[1:] == ["raw"]):
            rows = self.service.export_emoticons(convert=len(words) == 1)
            logger.info("本地缓存表情导出：%s", json.dumps([asdict(row) for row in rows],
                                                      ensure_ascii=False, default=str))
        elif action == "members" and len(words) in (1, 2):
            if self.members is None:
                raise ValueError("尚未配置成员目录")
            rows = [{"name": m.name, "display_name": m.display_name, "wxid": m.wxid,
                     "room": m.room, "bound": bool(m.wxid)} for m in self.members.list_members(words[1] if len(words) == 2 else None)]
            logger.info("成员目录：%s", json.dumps(rows, ensure_ascii=False))
        elif action == "history" and len(words) in (2, 3):
            if self.members is None:
                raise ValueError("尚未配置成员目录")
            rows = self.members.history(words[1], limit=int(words[2]) if len(words) == 3 else 10)
            logger.info("成员聊天记录：%s", json.dumps(rows, ensure_ascii=False))
        elif action == "plugins" and len(words) == 1:
            for row in self.plugins.status():
                state = "运行" if row["active"] else "停用"
                logger.info("%s [%s] 已处理=%s 异常=%s 丢弃=%s %s", row["name"], state,
                            row["handled"], row["errors"], row["dropped"], row["error"])
        elif action in ("enable", "disable") and len(words) == 2:
            getattr(self.plugins, action)(words[1])
        elif action == "reload" and len(words) in (1, 2):
            self.plugins.reload(words[1] if len(words) == 2 else None)
        else:
            raise ValueError("未知命令；输入 help 查看用法")

    def _read_console(self):
        # stdin 关闭时仍继续运行，适合后台启动。
        try:
            for line in sys.stdin:
                if self.stop_event.is_set():
                    break
                self.commands.put(line.strip())
        except (OSError, ValueError):
            pass

    def run(self, *, seconds=0):
        started = time.monotonic()
        next_connect = 0
        next_rollover = 0
        try:
            if self.members is not None:
                self.members.initialize()
            self.plugins.reload()
            if self.config.get("console", True):
                threading.Thread(target=self._read_console, name="BotConsole", daemon=True).start()
            logger.info("框架已启动；输入 help 查看插件管理命令，Ctrl+C 退出")
            while not self.stop_event.is_set():
                now = time.monotonic()
                if self.members is not None and now >= next_rollover:
                    self.members.rollover()
                    next_rollover = now + 0.5
                if seconds and now - started >= seconds:
                    break
                while True:
                    try:
                        command = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self.execute(command)
                    except Exception:
                        logger.exception("控制台命令执行失败")
                if self.stop_event.is_set():
                    break
                if self._connection_ready and self.backend.last_error:
                    self._log_connection_error(self.backend.last_error)
                    self._disconnect()
                    next_connect = now + self.config.get("reconnect_seconds", 5)
                elif not self._connection_ready and now >= next_connect:
                    if self.backend is None or self._disconnect():
                        self._connect()
                    next_connect = time.monotonic() + self.config.get("reconnect_seconds", 5)
                self.stop_event.wait(0.1)
        except KeyboardInterrupt:
            logger.info("收到退出指令")
        finally:
            self.stop()

    def stop(self):
        if self._shutdown_complete:
            return
        self.stop_event.set()
        self.service.cancel_pending()
        cleaned = False
        try:
            # 先卸载原生连接，再等待模型等插件回调，关闭窗口时也能及时清理 DLL。
            for attempt in range(3):
                cleaned = self._disconnect()
                if cleaned:
                    break
                if attempt < 2:
                    time.sleep(0.2)
        finally:
            try:
                self.plugins.shutdown()
            finally:
                try:
                    if self.members is not None:
                        self.members.close()
                finally:
                    self.stopped_event.set()
        if not cleaned:
            raise RuntimeError("原生连接未能清理；保留 DLL，请查看日志后再启动")
        self._shutdown_complete = True
        logger.info("框架已停止；连接清理：%s", self.last_cleanup)
