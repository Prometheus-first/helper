"""消息模型和插件上下文；插件不需要接触 Hook 或 DLL。"""
from __future__ import annotations

import copy
import logging
import threading
import time
from dataclasses import dataclass, field
from contextlib import nullcontext, contextmanager
from types import MappingProxyType
from typing import Mapping, Any

from .text import validate_target, validate_mentions
from .pacing import TextSendDelay
from .images import validate_image_path

logger = logging.getLogger("TextSend")


class CapabilityError(NotImplementedError):
    pass


@dataclass(frozen=True)
class Message:
    text: str
    chat_id: str
    sender_id: str = ""
    receiver_id: str = ""
    protocol: int = 11046
    type: int = 1
    is_group: bool = False
    is_self: bool = False
    sequence: int = 0
    session_id: str = ""
    received_at: float = field(default_factory=time.monotonic)
    data: Mapping[str, Any] = field(default_factory=dict, repr=False)

    @property
    def image_path(self):
        return self.data.get("image_path") or ""

    @property
    def file_path(self):
        return self.data.get("file_path") or ""

    @classmethod
    def from_native(cls, protocol, data, session_id):
        snapshot = copy.deepcopy(data)
        room = snapshot.get("room_wxid") or ""
        msg_type = snapshot.get("wx_type", snapshot.get("type", 1))
        if type(msg_type) is not int or msg_type <= 0:
            raise ValueError("微信消息类型必须是正整数")
        text = snapshot.get("msg") or snapshot.get("content") or ""
        if msg_type in (3, 34, 37, 43, 47, 49, 62, 10000, 10002):
            snapshot.setdefault("raw_content", text)
            text = ""
        # 群图片可能暂时没有作者；显式空作者不能回退成群 ID。
        sender = (snapshot.get("sender_id") or "") if "sender_id" in snapshot else (
            snapshot.get("native_sender") or snapshot.get("from_wxid") or "")
        return cls(text=text,
                   chat_id=snapshot.get("chat_id") or room or snapshot.get("from_wxid") or "",
                   sender_id=sender,
                   receiver_id=snapshot.get("native_receiver") or "", protocol=protocol, type=msg_type,
                   is_group=bool(room), is_self=bool(snapshot.get("is_self", False)), sequence=snapshot.get("sequence", 0),
                   session_id=session_id, data=MappingProxyType(snapshot))


