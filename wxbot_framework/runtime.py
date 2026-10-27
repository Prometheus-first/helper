"""长期运行、连接监督与本地控制台命令。"""
from __future__ import annotations

import json
import logging
import queue
import shlex
import sys
import threading
import time
from concurrent.futures import Future
from dataclasses import asdict, replace
from types import MappingProxyType

from .backend import CAPABILITIES, TEXT_CAPABILITIES, NativeFileHelperBackend, NativeTextBackend
from .plugins import PluginManager
from .sdk import Message, ServiceFacade
from .members import MemberStore
from .personality import prepare_switch

logger = logging.getLogger("Bot")


class BotApplication:
    def __init__(self, root, store, *, backend_factory=None):
        self.root, self.store = root, store
        self.config = store.load()
        self.service = ServiceFacade(text_delay=self.config.get("text_send_delay"))
        self.message_observer = None
        self.service.message_observer = self._observe_message
        self.members = MemberStore.from_config(self.config, self.store.path.parent)
        generic = self.config.get("backend") == "native_text"
        capabilities = TEXT_CAPABILITIES if generic else CAPABILITIES
        self.plugins = PluginManager(root / "plugins", store, self.service, capabilities,
                                     self.config.get("plugin_queue_size", 128), member_store=self.members)
        backend_type = NativeTextBackend if generic else NativeFileHelperBackend
        self.backend_factory = backend_factory or (lambda: backend_type(root, self.config, data_root=self.store.path.parent))
        self.backend = None
        self.stop_event = threading.Event()
        self.stopped_event = threading.Event()
        self._shutdown_complete = False
        self.commands = queue.Queue()
        self.controls = queue.Queue(maxsize=64)
        self.last_cleanup = None
        self._connection_ready = False
        self._last_connection_log = 0
        self._last_connection_error = ""
        self._group_tracker = None
        self._next_group_poll = 0
        self._group_poll_failures = set()
        self._group_roles = {}

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
            if callable(getattr(candidate, "group_members", None)):
                from .group_events import GroupMembershipTracker
                account = self.config.get("wechat_account")
                if account:
                    self._group_tracker = GroupMembershipTracker(account, candidate.session_id)
            self._next_group_poll = 0
            logger.info("微信已连接，PID=%s；收发范围：%s", candidate.pid,
                        getattr(candidate, "scope", "文本"))
        except Exception as error:
            self._connection_ready = False
            self._log_connection_error(error)
            self._disconnect()

    def _observe_message(self, message):
        message = self._with_group_role(message)
        observer = getattr(self, 'message_observer', None)
        if observer is not None:
            try:
                observer(message)
            except Exception:
                logger.warning('消息监控暂不可用，消息继续处理')

    def _emit(self, message):
        message = self._with_group_role(message)
        self._observe_message(message)
        allowed = self.config.get('chat_allowlist', [])
        recall = message.type in (10000, 10002) and isinstance(message.data.get('recall'), dict)
        if recall or message.data.get('monitor_only'):
            # 防撤回插件需要原始媒体类型和撤回通知；不把监控事件交给普通玩法。
            if not allowed or message.chat_id in allowed:
                self._dispatch_recall_plugins(message)
            return
        if message.is_self and message.data.get('native_message_id') and not message.data.get('dispatch_to_plugins', True) and (
                not allowed or message.chat_id in allowed):
            self._dispatch_recall_plugins(message)
        if message.type in (10000, 10002):
            logger.info("群系统事件 | 群ID=%s", message.chat_id)
            allowed = self.config.get("chat_allowlist", [])
            if not message.is_group or (allowed and message.chat_id not in allowed):
                return
            event = message.data.get("group_event")
            if event:
                self.plugins.dispatch(message)
            elif (tracker := self._group_tracker) is not None:
                from .group_events import GroupEventError, parse_group_system_message
                try:
                    parsed = parse_group_system_message(message.data.get("system_content", ""),
                        msg_type=message.type, room_id=message.chat_id, self_id=tracker.account)
                    identified = [item for item in parsed if item.member_id]
                    if not parsed or not tracker.members(message.chat_id):
                        return
                    if not identified:
                        # A plain system notice may know the newcomer's name
                        # before the contact cache does. It cannot trigger a
                        # welcome; retain its name for a unique confirmed delta.
                        tracker.observe_system(message.chat_id,
                            message.data.get("system_content", ""), message.type,
                            sequence=message.sequence, session_id=message.session_id)
                        return
                    # 系统消息也可能是历史同步。只有完整实时名单支持同一变化时才提前通知。
                    rows = self.service.group_members(message.chat_id, expected_session=message.session_id)
                    current = {row["wxid"] for row in rows or ()}
                    if tracker.account not in current or any(
                            (item.member_id in current) != (item.kind == "join") for item in identified):
                        return
                    self._remember_group_members(message.chat_id, rows)
                    events = tracker.observe_system(message.chat_id,
                        message.data.get("system_content", ""), message.type,
                        sequence=message.sequence, session_id=message.session_id)
                except (GroupEventError, RuntimeError, OSError):
                    logger.debug("群系统事件未匹配或连接已结束：群ID=%s", message.chat_id)
                    return
                self._publish_group_events(events, message.session_id)
            return
        if message.type == 37:
            request = message.data.get("friend_request", {})
            logger.info("好友申请 | 申请ID=%s | 微信ID=%s | 昵称=%s", request.get("request_id", ""),
                        request.get("sender_id", ""), json.dumps(request.get("nickname", ""), ensure_ascii=False))
            # 好友申请是独立事件，不受聊天会话白名单控制、不写群历史。
            if message.data.get("dispatch_to_plugins", True):
                self.plugins.dispatch(message)
            return
        self._emit_chat(message)

    def _dispatch_recall_plugins(self, message):
        for name in ('anti_recall', 'anti_recall_image', 'anti_recall_file', 'anti_recall_voice'):
            self.plugins.dispatch(message, only=name)

    def _emit_chat(self, message):
        # 先显示会话 ID，未加入白名单的新群也能被发现；群正文不进入消息日志。
        if message.is_group or message.chat_id.endswith("@chatroom"):
            logger.info("群消息 | 群ID=%s", message.chat_id)
        elif message.type == 3:
            logger.info("图片消息 | 会话ID=%s | 状态=%s | 文件=%s", message.chat_id,
                        message.data.get("image_status", "pending"), message.image_path or "")
        elif message.type == 49:
            if message.data.get('app_type') == 19:
                logger.info('合并转发 | 发送者微信ID=%s | 记录数=%s | 状态=%s',
                            message.sender_id or message.chat_id,
                            len(message.data.get('forwarded_records', ())),
                            message.data.get('forwarded_status', 'pending'))
            else:
                logger.info("文件消息 | 发送者微信ID=%s | 状态=%s | 文件名=%s | 大小=%s | 路径=%s",
                        message.sender_id or message.chat_id, message.data.get("file_status", "pending"),
                        json.dumps(message.data.get("file_name", ""), ensure_ascii=False),
                        message.data.get("file_size", 0), json.dumps(message.file_path, ensure_ascii=False))
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

    def _publish_group_events(self, events, session_id):
        if not self._connection_ready or self.backend is None or self.backend.session_id != session_id:
            return
        for event in events:
            self._emit(Message(text="", chat_id=event.room_id, sender_id=event.member_id,
                type=10002, is_group=True, session_id=session_id,
                data={"group_event": asdict(event)}))

    def _remember_group_members(self, room, rows):
        if self.backend is not None:
            self._group_roles[room] = (self.backend.session_id, time.monotonic(),
                {row['wxid']: row.get('role', 'unknown') for row in rows})
        if self.members is None:
            return
        for row in rows:
            if row.get("wechat_name"):
                self.members.remember_wechat_name(row["wxid"], row["wechat_name"])
            nickname = row.get("group_nickname", row.get("nickname", ""))
            self.members.remember_group_nickname(row["wxid"], room, nickname[:64])

    def _with_group_role(self, message):
        if not message.is_group:
            return message
        cached = getattr(self, '_group_roles', {}).get(message.chat_id)
        sender = message.sender_id or (self.config.get('wechat_account') if message.is_self else '')
        role = 'unknown'
        if cached and cached[0] == message.session_id and time.monotonic() - cached[1] <= 30:
            role = cached[2].get(sender, 'unknown')
        return replace(message, data=MappingProxyType(dict(message.data, sender_role=role)))

    def _poll_group_members(self):
        if not self._connection_ready or self._group_tracker is None or self.backend is None:
            return
        tracker = self._group_tracker
        now = time.monotonic()
        if now < self._next_group_poll:
            return
        self._next_group_poll = now + 15
        session_id = self.backend.session_id
        allowed = self.config.get("chat_allowlist", [])
        rooms = {room for room in allowed if room.endswith("@chatroom")}
        if self.members is not None:
            rooms.update(self.members.groups)
        if callable(getattr(self.backend, "chats", None)):
            rooms.update(row["chat_id"] for row in self.backend.chats()
                         if row.get("is_group") and row["chat_id"].endswith("@chatroom"))
        if allowed:
            rooms.intersection_update(allowed)
        for room in sorted(rooms):
            if self.stop_event.is_set() or not self._connection_ready:
                break
            try:
                rows = self.service.group_members(room, expected_session=session_id)
                snapshot = None if rows is None else {
                    row["wxid"]: row.get("nickname") or row.get("wechat_name") or "" for row in rows}
                events = tracker.update(room, snapshot, session_id=session_id)
                if rows is not None:
                    self._remember_group_members(room, rows)
                    self._group_poll_failures.discard(room)
                elif room not in self._group_poll_failures:
                    logger.info("群成员缓存尚未就绪，等待完整名单：群ID=%s", room)
                    self._group_poll_failures.add(room)
                if rows is None:
                    self._group_roles.pop(room, None)
                self._publish_group_events(events, session_id)
            except Exception:
                self._group_roles.pop(room, None)
                # 缓存变化和查询失败不等同于退群，更不能影响文本收发。
                try:
                    tracker.update(room, None, session_id=session_id)
                except (ValueError, RuntimeError):
                    pass
                if room not in self._group_poll_failures:
                    logger.warning("群成员查询暂不可用，将稍后重试：群ID=%s", room)
                    self._group_poll_failures.add(room)

    def _disconnect(self):
        self._connection_ready = False
        self._group_roles.clear()
        if self._group_tracker is not None:
            self._group_tracker.close()
            self._group_tracker = None
        self._group_poll_failures.clear()
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
            logger.info("命令：status / chats / members [群ID] / history 称呼 [条数] / send-image 会话ID \"图片路径\" / send-file 会话ID \"文件路径\" / send-voice 会话ID \"语音路径\" / export-emoticons [raw] / send-emoticon 会话ID \"GIF路径\" / plugins / enable 名称 / disable 名称 / reload [名称] / quit")
            logger.info("好友与群管理：friend-requests / accept-friend 申请ID / invite-members 群ID 好友ID [好友ID...] / create-group 好友ID 好友ID [好友ID...]")
            logger.info("群成员：group-members 群ID / group-nickname 群ID 微信ID；欢迎提醒：enable group_notice / disable group_notice")
        elif action == "status" and len(words) == 1:
            logger.info("状态：%s", json.dumps({"connected": self._connection_ready,
                                               "pid": self.backend.pid if self.backend else None,
                                               "native": self.backend.diagnostics() if self.backend and hasattr(self.backend, "diagnostics") else {},
                                               "plugins": self.plugins.status()}, ensure_ascii=False))
        elif action == "chats" and len(words) == 1:
            rows = self.backend.chats() if self.backend and hasattr(self.backend, "chats") else []
            logger.info("本次连接已接收的会话：%s", json.dumps(rows, ensure_ascii=False))
        elif action == "friend-requests" and len(words) == 1:
            logger.info("本次连接的好友申请：%s", json.dumps(self.service.friend_requests(), ensure_ascii=False))
        elif action == "accept-friend" and len(words) == 2:
            if not self.service.accept_friend(words[1]):
                raise RuntimeError("微信未确认通过好友申请，请核对申请状态")
            logger.info("好友申请已通过：申请ID=%s", words[1])
        elif action == "invite-members" and len(words) >= 3:
            if not self.service.invite_group_members(words[1], words[2:]):
                raise RuntimeError("微信未确认群邀请操作，请核对权限及成员状态")
            logger.info("群邀请操作完成：群ID=%s，好友数=%d", words[1], len(words) - 2)
        elif action == "create-group" and len(words) >= 3:
            room = self.service.create_group(words[1:])
            logger.info("新群已创建：群ID=%s", room)
        elif action == "group-members" and len(words) == 2:
            rows = self.service.group_members(words[1])
            if rows is None:
                raise RuntimeError("微信尚未提供完整群成员缓存，请先在微信打开该群的成员列表后重试")
            self._remember_group_members(words[1], rows)
            logger.info("群成员：%s", json.dumps(rows, ensure_ascii=False))
        elif action == "group-nickname" and len(words) == 3:
            name = self.service.group_member_name(words[1], words[2])
            logger.info("群友昵称：群ID=%s 微信ID=%s 昵称=%s", words[1], words[2],
                        json.dumps(name, ensure_ascii=False))
        elif action == "send-image" and len(words) == 3:
            if not self.service.send_image(words[1], words[2]):
                raise RuntimeError("微信未接受图片发送请求，请查看原生结果")
            logger.info("图片发送请求已提交：会话ID=%s", words[1])
        elif action == "send-voice" and len(words) == 3:
            if not self.service.send_voice(words[1], words[2]):
                raise RuntimeError("微信未接受语音发送请求，请查看原生结果")
            logger.info("语音发送请求已提交：会话ID=%s", words[1])
        elif action == "send-file" and len(words) == 3:
            if not self.service.send_file(words[1], words[2]):
                raise RuntimeError("微信未接受文件发送请求，请查看原生结果")
            logger.info("文件发送请求已提交：会话ID=%s", words[1])
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

    def submit_control(self, callback):
        """网页控制任务交给运行线程；HTTP 线程不直接调用原生接口。"""
        future = Future()
        if self.stop_event.is_set():
            future.set_exception(RuntimeError('机器人正在停止'))
        else:
            try:
                self.controls.put_nowait((callback, future))
            except queue.Full:
                future.set_exception(RuntimeError('管理操作排队过多，请稍后再试'))
        return future

    def _drain_controls(self, *, stopping=False):
        for _ in range(64):
            try:
                callback, future = self.controls.get_nowait()
            except queue.Empty:
                return
            if not future.set_running_or_notify_cancel():
                continue
            try:
                if stopping or self.stop_event.is_set():
                    raise RuntimeError('机器人已停止，操作未执行')
                future.set_result(callback())
            except Exception as error:
                future.set_exception(error)

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
                self._drain_controls()
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
                self._poll_group_members()
                self.stop_event.wait(0.1)
        except KeyboardInterrupt:
            logger.info("收到退出指令")
        finally:
            self.stop()

    def stop(self):
        if self._shutdown_complete:
            return
        self.stop_event.set()
        self._drain_controls(stopping=True)
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
