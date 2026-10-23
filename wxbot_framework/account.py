"""已核对微信版本当前登录账号的只读快照，不保存账号缓存。"""
from __future__ import annotations

import struct

from .text import SYSTEM_PEERS, validate_target
from .native_profiles import select_memory_profile

# 由该版本的反汇编及当前进程核对；使用前后端已校验核心 DLL SHA256。
APP_GLOBAL_RVA = 0xB912690
APP_VTABLE_RVA = 0x8D804E8
CONTEXT_VTABLE_RVA = 0x8DFE2F8
APP_CONTEXT_GETTER_RVA = 0x469A0  # app 虚表 +0x60，返回 app +0x68 的 shared_ptr。
IDENTITY_GETTER_RVA = 0x36F930   # context 虚表 +0x18，LEA RAX,[RCX+0x48]; RET。


def _pointer(value):
    if not 0x10000 <= value < 0x800000000000:
        raise ValueError("账号对象指针尚未就绪")
    return value


def validate_account(account):
    validate_target(account)
    if account == "filehelper" or account.endswith("@chatroom") or account.startswith("gh_") or account in SYSTEM_PEERS:
        raise ValueError("读取结果不是个人微信账号 ID")
    return account


def select_account_process(candidates, preferred_account=None):
    """只选绑定的账号；只有一个登录账号时允许无绑定自动识别。

    candidates 是 (pid, 当前读取到的账号或 None) 列表。进程 PID 不作为账号绑定。
    """
    if preferred_account is not None:
        validate_account(preferred_account)
        matches = [pid for pid, account in candidates if account == preferred_account]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise RuntimeError("等待绑定的机器人账号登录")
        raise RuntimeError("同一个机器人账号对应多个微信进程，等待唯一的登录会话")
    ready = [(pid, account) for pid, account in candidates if account]
    if len(ready) == 1:
        return ready[0][0]
    if len(ready) > 1:
        raise RuntimeError("多个微信账号同时登录，请用 wechat_account 绑定机器人账号")
    raise RuntimeError("等待微信账号完成登录")


def read_current_account(read_memory, core_base):
    """返回稳定的当前账号 ID；未登录、读取中发生切换时返回 None。

    read_memory(address, length) 必须返回完整字节串，只读取已确认的对象链。
    双读对象链、字符串头和正文，避免登录切换期间使用半份数据。
    """
    def read(address, length):
        value = read_memory(_pointer(address), length)
        if len(value) != length:
            raise ValueError("账号字段读取不完整")
        return value

    def ptr(address):
        return struct.unpack("<Q", read(address, 8))[0]

    try:
        profile = select_memory_profile(read_memory, core_base)
        if profile is None:
            return None
        rva = profile.rva
        app = _pointer(ptr(core_base + rva(APP_GLOBAL_RVA)))
        app_table = ptr(app)
        if app_table != core_base + rva(APP_VTABLE_RVA):
            return None
        if ptr(app_table + 0x60) != core_base + rva(APP_CONTEXT_GETTER_RVA):
            return None
        context = _pointer(ptr(app + 0x68))
        table = ptr(context)
        if table != core_base + rva(CONTEXT_VTABLE_RVA):
            return None
        if ptr(table + 0x18) != core_base + rva(IDENTITY_GETTER_RVA):
            return None
        if read(core_base + rva(IDENTITY_GETTER_RVA), 5) != b"\x48\x8d\x41\x48\xc3":
            return None
        address = context + 0x48
        header = read(address, 32)
        length, capacity = struct.unpack_from("<QQ", header, 16)
        if not 1 <= length <= 127 or capacity < length or not (capacity == 15 or 16 <= capacity <= 1024 * 1024):
            return None
        buffer = address if capacity == 15 else _pointer(struct.unpack_from("<Q", header)[0])
        body = read(buffer, length)
        account = validate_account(body.decode("ascii"))
        if (read(address, 32) != header or read(buffer, length) != body or
                ptr(core_base + rva(APP_GLOBAL_RVA)) != app or ptr(app) != app_table or
                ptr(app + 0x68) != context or ptr(context) != table):
            return None
        return account
    except (OSError, ValueError, struct.error):
        # 退出、登录切换或已释放字段的 ReadProcessMemory 错误由下一次连接重试。
        return None


def read_current_profile(read_memory, core_base):
    """只读当前账号 ID 和微信名；不读取电话等其他资料，不建立群内绑定。"""
    account = read_current_account(read_memory, core_base)
    if not account:
        return None
    try:
        profile = select_memory_profile(read_memory, core_base)
        if profile is None:
            return None
        def read(address, length):
            value = read_memory(_pointer(address), length)
            if len(value) != length:
                raise ValueError("微信名字字段读取不完整")
            return value
        app = struct.unpack("<Q", read(core_base + profile.rva(APP_GLOBAL_RVA), 8))[0]
        context = struct.unpack("<Q", read(app + 0x68, 8))[0]
        address = context + 0x88
        header = read(address, 32)
        length, capacity = struct.unpack_from("<QQ", header, 16)
        if not 1 <= length <= 256 or capacity < length or not (capacity == 15 or 16 <= capacity <= 1048576):
            return None
        buffer = address if capacity == 15 else _pointer(struct.unpack_from("<Q", header)[0])
        body = read(buffer, length)
        name = body.decode("utf-8")
        if not name.strip() or len(name) > 64 or any(ord(ch) < 32 for ch in name):
            return None
        if (read(address, 32) != header or read(buffer, length) != body or
                struct.unpack("<Q", read(core_base + profile.rva(APP_GLOBAL_RVA), 8))[0] != app or
                struct.unpack("<Q", read(app + 0x68, 8))[0] != context or
                read_current_account(read_memory, core_base) != account):
            return None
        return {"wxid": account, "name": name.strip()}
    except (OSError, ValueError, struct.error):
        return None