class ServiceFacade:
    """序列化发送与连接切换，避免卸载正在使用的原生桥接。"""

    def __init__(self, *, text_delay=None):
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._backend = None
        self._available = False
        self._cancel = threading.Event()
        self.text_delay = TextSendDelay.from_config(text_delay)
        self.message_observer = None

    def _sent(self, result, backend, chat_id, msg_type, *, text='', path=''):
        observer = self.message_observer
        if observer is not None and result is not False:
            try:
                observer(Message(text, chat_id, type=msg_type, is_self=True,
                    is_group=chat_id.endswith('@chatroom'), session_id=backend.session_id,
                    data={'monitor_outgoing': True, 'bot_generated': True,
                          'file_name': str(path).replace('\\', '/').rsplit('/', 1)[-1] if path else ''}))
            except Exception:
                logger.warning('发送已完成，消息监控记录暂不可用')
        return result

    def bind(self, backend):
        with self._lock:
            if self._backend is not None:
                raise RuntimeError("已有连接，需先关闭")
            self._backend = backend
            self._available = True
            self._cancel = threading.Event()

    @property
    def connected(self):
        with self._lock:
            return self._backend is not None and self._available

    def session_is_current(self, session_id):
        with self._lock:
            return bool(self._backend is not None and self._available and
                        self._backend.session_id == session_id)

    def send_text(self, to_wxid, content, *, expected_session=None, _mentions=(), delivery_guard=None):
        validate_target(to_wxid)
        if not isinstance(content, str):
            raise ValueError("文本正文必须是字符串")
        encoded = content.encode("utf-8")
        if not encoded or len(encoded) > 1023 or b"\0" in encoded:
            raise ValueError("文本应为 1–1023 个 UTF-8 字节，且不能包含 NUL")
        with self._lock:
            if self._backend is None or not self._available:
                raise RuntimeError("微信尚未连接")
            if expected_session and expected_session != self._backend.session_id:
                raise RuntimeError("消息所属的微信连接已结束，跳过旧会话回复")
            if _mentions and not callable(getattr(self._backend, "send_at_text", None)):
                raise CapabilityError("当前后端尚未开放真实群艾特")
            backend, cancel = self._backend, self._cancel
        # 等待不占用连接锁，状态查询、断线清理和退出可以及时进行。
        # 发送锁覆盖等待及原生发送，多个插件不会同时等待后集中发送。
        with self._send_lock:
            with self._lock:
                self._check_pending(backend, cancel)
            seconds = self.text_delay.seconds_for(content)
            logger.debug("文本发送等待 %.3f 秒（%d 个字符）", seconds, len(content))
            if cancel.wait(seconds):
                raise RuntimeError("微信连接已结束，取消待发送文本")
            with self._lock:
                self._check_pending(backend, cancel)
                # 在文本节奏等待之后检查记录版本，清空后不发送旧总结。
                with delivery_guard() if delivery_guard is not None else nullcontext():
                    if _mentions:
                        result = backend.send_at_text(to_wxid, content, _mentions)
                    else:
                        result = backend.send_text(to_wxid, content)
                    return self._sent(result, backend, to_wxid, 1, text=content)

    def send_image(self, to_wxid, path, *, expected_session=None, delivery_guard=None):
        validate_target(to_wxid)
        image_path = validate_image_path(path)
        with self._lock:
            if self._backend is None or not self._available:
                raise RuntimeError("微信尚未连接")
            if expected_session and expected_session != self._backend.session_id:
                raise RuntimeError("消息所属的微信连接已结束，跳过旧会话回复")
            if not callable(getattr(self._backend, "send_image", None)):
                raise CapabilityError("当前后端尚未开放图片发送")
            backend, cancel = self._backend, self._cancel
        with self._send_lock:
            # 排队期间文件可能被清理或替换，发送前重新核对实际文件。
            image_path = validate_image_path(image_path)
            with self._lock:
                self._check_pending(backend, cancel)
                with delivery_guard() if delivery_guard is not None else nullcontext():
                    return self._sent(backend.send_image(to_wxid, str(image_path)), backend, to_wxid, 3)

    def send_emoticon(self, to_wxid, path, *, expected_session=None, delivery_guard=None):
        validate_target(to_wxid)
        with self._lock:
            if self._backend is None or not self._available:
                raise RuntimeError("微信尚未连接")
            if expected_session is not None and expected_session != self._backend.session_id:
                raise RuntimeError("消息所属的微信连接已结束，跳过旧会话回复")
            if not callable(getattr(self._backend, "send_emoticon", None)):
                raise CapabilityError("当前后端尚未开放表情发送")
            backend, cancel = self._backend, self._cancel
        # 完整 GIF 校验可能较慢；先固定连接，校验期间重连也不能串到新账号。
        from .emoticons import validate_emoticon_path
        emoticon_path = validate_emoticon_path(path)
        with self._send_lock:
            with self._lock:
                self._check_pending(backend, cancel)
            emoticon_path = validate_emoticon_path(emoticon_path)
            with self._lock:
                self._check_pending(backend, cancel)
                with delivery_guard() if delivery_guard is not None else nullcontext():
                    self._check_pending(backend, cancel)
                    return self._sent(backend.send_emoticon(to_wxid, str(emoticon_path)), backend, to_wxid, 47)

    def send_voice(self, to_wxid, path, *, expected_session=None, delivery_guard=None):
        validate_target(to_wxid)
        with self._lock:
            if self._backend is None or not self._available:
                raise RuntimeError("微信尚未连接")
            if expected_session is not None and expected_session != self._backend.session_id:
                raise RuntimeError("消息所属的微信连接已结束，跳过旧会话回复")
            if not callable(getattr(self._backend, "send_voice", None)):
                raise CapabilityError("当前后端尚未开放语音发送")
            backend, cancel = self._backend, self._cancel
        # 在文件校验之前固定账号连接，校验或排队期间重连也不能串到新账号。
        from .voices import inspect_voice_file
        voice = inspect_voice_file(path)
        with self._send_lock:
            with self._lock:
                self._check_pending(backend, cancel)
            voice = inspect_voice_file(voice.path)
            with self._lock:
                self._check_pending(backend, cancel)
                with delivery_guard() if delivery_guard is not None else nullcontext():
                    self._check_pending(backend, cancel)
                    return self._sent(backend.send_voice(to_wxid, str(voice.path)), backend, to_wxid, 34)

    def send_file(self, to_wxid, path, *, expected_session=None, delivery_guard=None):
        validate_target(to_wxid)
        with self._lock:
            if self._backend is None or not self._available:
                raise RuntimeError("微信尚未连接")
            if expected_session is not None and expected_session != self._backend.session_id:
                raise RuntimeError("消息所属的微信连接已结束，跳过旧会话回复")
            if not callable(getattr(self._backend, "send_file", None)):
                raise CapabilityError("当前后端尚未开放文件发送")
            backend, cancel = self._backend, self._cancel
        from .files import inspect_file
        file = inspect_file(path)
        with self._send_lock:
            with self._lock:
                self._check_pending(backend, cancel)
            file = inspect_file(file.path)
            with self._lock:
                self._check_pending(backend, cancel)
                with delivery_guard() if delivery_guard is not None else nullcontext():
                    self._check_pending(backend, cancel)
                    return self._sent(backend.send_file(to_wxid, str(file.path)), backend, to_wxid, 49, path=file.path)

    def export_emoticons(self, *, convert=True, expected_session=None):
        if type(convert) is not bool:
            raise ValueError("convert 必须是布尔值")
        with self._lock:
            if self._backend is None or not self._available:
                raise RuntimeError("微信尚未连接")
            if expected_session is not None and expected_session != self._backend.session_id:
                raise RuntimeError("消息所属的微信连接已结束，跳过旧会话导出")
            if not callable(getattr(self._backend, "export_emoticons", None)):
                raise CapabilityError("当前后端尚未开放表情导出")
            backend, cancel = self._backend, self._cancel
        with self._send_lock:
            with self._lock:
                self._check_pending(backend, cancel)
            # 导出/转换可能耗时较长，让断线清理能够及时取消当前账号任务。
            result = backend.export_emoticons(convert=convert)
            with self._lock:
                self._check_pending(backend, cancel)
                return result

    def _manage(self, operation, *arguments, expected_session=None):
        """将账号管理操作与发送、切号和卸载串行化，不使用文本等待。"""
        with self._lock:
            if self._backend is None or not self._available:
                raise RuntimeError("微信尚未连接")
            if expected_session is not None and expected_session != self._backend.session_id:
                raise RuntimeError("操作所属的微信连接已结束")
            method = getattr(self._backend, operation, None)
            if not callable(method):
                raise CapabilityError("当前后端尚未开放此账号管理操作")
            backend, cancel = self._backend, self._cancel
        with self._send_lock:
            with self._lock:
                self._check_pending(backend, cancel)
                return method(*arguments)

    def friend_requests(self, *, expected_session=None):
        return self._manage("friend_requests", expected_session=expected_session)

    def moments_capabilities(self, *, expected_session=None):
        return self._manage("moments_capabilities", expected_session=expected_session)

    def publish_moments(self, payload, *, expected_session):
        """长任务不持有连接锁；离线取消与原生清理可继续进行。"""
        with self._lock:
            backend, cancel = self._backend, self._cancel
            if backend is None or not self._available or backend.session_id != expected_session:
                return {'status': 'failed', 'phase': 'preflight'}
            method = getattr(backend, 'publish_moments', None)
            if not callable(method):
                return {'status': 'failed', 'phase': 'preflight'}
        with self._send_lock:
            with self._lock:
                if (self._backend is not backend or not self._available or cancel.is_set() or
                        backend.session_id != expected_session):
                    return {'status': 'failed', 'phase': 'preflight'}
            result = method(payload)
            with self._lock:
                if (self._backend is not backend or not self._available or cancel.is_set() or
                        backend.session_id != expected_session):
                    return {'status': 'unknown',
                            'unload_blocked': isinstance(result, dict) and result.get('unload_blocked') is True,
                            'retain_images': isinstance(result, dict) and result.get('retain_images') is True}
            return result

    def accept_friend(self, request_id, *, expected_session=None):
        if not isinstance(request_id, str) or not request_id or len(request_id) > 80:
            raise ValueError("请提供 friend-requests 中显示的申请 ID")
        return self._manage("accept_friend", request_id, expected_session=expected_session)

    def invite_group_members(self, room, members, *, expected_session=None):
        from .group_management import validate_group_id, validate_group_members
        room = validate_group_id(room)
        members = validate_group_members(members)
        return self._manage("invite_group_members", room, members, expected_session=expected_session)

    def create_group(self, members, *, expected_session=None):
        from .group_management import validate_group_members
        members = validate_group_members(members, creating=True)
        return self._manage("create_group", members, expected_session=expected_session)

    def group_members(self, room, *, expected_session=None):
        """读取当前账号的完整群成员快照；缓存未就绪时返回 None。"""
        from .group_management import validate_group_id
        return self._manage("group_members", validate_group_id(room), expected_session=expected_session)

    def contacts(self, *, expected_session=None):
        """读取当前连接的联系人快照；连接变化时不交付旧账号数据。"""
        with self._lock:
            session = self._backend.session_id if self._backend is not None else None
        result = self._manage("contacts", expected_session=expected_session if expected_session is not None else session)
        with self._lock:
            if (not self.session_is_current(session) or
                    result is not None and result.get("session_id") != session):
                raise RuntimeError("联系人所属的微信连接已结束")
            return result

    def group_member_name(self, room, wxid, *, expected_session=None):
        from .account import validate_account
        validate_account(wxid)
        rows = self.group_members(room, expected_session=expected_session)
        for row in rows or ():
            if row["wxid"] == wxid:
                return row.get("nickname") or row.get("wechat_name") or None
        return None

    def group_member_role(self, room, wxid, *, expected_session=None):
        """返回 owner/admin/member/unknown；未知身份不具有任何管理权限含义。"""
        from .account import validate_account
        validate_account(wxid)
        rows = self.group_members(room, expected_session=expected_session)
        for row in rows or ():
            if row['wxid'] == wxid:
                role = row.get('role', 'unknown')
                return role if role in ('owner', 'admin', 'member') else 'unknown'
        return 'unknown'

    def send_at_text(self, room_wxid, content, at_list, *, expected_session=None, display_names=None, delivery_guard=None):
        members = validate_mentions(room_wxid, at_list)
        if not isinstance(content, str) or not content:
            raise ValueError("文本正文必须是非空字符串")
        names = members if display_names is None else display_names
        if not isinstance(names, (list, tuple)) or len(names) != len(members) or any(
                not isinstance(name, str) or not name.strip() or len(name) > 64 or
                any(ch in name for ch in ("\n", "\r", "\0")) for name in names):
            raise ValueError("艾特显示昵称必须与成员列表一一对应，且每个昵称最多 64 字符")
        # 标签用于显示；真实通知始终使用 members 中的微信 ID。
        labels = " ".join("@" + name + "\u2005" for name in names)
        return self.send_text(room_wxid, labels + "\n" + content,
                              expected_session=expected_session, _mentions=members, delivery_guard=delivery_guard)

    def _check_pending(self, backend, cancel):
        if cancel.is_set() or not self._available or self._backend is not backend or self._cancel is not cancel:
            raise RuntimeError("微信连接已结束，取消待发送消息")

    def cancel_pending(self):
        with self._lock:
            self._available = False
            self._cancel.set()

    def send_payload(self, payload, *, expected_session=None):
        if payload.get("type") != 11237:
            raise CapabilityError("当前后端只支持文本发送 11237")
        data = payload["data"]
        return self.send_text(data["to_wxid"], data["content"], expected_session=expected_session)

    def close(self):
        with self._lock:
            self.cancel_pending()
            if self._backend is None:
                return None
            # 清理失败时仍持有此实例，下次可以继续清理，不能重新注入。
            result = self._backend.stop()
            self._backend = None
            return result


