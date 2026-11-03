"""Linux 微信本地资料候选识别；结果不等同于登录状态。"""
from __future__ import annotations

import os
from pathlib import Path
import re

from .account import validate_account

_SUFFIX = re.compile(r"^[0-9a-fA-F]{4,16}$")


def profile_candidates(pid: int, home: Path, *, proc_root: Path = Path("/proc")) -> tuple[str, ...]:
    """只列出被该进程打开了数据库文件的资料目录候选，不读取数据库内容。

    这个结果可能是历史账号或尚未完成登录的账号，不能用于绑定、发信或作为已登录状态。
    """
    if type(pid) is not int or pid <= 0:
        raise ValueError("PID 无效")
    home = Path(home)
    data_root = home / "xwechat_files"
    identity_root = home / ".xwechat"
    if data_root.is_symlink() or identity_root.is_symlink() or not data_root.is_dir() or not identity_root.is_dir():
        return ()
    base = data_root.resolve()
    candidates = {}
    try:
        children = list(data_root.iterdir())
    except OSError:
        return ()
    for directory in children:
        try:
            if directory.is_symlink() or not directory.is_dir():
                continue
            account, separator, suffix = directory.name.rpartition("_")
            if not separator or not _SUFFIX.fullmatch(suffix) or not account.startswith("wxid_"):
                continue
            validate_account(account)
            identity = identity_root / account
            if identity.is_symlink() or not identity.is_dir():
                continue
            path = directory.resolve()
            if not path.is_relative_to(base):
                continue
            candidates[path] = account
        except (OSError, ValueError):
            continue
    found = set()
    try:
        process = Path(proc_root) / str(pid)
        if process.stat().st_uid != os.geteuid():
            return ()
        handles = list((process / "fd").iterdir())
    except OSError:
        return ()
    for handle in handles:
        try:
            target = os.readlink(handle)
            if target.endswith(" (deleted)") or not os.path.isabs(target):
                continue
            path = Path(target).resolve()
            if not any(path.name.endswith(suffix) for suffix in (".db", ".db-wal", ".db-shm")):
                continue
            for directory, account in candidates.items():
                if path.is_relative_to(directory):
                    found.add(account)
        except OSError:
            continue
    return tuple(sorted(found))
