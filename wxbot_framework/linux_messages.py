"""将已验证的 Linux 原生文本事件转换为框架 Message，共用账号隔离和群文本解析。"""
from __future__ import annotations

from collections.abc import Mapping

from .account import validate_account
from .sdk import Message
from .mentions import parse_at_members
from .recall import message_id
from .text import TextRouter


class LinuxTextAdapter:
    """将已核实的 Linux 字段转换为现有插件使用的 Message。"""

    def __init__(self, account: str, session_id: str, *, include_self_groups=False):
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("必须提供当前机器人连接的 session_id")
        self.account = validate_account(account)
        self.session_id = session_id
        self._router = TextRouter(include_self_groups=include_self_groups)
        self._router.set_account(self.account)

    def convert(self, payload: Mapping) -> Message | None:
        if not isinstance(payload, Mapping):
            raise TypeError("消息事件必须是映射")
        sender = payload.get("sender")
        receiver = payload.get("receiver")
        text = payload.get("text")
        if not isinstance(text, str):
            raise ValueError("消息正文必须是字符串")
        sequence = payload.get("sequence", 0)
        if type(sequence) is not int or sequence < 0:
            raise ValueError("消息序号必须是非负整数")
        # 仅开放已验证的文本；其他消息类型不能冒充文本进入插件。
        msg_type = payload.get("type", 1)
        if type(msg_type) is not int or msg_type != 1:
            raise ValueError("Linux 目前只处理已验证的文本消息")
        data = self._router.convert(sender, receiver, text, sequence=sequence, msg_type=msg_type)
        if data is None:
            return None
        native_id = message_id(payload.get("native_message_id"))
        if native_id:
            data["native_message_id"] = native_id
        if data.get("room_wxid"):
            data["at_members"] = parse_at_members(payload.get("msgsource"))
            data["is_at_self"] = self.account in data["at_members"]
        return Message.from_native(11046, data, self.session_id)
