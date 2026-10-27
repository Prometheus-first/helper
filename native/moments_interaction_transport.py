"""当前桥接上的手动朋友圈互动；不加载 DLL、不重发、不自动操作其他帖子。"""
from __future__ import annotations

import ctypes as c
from pathlib import Path
import re
import threading
import time
import secrets

if __package__:
    from .moments_transport import TEXT_DLL, _BaseConfig, _Channel, _core_profile, _exports, _native
else:
    from moments_transport import TEXT_DLL, _BaseConfig, _Channel, _core_profile, _exports, _native

_NAMES = {name: ('WxMomentsInteraction' + name).encode() for name in ('Start', 'Poll', 'Cancel', 'Close')}
_CAPABILITY = b'WxMomentsInteractionCapabilities'
_TERMINAL = {5, 6, 7, 8, 11, 12, 13}
_PREFLIGHT = {50, 87, 170, 1306}


class Config(c.Structure):
    _fields_ = [('base', _BaseConfig), ('account', c.c_char * 128), ('sns_id', c.c_uint64),
                ('timeout_ms', c.c_uint32), ('flags', c.c_uint32), ('action', c.c_uint32),
                ('reserved', c.c_uint32), ('content', c.c_char * 1024), ('operation_id', c.c_uint64)]


class Poll(c.Structure):
    _fields_ = [(key, c.c_uint32) for key in ('size', 'state', 'tls_start', 'tls_check', 'tasks',
        'retired', 'cleanup_complete', 'reserved', 'attempted', 'native_known', 'native_value',
        'was_liked', 'native_status', 'exception')] + [('operation_id', c.c_uint64)]


assert c.sizeof(Config) == 2744 and c.sizeof(Poll) == 64


def interaction_capability(bridge_path=TEXT_DLL):
    """只读导出和常量机器码，不执行 DLL 探测函数。"""
    import pefile
    try:
        exports, _ = _exports(bridge_path)
        if not (set(_NAMES.values()) | {_CAPABILITY}) <= exports.keys():
            return False
        pe = pefile.PE(str(bridge_path), fast_load=True)
        try:
            return pe.get_data(exports[_CAPABILITY], 6) == b'\xb8\x01\x00\x00\x00\xc3'
        finally:
            pe.close()
    except (OSError, ValueError, pefile.PEFormatError):
        return False


def _config(pid, account, payload):
    from wxbot_framework.account import validate_account
    account = validate_account(account)
    if type(pid) is not int or not 0 < pid <= 0xffffffff or not isinstance(payload, dict):
        raise ValueError('朋友圈互动目标无效')
    sns_id, action, content = payload.get('sns_id'), payload.get('action'), payload.get('content', '')
    if (not isinstance(sns_id, str) or not re.fullmatch(r'[1-9][0-9]{0,19}', sns_id) or
            int(sns_id) > 0xffffffffffffffff or action not in ('like', 'comment')):
        raise ValueError('朋友圈 ID 或互动类型无效')
    if not isinstance(content, str) or '\0' in content:
        raise ValueError('评论内容无效')
    encoded = content.encode('utf-8')
    if (action == 'like' and encoded) or (action == 'comment' and (not content.strip() or len(encoded) > 1023)):
        raise ValueError('评论应为非空文本且不超过 1023 UTF-8 字节')
    cfg = Config()
    cfg.base.size, cfg.base.pid = c.sizeof(cfg), pid
    cfg.account, cfg.sns_id = account.encode('ascii'), int(sns_id)
    cfg.timeout_ms, cfg.action = 30000, 1 if action == 'like' else 2
    cfg.content = encoded
    cfg.operation_id = secrets.randbits(64) or 1
    return cfg


def _result(poll, closed):
    if not closed:
        return {'status': 'unknown', 'unload_blocked': True}
    if poll.state == 13 and not poll.attempted:
        return {'status': 'already_liked', 'unload_blocked': False}
    if (poll.state == 11 and poll.attempted == 1 and poll.native_known == 1 and
            poll.native_value == 1 and not poll.native_status and not poll.exception):
        # The high-level native function completed its network and local update
        # path. No fabricated independent server status code is exposed here.
        return {'status': 'completed', 'unload_blocked': False}
    if poll.state in (6, 7, 8) and not poll.attempted:
        return {'status': 'failed', 'phase': 'preflight', 'unload_blocked': False}
    return {'status': 'unknown', 'unload_blocked': False}


