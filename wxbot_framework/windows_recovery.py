"""Recover a verified Windows WeChat instance when a bridge call stays on the stack."""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
from functools import lru_cache
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time

from .account import read_current_account
from .bridge_lifecycle import checked, kernel
from .config import account_is_unbound
from .panel_common import PanelError


NATIVE_STALL_MS = 90_000
RECOVERY_COOLDOWN = 600
RECOVERY_WINDOW = 3600
RECOVERY_LIMIT = 3
RECOVERY_LOGIN_TIMEOUT = 300
SLOTS = struct.Struct("<" + "qq" * 16)


@lru_cache(maxsize=1)
def _kernel():
    api = kernel()
    api.ReadProcessMemory.argtypes = [w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)]
    api.ReadProcessMemory.restype = w.BOOL
    api.GetProcessId.argtypes = [w.HANDLE]
    api.GetProcessId.restype = w.DWORD
    api.GetProcessTimes.argtypes = [w.HANDLE] + [c.POINTER(w.FILETIME)] * 4
    api.GetProcessTimes.restype = w.BOOL
    api.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
    api.QueryFullProcessImageNameW.restype = w.BOOL
    api.QueryUnbiasedInterruptTime.argtypes = [c.POINTER(c.c_uint64)]
    api.QueryUnbiasedInterruptTime.restype = w.BOOL
    api.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
    api.TerminateProcess.restype = w.BOOL
    return api


def _creation_time(api, handle):
    stamps = [w.FILETIME() for _ in range(4)]
    checked(api.GetProcessTimes(handle, *(c.byref(item) for item in stamps)), "读取微信进程创建时间")
    return stamps[0].dwHighDateTime, stamps[0].dwLowDateTime


def _active_slots(first, second, now_ms):
    # Two reads exclude a slot that an interlocked writer is replacing.
    a, b = SLOTS.unpack(first), SLOTS.unpack(second)
    return {a[i]: now_ms - a[i + 1] for i in range(0, len(a), 2)
            if a[i] > 0 and a[i:i + 2] == b[i:i + 2] and 0 <= a[i + 1] <= now_ms}


