"""版本锁定的小程序原生 IPC。每个 PID 串行，调用结束即移除观察 Hook。

不使用 SendInput/UIA，不开放调试端口，不记录页面、输入或原生认证头。
"""
from __future__ import annotations

import base64
import ctypes as c
import hashlib
import json
from functools import lru_cache
from pathlib import Path
import re
import struct
import time
import uuid

import pefile

from native.moments_transport import _Channel
from wxbot_framework.account import read_current_account, validate_account

DLL = Path(__file__).resolve().parent / 'build_miniprogram/miniprogram_bridge.dll'
CORE_HASH = '2b3677c3f932daaa4ef84c8b2acd0aee12cbe5a779ff9e740c370761e5c291ce'
HOST_HASH = '331f44c811b5f2489add4ebcec82a79b90ffbeca991aaaab4c16debe1df5f426'
METHODS = frozenset({'XWeb.getCapabilities', 'XWeb.LaunchApplet', 'XWeb.GetAppletPageInfo',
    'XWeb.EvaluateJavaScript', 'DOM.getDocument', 'DOM.getOuterHTML', 'Page.captureScreenshot',
    'Input.dispatchTouchEvent', 'Input.dispatchKeyEvent', 'Input.insertText'})


class MiniProgramError(RuntimeError):
    """已确认失败，或当前构建/页面尚不可用。"""


class MiniProgramOutcomeUnknown(MiniProgramError):
    """已提交但没有最终回执；调用者应先核对页面，不得自动重发。"""


class CommandConfig(c.Structure):
    _fields_ = [('size', c.c_uint32), ('pid', c.c_uint32), ('stage', c.c_uint32),
                ('result', c.c_uint32), ('response_size', c.c_uint32), ('request_size', c.c_uint32),
                ('responses_seen', c.c_uint32), ('largest_response', c.c_uint32),
                ('call_id', c.c_char * 65), ('request', c.c_char * 16384),
                ('response', c.c_char * 1048576)]


assert CommandConfig.request.offset == 97 and CommandConfig.response.offset == 16481
assert c.sizeof(CommandConfig) == 1065060


def _varint(value):
    if type(value) is not int or not 0 <= value < 1 << 64:
        raise ValueError('无效的 protobuf 整数')
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def _string(field, value):
    raw = value.encode('utf-8')
    return _varint(field * 8 + 2) + _varint(len(raw)) + raw


def _fields(raw):
    """跳过原生 envelope；只让调用关联字段及结果进入 Python 对象。"""
    at = 0
    def integer():
        nonlocal at
        value = 0
        for shift in range(0, 70, 7):
            if at >= len(raw):
                break
            byte = raw[at]
            at += 1
            if shift == 63 and byte > 1:
                break
            value |= (byte & 127) << shift
            if not byte & 128:
                return value
        raise MiniProgramError('小程序回执整数无效')
    while at < len(raw):
        key = integer()
        field, wire = key >> 3, key & 7
        if not field:
            raise MiniProgramError('小程序回执字段无效')
        if wire == 0:
            value = integer()
            if field == 4:
                yield field, value
        elif wire == 2:
            length = integer()
            end = at + length
            if end > len(raw):
                raise MiniProgramError('小程序回执长度无效')
            if field in (2, 3, 5):
                yield field, raw[at:end].decode('utf-8')
            at = end
        elif wire in (1, 5):
            at += 8 if wire == 1 else 4
            if at > len(raw):
                raise MiniProgramError('小程序回执长度无效')
        else:
            raise MiniProgramError('小程序回执编码无效')


def encode_request(session_id, call_id, requests, timeout_ms=30000):
    if not re.fullmatch(r'helper_mp_[0-9a-f]{32}', session_id):
        raise ValueError('小程序会话标识无效')
    if not re.fullmatch(r'mp_[0-9a-f]{32}', call_id):
        raise ValueError('小程序调用标识无效')
    if not isinstance(requests, list) or not 1 <= len(requests) <= 8:
        raise ValueError('每次需要 1 到 8 个小程序命令')
    packet = _string(1, session_id) + _string(2, call_id)
    for request in requests:
        if (not isinstance(request, dict) or set(request) != {'method', 'params'} or
                request['method'] not in METHODS or not isinstance(request['params'], dict)):
            raise ValueError('不支持的小程序命令')
        packet += _string(3, json.dumps(request, ensure_ascii=False, separators=(',', ':'), allow_nan=False))
    packet += _varint(40) + _varint(timeout_ms)
    payload = json.dumps({'call_id': call_id, 'req_args': base64.b64encode(packet).decode('ascii')},
                         separators=(',', ':')).encode('utf-8')
    if len(payload) >= 16384:
        raise ValueError('单次小程序请求过长，请拆分输入或脚本')
    return payload


