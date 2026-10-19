"""控制台群管理参数校验；数量上限是当前框架接口限制。"""
from __future__ import annotations

from .account import validate_account
from .text import validate_target

MAX_GROUP_OPERATION_MEMBERS = 8


def validate_group_id(room):
    validate_target(room)
    if not room.endswith("@chatroom"):
        raise ValueError("群管理目标必须是以 @chatroom 结尾的群 ID")
    return room


def validate_group_members(members, *, creating=False, self_wxid=None):
    minimum = 2 if creating else 1
    if (not isinstance(members, (list, tuple)) or
            not minimum <= len(members) <= MAX_GROUP_OPERATION_MEMBERS):
        raise ValueError(f"本框架每次操作需要 {minimum}–{MAX_GROUP_OPERATION_MEMBERS} 个好友 ID")
    result = tuple(validate_account(member) for member in members)
    if any(member in (".", "..") for member in result):
        raise ValueError("好友 ID 无效")
    if len(result) != len(set(result)):
        raise ValueError("好友 ID 不能重复")
    if self_wxid is not None and self_wxid in result:
        raise ValueError("机器人自己的账号不需要放进待邀请好友列表")
    return result