class RecoveryTarget:
    """A dedicated handle pins the same kernel process object while the bridge may be busy."""

    def __init__(self, pid, channel):
        self.api = _kernel()
        self.core = channel.core_base
        self.address = channel.execution_address
        original = channel.process
        if self.api.GetProcessId(original) != pid:
            raise OSError("原接收句柄已变化")
        created = _creation_time(self.api, original)
        # TERMINATE | VM_READ | QUERY_LIMITED_INFORMATION | SYNCHRONIZE
        self.handle = checked(self.api.OpenProcess(0x101011, False, pid), "打开微信恢复句柄")
        try:
            if _creation_time(self.api, self.handle) != created or not self.alive():
                raise OSError("微信进程身份已变化")
            name = c.create_unicode_buffer(32768)
            size = w.DWORD(len(name))
            checked(self.api.QueryFullProcessImageNameW(self.handle, 0, name, c.byref(size)), "读取微信程序路径")
            self.executable = Path(name.value).resolve()
            if self.executable.name.casefold() != "weixin.exe":
                raise OSError("目标不是微信客户端")
        except BaseException:
            self.close()
            raise

    def alive(self):
        return self.api.WaitForSingleObject(self.handle, 0) == 258

    def read(self, address, length):
        data, size = c.create_string_buffer(length), c.c_size_t()
        checked(self.api.ReadProcessMemory(self.handle, address, data, length, c.byref(size)), "读取原生执行状态")
        if size.value != length:
            raise OSError("原生执行状态读取不完整")
        return data.raw

    def active(self):
        if not self.alive():
            return {}
        first = self.read(self.address, SLOTS.size)
        second = self.read(self.address, SLOTS.size)
        now = c.c_uint64()
        checked(self.api.QueryUnbiasedInterruptTime(c.byref(now)), "读取系统执行时间")
        return _active_slots(first, second, now.value // 10_000)

    def account(self):
        return read_current_account(self.read, self.core)

    def terminate(self):
        if self.api.WaitForSingleObject(self.handle, 0) == 0:
            return
        checked(self.api.TerminateProcess(self.handle, 1), "结束卡住的微信进程")
        if self.api.WaitForSingleObject(self.handle, 8000) != 0:
            raise TimeoutError("旧微信进程尚未退出")

    def close(self):
        self.api.CloseHandle(self.handle)


def launch_replacement(executable):
    return subprocess.Popen([str(executable)], cwd=str(executable.parent))


def replacement_account(root, process):
    if process.poll() is not None:
        return None
    from .wechat_login import _native
    _native(root)
    from text_service import inspect_account
    account = inspect_account(process.pid)
    return account if process.poll() is None else None


class WindowsRecovery:
    def __init__(self, panel):
        self.panel = panel
        self.cancel_event = None
        self.status = ""
        self.attempts = {}

    def cancel(self):
        if self.cancel_event is not None:
            self.cancel_event.set()

    def arm(self, app, *, restored=False):
        self.cancel()
        self.status = "restarted" if restored else ""
        self.cancel_event = threading.Event()
        if sys.platform == "win32" and app.config.get("windows_auto_recovery", True) is True:
            threading.Thread(target=self._watch, args=(app, self.cancel_event),
                             name="WindowsWechatRecovery", daemon=True).start()

    def _watch(self, app, cancel):
        backend = target = None
        try:
            while not cancel.wait(2) and not app.stop_event.is_set():
                current = app.backend
                if current is not backend or (target is not None and not target.alive()):
                    if target is not None:
                        target.close()
                    backend, target = current, None
                raw = getattr(backend, "raw", None)
                if not getattr(raw, "_owns_bridge", False):
                    continue
                channel = raw._channel
                if target is None:
                    if channel is None or getattr(channel, "execution_address", None) is None:
                        continue
                    try:
                        target = RecoveryTarget(raw.pid, channel)
                    except (OSError, TypeError, ValueError):
                        continue
                try:
                    stalled = next((identifier for identifier, age in target.active().items()
                                    if age >= NATIVE_STALL_MS), None)
                    if stalled is not None and self._recover(app, backend, cancel, target, stalled):
                        return
                except OSError:
                    continue
        finally:
            if target is not None:
                target.close()

    def _recover(self, app, backend, cancel, target, identifier):
        panel = self.panel
        account = app.config.get("wechat_account")
        with panel.actions:
            with panel.lock:
                try:
                    config = panel.store.load()
                except (OSError, ValueError):
                    return False
                if (cancel.is_set() or panel.stop_event.is_set() or app.stop_event.is_set() or
                        panel.app is not app or app.backend is not backend or not backend.raw._owns_bridge or
                        account_is_unbound(account) or config.get("wechat_account") != account or
                        config.get("windows_auto_recovery", True) is not True):
                    return False
                if target.account() != account or target.active().get(identifier, 0) < NATIVE_STALL_MS:
                    return False
                now = time.monotonic()
                attempts = self.attempts.setdefault(account, [])
                attempts[:] = [when for when in attempts if now - when < RECOVERY_WINDOW]
                if len(attempts) >= RECOVERY_LIMIT or (attempts and now - attempts[-1] < RECOVERY_COOLDOWN):
                    self.status = "limited"
                    return False
                attempts.append(now)
                worker = panel.worker
                self.status = "restarting"
                panel.state = "stopping"
                app.stop_event.set()
                panel._retire_old_send_jobs(None)
            try:
                target.terminate()
                if cancel.is_set() or panel.stop_event.is_set():
                    return True
                from .update_guard import check_update_protection
                check_update_protection(panel.root, config)
                replacement = launch_replacement(target.executable)
                self.status = "waiting_login"
            except (OSError, RuntimeError, ValueError):
                self.status = "failed"
                return True
        self._wait_for_account(replacement, account, cancel, old_worker=worker)
        return True

    def _wait_for_account(self, process, account, cancel, *, old_worker=None):
        deadline = time.monotonic() + RECOVERY_LOGIN_TIMEOUT
        while not cancel.wait(2) and not self.panel.stop_event.is_set():
            if process.poll() is not None or time.monotonic() >= deadline:
                self.status = "manual_login"
                return
            try:
                found = replacement_account(self.panel.root, process)
            except (OSError, RuntimeError, ValueError):
                continue
            if cancel.is_set():
                return
            if found and found != account:
                self.status = "wrong_account"
                return
            if found == account:
                if old_worker is not None and old_worker.is_alive():
                    self.status = "waiting_cleanup"
                    continue
                try:
                    self.panel.start(_replacement=(process, account, cancel))
                except (OSError, RuntimeError, ValueError):
                    if not cancel.is_set():
                        self.status = "failed"
                return

    def matches_replacement(self, process, account):
        try:
            return replacement_account(self.panel.root, process) == account
        except (OSError, RuntimeError, ValueError):
            return False

    def validate_replacement(self, replacement, account):
        process, expected_account, cancel = replacement
        if (cancel is not self.cancel_event or cancel.is_set() or account != expected_account or
                not self.matches_replacement(process, account)):
            raise PanelError("恢复进程或账号已变化，请手动启动机器人", 409)
        return process