def decode_response(raw, session_id, call_id, count):
    try:
        response = json.loads(raw)
        if not isinstance(response, dict) or response.get('call_id') != call_id:
            raise MiniProgramError('小程序回执调用标识不匹配')
        fields = list(_fields(base64.b64decode(response['resp_args'], validate=True)))
        for field, expected in ((2, session_id), (3, call_id)):
            if [value for number, value in fields if number == field] != [expected]:
                raise MiniProgramError('小程序回执会话标识不匹配')
        errors = [value for number, value in fields if number == 4]
        if len(errors) > 1 or any(errors):
            raise MiniProgramOutcomeUnknown('小程序命令未在期限内完成，请先核对页面')
        results = [json.loads(value) for field, value in fields if field == 5]
        if len(results) != count:
            raise MiniProgramOutcomeUnknown('小程序命令回执不完整，请先核对页面')
        output = []
        for result in results:
            if (not isinstance(result, dict) or type(result.get('code')) is not int or
                    result['code'] != 0):
                # 不向日志或异常复制网页返回的正文、堆栈、凭据。
                code = result.get('code') if isinstance(result, dict) else None
                code = code if type(code) is int else 'invalid'
                raise MiniProgramError('小程序命令失败，原生错误码：' + str(code))
            envelope = result.get('data')
            data = envelope.get('data') if isinstance(envelope, dict) else None
            if not isinstance(data, dict):
                raise MiniProgramError('小程序命令回执结构无效')
            output.append(data)
        return output
    except (ValueError, TypeError, KeyError, UnicodeError):
        raise MiniProgramError('小程序回执无法解析') from None


class _ProcessMutex:
    def __init__(self, pid):
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        for name, args, result in (
            ('CreateMutexW', [c.c_void_p, c.c_int, c.c_wchar_p], c.c_void_p),
            ('WaitForSingleObject', [c.c_void_p, c.c_uint32], c.c_uint32),
            ('ReleaseMutex', [c.c_void_p], c.c_int), ('CloseHandle', [c.c_void_p], c.c_int)):
            fn = getattr(self.kernel, name)
            fn.argtypes, fn.restype = args, result
        self.handle = self.kernel.CreateMutexW(None, False, 'Local\\HelperMiniProgram-' + str(pid))
        self.owned = False
        if not self.handle:
            raise MiniProgramError('无法锁定小程序通道')
    def __enter__(self):
        self.owned = self.kernel.WaitForSingleObject(self.handle, 0) in (0, 128)
        if not self.owned:
            self.kernel.CloseHandle(self.handle)
            self.handle = None
            raise MiniProgramError('该微信的小程序操作正在执行，请稍后重试')
        return self
    def __exit__(self, *_):
        if self.handle:
            if self.owned:
                self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle)
            self.handle = None


@lru_cache(maxsize=16)
def _digest(path, size, mtime):
    with Path(path).open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    current = Path(path).stat()
    if (current.st_size, current.st_mtime_ns) != (size, mtime):
        raise MiniProgramError('微信模块已变化，请重新连接')
    return digest


def _verify_module(path, size, digest):
    path = Path(path).resolve(strict=True)
    stat = path.stat()
    if stat.st_size != size or _digest(str(path), stat.st_size, stat.st_mtime_ns) != digest:
        raise MiniProgramError('小程序原生接口目前仅适配微信 4.2.0.12 和指定小程序运行库')


def _native():
    from native import run_selftest
    return run_selftest


def _exports(path):
    path = Path(path).resolve(strict=True)
    image = pefile.PE(str(path), fast_load=True)
    try:
        if image.FILE_HEADER.Machine != 0x8664:
            raise MiniProgramError('小程序桥接必须为 x64 DLL')
        image.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXPORT']])
        result = {item.name: item.address for item in image.DIRECTORY_ENTRY_EXPORT.symbols
                  if item.name and not item.forwarder}
        if not {b'MpCommand', b'MpStopCommand', b'MpCanUnload'} <= result.keys():
            raise MiniProgramError('小程序桥接导出不完整')
        return result
    finally:
        image.close()


def _check_account(channel, core, account):
    from wxbot_framework.native_profiles import profile_for_fingerprint
    profile = profile_for_fingerprint(205932072, CORE_HASH)
    current = read_current_account(channel.read_memory, core, profiles=(profile,))
    if not current or current != account:
        raise MiniProgramError('微信账号已变化，取消小程序操作')


def _stop(channel, loaded, exports):
    if channel.pending:
        return False
    try:
        return (channel.call(loaded + exports[b'MpStopCommand']) == 0 and
                channel.call(loaded + exports[b'MpCanUnload']) == 0)
    except BaseException:
        channel.pending = True
        raise


