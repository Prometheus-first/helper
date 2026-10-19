"""纯 Python 的文本会话解析；不依赖 Windows 或原生 DLL。"""
from __future__ import annotations

import re

WXID = re.compile(r"^[A-Za-z0-9_.@-]{1,127}$")
SYSTEM_PEERS = frozenset({"weixin", "fmessage", "newsapp", "floatbottle"})
GROUP_CONTROL_COMMANDS = frozenset({"/help", "成语接龙", "合并接龙", "退出接龙"})
FOUR_CHINESE = re.compile(r"^[\u3400-\u4dbf\u4e00-\u9fff]{4}$")


def own_group_command(text):
    value = text.strip()
    return bool(value in GROUP_CONTROL_COMMANDS or FOUR_CHINESE.fullmatch(value) or
                value.split(maxsplit=1)[:1] in (["/绑定"], ["/总结"], ["/总结群聊"], ["/切换人格"], ["/清空"]))


def validate_target(target):
    if not isinstance(target, str) or not WXID.fullmatch(target):
        raise ValueError("目标应为微信 ID 或群 ID，长度最多 127 个 ASCII 字符")
    return target


def validate_mentions(room, members):
    validate_target(room)
    if not room.endswith("@chatroom"):
        raise ValueError("艾特消息的目标必须是群 ID")
    if not isinstance(members, (list, tuple)) or not 1 <= len(members) <= 8:
        raise ValueError("at_list 应包含 1–8 个成员微信 ID")
    from .account import validate_account
    result = tuple(validate_account(member) for member in members)
    if len(set(result)) != len(result):
        raise ValueError("at_list 不能重复")
    return result


class TextRouter:
    """先识别本账号，再区分会话与作者，避免回复自己同步过来的消息。"""

    def __init__(self, *, include_self_groups=False):
        self.include_self_groups = include_self_groups
        self.self_wxid = ""
        self.unidentified = 0
        self.unparsed_groups = 0
        self.ignored_self = 0

    def set_account(self, account):
        from .account import validate_account
        validate_account(account)
        if self.self_wxid and self.self_wxid != account:
            raise RuntimeError("当前登录账号发生变化，需要关闭旧连接后重新识别")
        self.self_wxid = account

    def convert(self, sender, receiver, text, *, sequence=0, thread_id=0, msg_type=1):
        validate_target(sender)
        validate_target(receiver)
        if type(msg_type) is not int or msg_type <= 0:
            raise ValueError("微信消息类型必须是正整数")
        filehelper = "filehelper" in (sender, receiver)
        incoming_group = sender.endswith("@chatroom")
        # 文件传输助手的另一端，以及收到群消息的接收方，都是本账号。
        identified = (receiver if sender == "filehelper" else sender) if filehelper else (
            receiver if incoming_group else "")
        if identified:
            validate_target(identified)
            if identified == "filehelper" or identified.endswith("@chatroom"):
                return None
            self.set_account(identified)
        if not self.self_wxid:
            self.unidentified += 1
            return None

        is_self = sender == self.self_wxid
        if filehelper:
            chat, author, room, body = "filehelper", sender, "", text
        elif incoming_group:
            if receiver != self.self_wxid:
                return None
            author, separator, body = text.partition(":\n")
            # Sync AddMsg 的群文本包含“成员 ID:\n正文”；不猜测缺失的作者。
            valid_author = bool(separator and WXID.fullmatch(author) and not author.endswith("@chatroom"))
            if msg_type != 1:
                valid_author = valid_author and author != "filehelper" and not author.startswith("gh_") and author not in SYSTEM_PEERS
            if not valid_author:
                if msg_type == 1:
                    self.unparsed_groups += 1
                    return None
                author, body = "", text
            chat = room = sender
            is_self = author == self.self_wxid
        elif receiver.endswith("@chatroom"):
            if not is_self:
                return None
            chat = room = receiver
            author, body = sender, text
        elif receiver == self.self_wxid:
            chat, author, room, body = sender, sender, "", text
        elif is_self:
            chat, author, room, body = receiver, sender, "", text
        else:
            return None
        own_group_input = bool(msg_type == 1 and room and own_group_command(body))
        if not filehelper and is_self and not own_group_input and not (room and self.include_self_groups):
            self.ignored_self += 1
            return None
        if chat.startswith("gh_") or chat in SYSTEM_PEERS:
            return None
        result = {"msg": body, "content": body, "from_wxid": author if room else chat,
                "room_wxid": room, "to_wxid": receiver, "wx_type": msg_type,
                "chat_id": chat, "sender_id": author, "is_self": is_self,
                "self_wxid": self.self_wxid, "native_sender": sender,
                "native_receiver": receiver, "sequence": sequence,
                "dispatch_to_plugins": not (room and is_self and not own_group_input),
                "native_thread_id": thread_id}
        if msg_type in (3, 49):
            result["raw_content"] = body
        return result
