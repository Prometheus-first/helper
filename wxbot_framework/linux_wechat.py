"""Linux 微信进程只读发现；发现结果不代表已登录或已连接收发桥接。"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

# 候选版本仅标识样本，不意味着消息功能已经适配。
KNOWN_EXECUTABLES = {
    "2749535f299ba67feb8ef80cab898bb0531fa8f376b16f8a72db706811c84132": "4.1.13.26",
}


def executable_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_wechat(proc_root: Path = Path("/proc")) -> list[dict]:
    """仅从当前 Linux 系统枚举微信主进程，不读取账号、消息和登录状态。"""
    results = []
    for entry in proc_root.iterdir():
        if not entry.name.isascii() or not entry.name.isdecimal():
            continue
        try:
            executable = entry / "exe"
            target = Path(os.readlink(executable))
            if target.name != "wechat":
                continue
            args = (entry / "cmdline").read_bytes().split(b"\0")
            # 排除 Chromium/Qt 帮助进程，不把它们误认为单独的登录账号。
            if any(arg.startswith(b"--type=") for arg in args[1:]):
                continue
            fingerprint = executable_sha256(executable)
            results.append({"pid": int(entry.name), "path": str(target),
                            "sha256": fingerprint,
                            "version": KNOWN_EXECUTABLES.get(fingerprint),
                            "account": None, "bridge_supported": False})
        except (OSError, ValueError):
            # 进程可能在枚举过程中退出或当前用户没有访问权限。
            continue
    return sorted(results, key=lambda row: row["pid"])