class LegacyService:
    def __init__(self, service, session_id):
        self._service, self._session_id = service, session_id

    def send_text(self, to_wxid, content):
        return self._service.send_text(to_wxid, content, expected_session=self._session_id)

    def send_image(self, to_wxid, path):
        return self._service.send_image(to_wxid, path, expected_session=self._session_id)

    def send_file(self, to_wxid, path):
        return self._service.send_file(to_wxid, path, expected_session=self._session_id)

    def send_voice(self, to_wxid, path, *, delivery_guard=None):
        return self._service.send_voice(to_wxid, path, expected_session=self._session_id,
                                        delivery_guard=delivery_guard)

    def send_emoticon(self, to_wxid, path):
        return self._service.send_emoticon(to_wxid, path, expected_session=self._session_id)

    def export_emoticons(self, *, convert=True):
        return self._service.export_emoticons(convert=convert, expected_session=self._session_id)

    def send_payload(self, payload):
        return self._service.send_payload(payload, expected_session=self._session_id)


class PluginContext:
    def __init__(self, name, options, service, capabilities, *, llm_config_path=None, member_store=None, speech=None,
                 plugin_active=None):
        self.name = name
        self.config = MappingProxyType(copy.deepcopy(options))
        self.logger = logging.getLogger(f"Plugin.{name}")
        self.capabilities = frozenset(capabilities)
        self._service = service
        self._llm_config_path = llm_config_path
        self._llm = None
        self._members = member_store
        self._speech = speech
        self._plugin_active = plugin_active

    def plugin_is_active(self, name):
        return bool(self._plugin_active and self._plugin_active(name))

    @property
    def speech(self):
        if self._speech is None:
            raise CapabilityError("尚未配置语音回复服务")
        return self._speech

    @property
    def members(self):
        if self._members is None:
            raise CapabilityError("尚未配置框架成员目录")
        return self._members

    @property
    def llm(self):
        if self._llm is None:
            if self._llm_config_path is None:
                raise ValueError("尚未配置大模型文件")
            from .llm import LLMClient
            self._llm = LLMClient.from_file(self._llm_config_path)
        return self._llm

    def send_text(self, chat_id, text):
        return self._service.send_text(chat_id, text)

    def send_image(self, chat_id, path):
        return self._service.send_image(chat_id, path)

    def send_file(self, chat_id, path, *, expected_session=None, delivery_guard=None):
        return self._service.send_file(chat_id, path, expected_session=expected_session,
                                       delivery_guard=delivery_guard)

    def accept_friend(self, request_id, *, expected_session=None):
        return self._service.accept_friend(request_id, expected_session=expected_session)

    def group_members(self, room, *, expected_session=None):
        return self._service.group_members(room, expected_session=expected_session)

    def contacts(self, *, expected_session=None):
        if "contact.read" not in self.capabilities:
            raise CapabilityError("当前插件缺少联系人读取能力")
        return self._service.contacts(expected_session=expected_session)

    def group_member_name(self, room, wxid, *, expected_session=None):
        return self._service.group_member_name(room, wxid, expected_session=expected_session)

    def group_member_role(self, room, wxid, *, expected_session=None):
        return self._service.group_member_role(room, wxid, expected_session=expected_session)

    def send_at_text(self, room, text, members, *, expected_session=None, display_names=None):
        return self._service.send_at_text(room, text, members, expected_session=expected_session,
                                         display_names=display_names)

    def send_voice(self, chat_id, path, *, expected_session=None, delivery_guard=None):
        return self._service.send_voice(chat_id, path, expected_session=expected_session,
                                        delivery_guard=delivery_guard)

    def send_emoticon(self, chat_id, path):
        return self._service.send_emoticon(chat_id, path)

    def export_emoticons(self, *, convert=True):
        return self._service.export_emoticons(convert=convert)

    def personality(self, message):
        from .personality import PersonalityState
        return self._members.personality(message.chat_id) if self._members is not None else PersonalityState(message.chat_id)

    def llm_for(self, message, *, personality=None):
        from .personality import PersonalityLLM
        state = personality if personality is not None else self.personality(message)
        return PersonalityLLM(self.llm, state, current=lambda: self._members is None or
                              self._members.personality_is_current(state))

    def personality_guard(self, state):
        return self._members.personality_delivery_guard(state) if self._members is not None else nullcontext()

    @contextmanager
    def response_guard(self, snapshot, personality):
        with self.members.delivery_guard(snapshot), self.personality_guard(personality):
            yield

    def reply(self, message, text, *, delivery_guard=None):
        return self._service.send_text(message.chat_id, text, expected_session=message.session_id,
                                       delivery_guard=delivery_guard)

    def reply_image(self, message, path, *, delivery_guard=None):
        return self._service.send_image(message.chat_id, path, expected_session=message.session_id,
                                        delivery_guard=delivery_guard)

    def reply_file(self, message, path, *, delivery_guard=None):
        return self._service.send_file(message.chat_id, path, expected_session=message.session_id,
                                       delivery_guard=delivery_guard)

    def reply_voice(self, message, path, *, delivery_guard=None):
        return self._service.send_voice(message.chat_id, path, expected_session=message.session_id,
                                        delivery_guard=delivery_guard)

    def reply_emoticon(self, message, path, *, delivery_guard=None):
        return self._service.send_emoticon(message.chat_id, path, expected_session=message.session_id,
                                           delivery_guard=delivery_guard)

    def reply_at(self, message, text, *, display_name=None, delivery_guard=None):
        if not message.is_group:
            raise ValueError("reply_at 仅用于群消息")
        if self._members is not None:
            member = self._members.find_by_wxid(message.sender_id, message.chat_id)
            if member is not None and (display_name is None or member.self_named):
                display_name = member.display_name
            elif display_name is None:
                display_name = self._members.display_name(message.sender_id, message.chat_id)
        return self._service.send_at_text(message.chat_id, text, [message.sender_id],
                                         expected_session=message.session_id,
                                         display_names=[display_name] if display_name is not None else None,
                                         delivery_guard=delivery_guard)
