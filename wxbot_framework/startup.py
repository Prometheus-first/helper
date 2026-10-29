"""首次启动的只读账号发现与本地绑定；绑定完成后再加载机器人后端。"""
from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

from .config import account_is_unbound


def scan_logged_accounts(root):
    if sys.platform != "win32" or ctypes.sizeof(ctypes.c_void_p) != 8:
        raise RuntimeError("自动识别账号需要 Windows 和 64 位 Python")
    native = Path(root) / "native"
    if not native.is_dir():
        raise RuntimeError("未找到原生账号识别模块")
    if str(native) not in sys.path:
        sys.path.insert(0, str(native))
    from text_service import inspect_profile, wechat_candidates

    accounts = []
    for pid in wechat_candidates():
        try:
            profile = inspect_profile(pid)
        except OSError:
            # 微信在枚举期间退出、或当前进程无读取权限时不参与选择。
            continue
        if profile:
            row = {"pid": pid, "wxid": profile["wxid"], "name": profile["name"]}
            if profile.get('bridge_supported') is False:
                row.update(bridge_supported=False, wechat_version=profile.get('wechat_version', ''))
            accounts.append(row)
    return sorted(accounts, key=lambda row: row["pid"])


def ensure_account_binding(root, store, *, scan=None, input_fn=None, output=print,
                           sleep=time.sleep, clock=time.monotonic, timeout=0):
    config = store.load()
    if config.get("backend", "native_filehelper") != "native_text" or not account_is_unbound(config.get("wechat_account")):
        return config
    scan = scan or (lambda: scan_logged_accounts(root))
    interactive = input_fn is not None or (config.get("console", True) and sys.stdin is not None and sys.stdin.isatty())
    read_choice = input_fn or input
    started = clock()
    last_wait = None
    require_choice = False
    output("首次启动：请在受支持版本的电脑微信中登录机器人账号，程序会自动识别并保存微信 ID。")

    def candidates():
        rows = scan()
        pid = config.get("wechat_pid")
        return [row for row in rows if pid is None or row["pid"] == pid]

    while True:
        if timeout and clock() - started >= timeout:
            raise TimeoutError("等待账号绑定超时；请登录微信后重新启动")
        rows = candidates()
        if rows and all(row.get('bridge_supported') is False for row in rows):
            raise RuntimeError('当前微信版本仅支持账号识别，消息桥接尚未适配，暂不能启动机器人')
        rows = [row for row in rows if row.get('bridge_supported') is not False]
        if not rows:
            now = clock()
            if last_wait is None or now - last_wait >= 30:
                output("等待微信登录；当前只读取已登录账号信息，尚未启动机器人。Ctrl+C 可退出。")
                last_wait = now
            sleep(min(2, max(0, timeout - (clock() - started))) if timeout else 2)
            continue
        if len(rows) == 1 and not require_choice:
            selected = rows[0]
        else:
            require_choice = True
            output("检测到多个已登录账号，请选择要作为机器人的账号：")
            for index, row in enumerate(rows, 1):
                output(f"{index}. {row['name'] or '昵称暂不可读'} | 微信ID={row['wxid']} | PID={row['pid']}")
            if not interactive:
                raise RuntimeError("多个微信账号同时登录：请在交互终端运行以选择账号，或先只登录机器人账号")
            try:
                choice = read_choice("输入账号序号，r 刷新，q 退出：").strip()
            except EOFError as error:
                raise RuntimeError("账号选择输入已关闭；请重新在交互终端启动") from error
            if choice.lower() in ("q", "quit", "exit"):
                raise KeyboardInterrupt
            if choice.lower() == "r":
                continue
            if not choice.isascii() or not choice.isdecimal() or not 1 <= int(choice) <= len(rows):
                output("序号无效，请重新选择。")
                continue
            selected = rows[int(choice) - 1]
        # 选择期间可能退出登录或切号，保存前再次只读核对，避免绑定过期的列表项。
        confirmed = candidates()
        if not any(row["pid"] == selected["pid"] and row["wxid"] == selected["wxid"] for row in confirmed):
            output("账号状态已变化，重新识别。")
            continue
        if sum(row["wxid"] == selected["wxid"] for row in confirmed) != 1:
            output("同一账号对应多个微信进程，请关闭重复会话后重试。")
            sleep(2)
            continue
        config = store.bind_account(selected["wxid"])
        output(f"机器人账号已绑定：{selected['name'] or '未读取到昵称'} | 微信ID={config['wechat_account']}")
        output("绑定已保存到本地配置；后续启动只连接这个账号。")
        return config