def _exchange(channel, loaded, exports, pid, session, requests, cancelled):
    call = 'mp_' + uuid.uuid4().hex
    payload = encode_request(session, call, requests)
    config = CommandConfig(size=c.sizeof(CommandConfig), pid=pid, request_size=len(payload),
                           call_id=call.encode('ascii'), request=payload)
    address = channel.allocate(bytes(config))
    submitted = False
    try:
        if cancelled():
            raise MiniProgramError('小程序操作已取消')
        admission = channel.call(loaded + exports[b'MpCommand'], address)
        if admission:
            raise MiniProgramError('小程序通道暂不可用，原生错误码：' + str(admission))
        submitted = True
        deadline = time.monotonic() + 35
        while time.monotonic() < deadline:
            if cancelled():
                raise MiniProgramOutcomeUnknown('操作已取消，请先核对小程序页面')
            header = struct.unpack('<8I', channel.read_memory(address, 32))
            if header[2] == 3:
                raise MiniProgramOutcomeUnknown('小程序回执超过缓冲区，请缩小读取范围')
            if header[2] == 2:
                length = header[4]
                if not 0 < length < 1048576:
                    raise MiniProgramError('小程序回执长度无效')
                raw = channel.read_memory(address + CommandConfig.response.offset, length)
                return decode_response(raw, session, call, len(requests))
            time.sleep(.03)
        raise MiniProgramOutcomeUnknown('小程序没有及时返回结果，请先核对页面')
    except (TimeoutError, KeyboardInterrupt):
        if submitted or channel.pending:
            raise MiniProgramOutcomeUnknown('小程序操作结果不确定，请先核对页面') from None
        raise
    finally:
        if not _stop(channel, loaded, exports):
            channel.pending = True
            raise MiniProgramOutcomeUnknown('小程序桥接尚未安全停止，保留模块等待清理')


def execute_commands(pid, account, app_id, session_id, requests, *, check_app=True,
                     cancelled=lambda: False, bridge_path=DLL):
    """从框架连接取得 PID/账号；绝不自动选择另一个已登录账号。"""
    if type(pid) is not int or not 0 < pid <= 0xffffffff:
        raise ValueError('微信进程编号无效')
    validate_account(account)
    if not isinstance(app_id, str) or not re.fullmatch(r'wx[0-9a-f]{16}', app_id):
        raise ValueError('小程序 AppID 无效')
    # 参数在打开目标进程前验证。
    encode_request(session_id, 'mp_' + '0' * 32, requests)
    bridge = Path(bridge_path).resolve(strict=True)
    exports = _exports(bridge)
    with _ProcessMutex(pid):
        modules = _native().modules(pid)
        if bridge.name.lower() in modules:
            raise MiniProgramError('该微信存在未清理的小程序桥接，请先停止机器人完成清理')
        core, host = modules.get('weixin.dll'), modules.get('wmpf_host_export_x64.dll')
        if not core or not host:
            raise MiniProgramError('请先在此微信打开任意小程序，等待原生运行库加载')
        _verify_module(core[1], 205932072, CORE_HASH)
        _verify_module(host[1], 6189608, HOST_HASH)
        channel, loaded = _Channel(pid), 0
        try:
            channel.verify_client()
            _check_account(channel, core[0], account)
            if cancelled():
                raise MiniProgramError('小程序操作已取消')
            parameter = channel.allocate(str(bridge).encode('utf-16-le') + b'\0\0')
            channel.system(b'LoadLibraryW', parameter)
            entry = _native().modules(pid).get(bridge.name.lower())
            if not entry or Path(entry[1]).resolve() != bridge:
                raise MiniProgramError('小程序桥接加载未确认')
            loaded = entry[0]
            if check_app:
                info = _exchange(channel, loaded, exports, pid, session_id,
                    [{'method': 'XWeb.GetAppletPageInfo', 'params': {}}], cancelled)[0]
                if info.get('app_id') != app_id:
                    raise MiniProgramError('当前小程序已切换，请重新打开目标小程序')
            _check_account(channel, core[0], account)
            result = _exchange(channel, loaded, exports, pid, session_id, requests, cancelled)
            _check_account(channel, core[0], account)
            return result
        finally:
            try:
                if loaded:
                    if _stop(channel, loaded, exports):
                        channel.system(b'FreeLibrary', loaded)
                    else:
                        channel.pending = True
                        raise MiniProgramOutcomeUnknown('小程序桥接暂不能卸载，请先完成停止清理')
            finally:
                channel.close()


def cleanup_miniprogram_bridge(pid, *, bridge_path=DLL):
    """正常退出、异常退出守护和下次启动共用；不接管其他项目的 DLL。"""
    bridge = Path(bridge_path).resolve()
    with _ProcessMutex(pid):
        entry = _native().modules(pid).get(bridge.name.lower())
        if not entry:
            return {'dll_unloaded': True, 'code': 0}
        if Path(entry[1]).resolve() != bridge:
            raise MiniProgramError('微信中加载了其他项目的小程序桥接')
        exports = _exports(bridge)
        channel = _Channel(pid)
        try:
            channel.verify_client()
            if not _stop(channel, entry[0], exports):
                channel.pending = True
                raise MiniProgramError('小程序操作仍在结束，暂不卸载桥接')
            channel.system(b'FreeLibrary', entry[0])
            if bridge.name.lower() in _native().modules(pid):
                raise MiniProgramError('小程序桥接卸载尚未确认')
            return {'dll_unloaded': True, 'code': 0}
        finally:
            channel.close()