def _reap(pid, bridge_path, operation_id):
    """只处理已存在任务的退出，不调用 Start，不重复点赞或评论。"""
    channel = None
    try:
        loaded = _native().modules(pid).get(Path(bridge_path).name.lower())
        if not loaded or Path(loaded[1]).resolve() != Path(bridge_path).resolve():
            return
        exports, _ = _exports(bridge_path)
        channel = _Channel(pid)
        poll = Poll(); poll.size = c.sizeof(poll); poll.operation_id = operation_id
        ptr = channel.allocate(bytes(poll))
        until = time.monotonic() + 300
        while time.monotonic() < until:
            code = channel.call(loaded[0] + exports[_NAMES['Poll']], ptr)
            if code == 1168:  # Start's remote thread may not have created its lease yet.
                time.sleep(1)
                continue
            if code:
                return
            poll = Poll.from_buffer_copy(channel.read_memory(ptr, c.sizeof(poll)))
            if poll.state == 9:
                return
            if poll.state in _TERMINAL and channel.call(loaded[0] + exports[_NAMES['Close']], ptr) == 0:
                return
            time.sleep(1)
    except Exception:
        pass
    finally:
        if channel is not None:
            channel.close()


def interact_moments(pid, account, payload, *, bridge_path=TEXT_DLL, timeout=90):
    cfg = _config(pid, account, payload)
    if not interaction_capability(bridge_path):
        return {'status': 'failed', 'phase': 'preflight', 'unload_blocked': False}
    from wxbot_framework.account import read_current_account
    mods = _native().modules(pid)
    loaded, core = mods.get(Path(bridge_path).name.lower()), mods.get('weixin.dll')
    if (not loaded or not core or Path(loaded[1]).resolve() != Path(bridge_path).resolve() or
            _core_profile(core[1]) is None):
        return {'status': 'failed', 'phase': 'preflight', 'unload_blocked': False}
    exports, _ = _exports(bridge_path)
    channel = _Channel(pid)
    started = False
    closed = False
    recovery = False
    try:
        channel.verify_client()
        if read_current_account(channel.read_memory, core[0]) != account:
            return {'status': 'failed', 'phase': 'preflight', 'unload_blocked': False}
        parameter = channel.allocate(bytes(cfg))
        # From entering Start, a failure or timeout may leave an accepted task.
        started = True
        code = channel.call(loaded[0] + exports[_NAMES['Start']], parameter)
        if code in _PREFLIGHT:
            started = False
            return {'status': 'failed', 'phase': 'preflight', 'unload_blocked': False}
        poll = Poll(); poll.size = c.sizeof(poll); poll.operation_id = cfg.operation_id
        ptr = channel.allocate(bytes(poll))
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            code = channel.call(loaded[0] + exports[_NAMES['Poll']], ptr)
            if code:
                break
            poll = Poll.from_buffer_copy(channel.read_memory(ptr, c.sizeof(poll)))
            if poll.state == 9:
                break
            if poll.state in _TERMINAL:
                close = channel.call(loaded[0] + exports[_NAMES['Close']], ptr)
                if close == 0:
                    closed = True
                    result = _result(poll, True)
                    if read_current_account(channel.read_memory, core[0]) != account:
                        return {'status': 'unknown', 'unload_blocked': False}
                    return result
            time.sleep(0.15)
        # Cancel stops work that has not entered the interaction, never resends
        # or pretends to abort an already submitted network request.
        channel.call(loaded[0] + exports[_NAMES['Cancel']], ptr)
        recovery = True
        return {'status': 'unknown', 'unload_blocked': True}
    except Exception:
        recovery = started and not closed
        return {'status': 'unknown', 'unload_blocked': not closed} if started else {
            'status': 'failed', 'phase': 'preflight', 'unload_blocked': False}
    finally:
        channel.close()
        if recovery:
            threading.Thread(target=_reap, args=(pid, bridge_path, cfg.operation_id), name='MomentsInteractionCleanup', daemon=True).start()
