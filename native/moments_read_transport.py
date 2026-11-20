"""Read the first server page through an account-bound native SNS operation."""
from __future__ import annotations

import ctypes as c
from pathlib import Path
import secrets
import threading
import time

from native.moments_transport import TEXT_DLL, _Channel, _core_profile, _exports, _local_path, _native
from wxbot_framework.account import read_current_account, validate_account
from wxbot_framework.moments_reader import MomentsReadError, decode_moments_vector

_START = b'WxStartMomentsRead'
_POLL = b'WxPollMomentsRead'
_CLOSE = b'WxCloseMomentsRead'
_WAIT = b'WxWaitForTasks'
_TERMINAL = {2, 3}


class Config(c.Structure):
    _fields_ = [('size', c.c_uint32), ('pid', c.c_uint32), ('account', c.c_char * 128),
                ('operation_id', c.c_uint64)]


class Poll(c.Structure):
    _fields_ = [('size', c.c_uint32), ('state', c.c_uint32), ('attempted', c.c_uint32),
                ('error_type', c.c_uint32), ('native_code', c.c_int32), ('exception', c.c_uint32),
                ('operation_id', c.c_uint64), ('begin', c.c_uint64), ('count', c.c_uint64)]


assert c.sizeof(Config) == 144 and c.sizeof(Poll) == 48


def _reap(pid, bridge, operation_id, owned):
    """Close the original operation after timeout or cancellation, without resubmission."""
    channel = None
    try:
        loaded = _native().modules(pid).get(bridge.name.lower())
        if not loaded or Path(loaded[1]).resolve() != bridge:
            return
        exports, _ = _exports(bridge)
        channel = _Channel(pid)
        poll = Poll(size=c.sizeof(Poll), operation_id=operation_id)
        pointer = channel.allocate(bytes(poll))
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            code = channel.call(loaded[0] + exports[_POLL], pointer)
            if code == 1168:
                time.sleep(.25)
                continue
            if code:
                return
            result = Poll.from_buffer_copy(channel.read_memory(pointer, c.sizeof(Poll)))
            if result.operation_id != operation_id or result.size != c.sizeof(Poll):
                return
            if result.state == 4:
                return
            # Close also cancels a still queued request. The native lease stays
            # alive while a submitted callable or synchronous query owns it.
            code = channel.call(loaded[0] + exports[_CLOSE], pointer)
            if code == 0:
                if owned and channel.call(loaded[0] + exports[_WAIT]) == 0:
                    channel.system(b'FreeLibrary', loaded[0])
                return
            if code != 170:
                return
            time.sleep(.25)
    except Exception:
        pass
    finally:
        if channel is not None:
            channel.close()


