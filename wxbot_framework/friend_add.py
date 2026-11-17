"""Validate a single group-sourced outgoing friend request."""
from __future__ import annotations
import re
from .account import validate_account
from .group_management import validate_group_id

def validate_friend_add(wxid, room, message, *, self_wxid=None, allow_auto=False):
    validate_account(wxid)
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,126}", wxid):
        raise ValueError("请填写有效的个人微信 ID")
    if room in (None, "") and allow_auto:
        room = ""
    else:
        validate_group_id(room)
    if wxid == self_wxid:
        raise ValueError("不能添加当前机器人账号")
    if not isinstance(message, str) or not message.strip() or "\0" in message:
        raise ValueError("请填写验证消息")
    try:
        encoded = message.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("验证消息包含无效字符") from None
    if len(encoded) > 1023:
        raise ValueError("验证消息超过 1023 个 UTF-8 字节")
    return wxid, room, message