def read_fresh_moments(pid, expected_account, *, bridge_path=TEXT_DLL, limit=20,
                       cancelled=lambda: False, timeout=75, keep_loaded=True):
    account = validate_account(expected_account)
    if (type(pid) is not int or not 0 < pid <= 0xffffffff or type(limit) is not int or
            not 1 <= limit <= 50 or not callable(cancelled) or
            type(timeout) not in (int, float) or not 0 < timeout <= 120):
        raise ValueError('朋友圈读取参数无效')
    bridge = _local_path(Path(bridge_path).absolute(), suffix={'.dll'})
    modules = _native().modules(pid)
    core = modules.get('weixin.dll')
    profile = _core_profile(core[1]) if core else None
    if profile is None or profile.version != '4.2.0.12':
        raise MomentsReadError('主动刷新朋友圈仅适配微信 4.2.0.12')
    exports, _ = _exports(bridge)
    if not {_START, _POLL, _CLOSE, _WAIT} <= exports.keys():
        raise MomentsReadError('请更新朋友圈桥接并重新启动机器人')
    existing = modules.get(bridge.name.lower())
    if existing and Path(existing[1]).resolve() != bridge:
        raise MomentsReadError('朋友圈桥接来自其他项目目录')
    if keep_loaded and not existing:
        raise MomentsReadError('当前机器人未持有朋友圈桥接')
    if not keep_loaded and existing:
        raise MomentsReadError('独立读取不能接管已运行的朋友圈桥接')
    channel = _Channel(pid)
    loaded, owned, submitted, closed = existing, False, False, False
    operation_id = secrets.randbits(64) or 1
    poll_pointer = cleanup_pointer = None
    result = None
    try:
        channel.verify_client()
        if cancelled() or read_current_account(channel.read_memory, core[0]) != account:
            raise MomentsReadError('朋友圈账号或连接已变化')
        if loaded is None:
            channel.system(b'LoadLibraryW', channel.allocate((str(bridge) + '\0').encode('utf-16-le')))
            loaded = _native().modules(pid).get(bridge.name.lower())
            if not loaded or Path(loaded[1]).resolve() != bridge:
                raise MomentsReadError('朋友圈桥接加载失败')
            owned = True
        config = Config(size=c.sizeof(Config), pid=pid, account=account.encode('ascii'),
                        operation_id=operation_id)
        parameter = channel.allocate(bytes(config))
        poll = Poll(size=c.sizeof(Poll), operation_id=operation_id)
        poll_pointer = channel.allocate(bytes(poll))
        cleanup_pointer = channel.allocate(bytes(poll))
        if cancelled() or read_current_account(channel.read_memory, core[0]) != account:
            raise MomentsReadError('朋友圈账号或连接已变化')
        submitted = True
        code = channel.call(loaded[0] + exports[_START], parameter)
        if code:
            if code in (50, 87, 170, 1306):
                submitted = False
            raise MomentsReadError('朋友圈请求未能提交，请稍后重试')
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancelled() or read_current_account(channel.read_memory, core[0]) != account:
                raise MomentsReadError('朋友圈账号或连接已变化，读取已取消')
            if channel.call(loaded[0] + exports[_POLL], poll_pointer):
                raise MomentsReadError('朋友圈读取状态暂不可用')
            poll = Poll.from_buffer_copy(channel.read_memory(poll_pointer, c.sizeof(Poll)))
            if poll.operation_id != operation_id or poll.size != c.sizeof(Poll):
                raise MomentsReadError('朋友圈回包身份不匹配')
            if poll.state == 2:
                if poll.attempted != 1 or poll.error_type or poll.native_code or poll.exception:
                    raise MomentsReadError('微信未确认本次朋友圈读取成功')
                result = decode_moments_vector(channel.read_memory, poll.begin, poll.count,
                    account, core[0], limit=limit, cancelled=cancelled)
                if cancelled() or read_current_account(channel.read_memory, core[0]) != account:
                    raise MomentsReadError('朋友圈账号或连接已变化，已丢弃读取结果')
                break
            if poll.state in (3, 4):
                raise MomentsReadError('微信未能完成朋友圈刷新，请稍后重试')
            if poll.state != 1:
                raise MomentsReadError('朋友圈读取状态无效')
            time.sleep(.05)
        if result is None:
            raise MomentsReadError('朋友圈刷新超时，本次未取得已确认的服务端结果')
    except (OSError, TimeoutError) as error:
        raise MomentsReadError('朋友圈连接中断，本次读取结果无法确认') from error
    finally:
        if submitted and cleanup_pointer is not None and not channel.pending:
            for _ in range(20):
                try:
                    code = channel.call(loaded[0] + exports[_CLOSE], cleanup_pointer)
                except (OSError, TimeoutError):
                    break
                if code == 0:
                    closed = True
                    break
                if code != 170:
                    break
                time.sleep(.05)
        if submitted and not closed:
            try:
                threading.Thread(target=_reap, args=(pid, bridge, operation_id, owned),
                                 name='MomentsReadCleanup', daemon=True).start()
            except RuntimeError:
                pass
        elif owned and loaded and not channel.pending:
            try:
                if channel.call(loaded[0] + exports[_WAIT]) == 0:
                    channel.system(b'FreeLibrary', loaded[0])
            except (OSError, TimeoutError):
                pass
        channel.close()
    return result
