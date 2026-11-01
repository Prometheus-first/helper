"""版本锁定的公众号主动读取通道；响应和分页游标只经过内存。"""
from __future__ import annotations

import ctypes as c
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit

import pefile

from .moments_transport import _Channel as _BaseChannel, _local_path

ROOT = Path(__file__).resolve().parent.parent
ARTICLE_DLL = ROOT / 'native/build_article/article_bridge.dll'
MAX_RESPONSE = 2 * 1024 * 1024
MAX_OFFSET = 8192
_CORE_SIZE = 205929008
_CORE_HASH = '745cdf2955fa5128cdb00c056c97571156a5f107406a9bb15ac0754f3246b201'
_HOST_SIZE = 6189608
_HOST_HASH = '6444b98761a5e532a9fa5b628243ff4a80d784fe7615004af259405c3941209b'
_FEED = b'weixin://resourceid/SubscriptionDisorder/disorder.html'
_START, _STOP, _ABI = b'WxArticleStart', b'WxArticleStop', b'WxArticleBridgeAbi'
_LOCKS, _LOCKS_GUARD = {}, threading.Lock()


class ArticleTransportError(RuntimeError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


class ArticleConfig(c.Structure):
    _fields_ = [('size', c.c_uint32), ('pid', c.c_uint32), ('operation', c.c_uint32), ('timeout_ms', c.c_uint32),
                ('frame', c.c_uint32), ('stage', c.c_uint32), ('status', c.c_uint32), ('response_size', c.c_uint32),
                ('request_id', c.c_uint64), ('response_address', c.c_uint64),
                ('response_capacity', c.c_uint32), ('offset_present', c.c_uint32),
                ('login_root', c.c_uint64), ('login_context', c.c_uint64),
                ('account', c.c_char * 128), ('publisher', c.c_char * 128), ('nonce', c.c_char * 33),
                ('offset', c.c_char * 8193), ('url', c.c_char * 4097)]


assert c.sizeof(ArticleConfig) == 12656
assert ArticleConfig.stage.offset == 20 and ArticleConfig.account.offset == 72
assert ArticleConfig.offset.offset == 361 and ArticleConfig.url.offset == 8554


def _native():
    # Package import does not depend on the bot having modified sys.path already.
    from . import run_selftest
    return run_selftest


class _Channel(_BaseChannel):
    def __init__(self, pid):
        self.api, self.pid = _native(), pid
        self.process = self.api.check(self.api.K.OpenProcess(0x43a, False, pid), '打开公众号原生通道')
        self.allocations, self.pending = [], False


class _ProcessMutex:
    """One selected PID is serialized across independent RobotWorker processes."""
    def __init__(self, pid):
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [c.c_void_p, c.c_int, c.c_wchar_p]
        self.kernel.CreateMutexW.restype = c.c_void_p
        self.kernel.WaitForSingleObject.argtypes = [c.c_void_p, c.c_uint32]
        self.kernel.WaitForSingleObject.restype = c.c_uint32
        self.kernel.ReleaseMutex.argtypes = [c.c_void_p]
        self.kernel.ReleaseMutex.restype = c.c_int
        self.kernel.CloseHandle.argtypes = [c.c_void_p]
        self.kernel.CloseHandle.restype = c.c_int
        self.handle = self.kernel.CreateMutexW(None, False, 'Local\\HelperArticleRead-' + str(pid))
        self.owned = False
        if not self.handle:
            raise ArticleTransportError('暂时无法锁定公众号读取通道，请稍后重试')
    def acquire(self):
        status = self.kernel.WaitForSingleObject(self.handle, 0)
        self.owned = status in (0, 128)  # An abandoned previous worker still needs the module-presence guard.
        if status not in (0, 128, 258):
            raise ArticleTransportError('暂时无法锁定公众号读取通道，请稍后重试')
        return self.owned
    def close(self):
        if self.handle:
            if self.owned:
                self.kernel.ReleaseMutex(self.handle)
            self.kernel.CloseHandle(self.handle)
            self.handle, self.owned = None, False


@lru_cache(maxsize=16)
def _file_hash(path, size, modified):
    selected = _local_path(Path(path), suffix={'.dll'})
    with selected.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    info = selected.stat()
    if (info.st_size, info.st_mtime_ns) != (size, modified):
        raise ArticleTransportError('微信模块已变化，请重新读取')
    return digest


def _fingerprint(path, expected_size, expected_hash):
    selected = _local_path(Path(path), suffix={'.dll'})
    info = selected.stat()
    if info.st_size != expected_size or _file_hash(str(selected), info.st_size, info.st_mtime_ns) != expected_hash:
        raise ArticleTransportError('当前微信或公众号浏览器版本尚未适配主动读取', 501)
    return str(selected), info.st_size, info.st_mtime_ns


def _exports(path):
    image = pefile.PE(str(path), fast_load=True)
    try:
        if image.FILE_HEADER.Machine != 0x8664:
            raise ArticleTransportError('公众号原生通道需要 x64 DLL', 501)
        image.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXPORT']])
        exports = {symbol.name: symbol.address for symbol in getattr(getattr(image, 'DIRECTORY_ENTRY_EXPORT', None), 'symbols', ())
                   if symbol.name and not symbol.forwarder}
        if not {_START, _STOP, _ABI}.issubset(exports):
            raise ArticleTransportError('公众号原生通道文件版本无效，请重新构建', 501)
        # The version is an explicit pure constant export; never execute a local DLL to inspect it.
        if image.get_data(exports[_ABI], 6) != b'\xb8\x01\x00\x00\x00\xc3':
            raise ArticleTransportError('公众号原生通道 ABI 尚未适配', 501)
        return exports
    finally:
        image.close()


def _pointer(read, address):
    if not 0x10000 <= address < 0x800000000000 - 8:
        raise ValueError('invalid pointer')
    result = read(address, 8)
    if len(result) != 8:
        raise ValueError('incomplete pointer')
    return struct.unpack('<Q', result)[0]


def _feed_string(read, node):
    header = read(node + 0xe8, 24)
    if len(header) != 24:
        return None
    heap = bool(header[23] & 128)
    length = struct.unpack_from('<Q', header, 8)[0] if heap else header[23]
    capacity = struct.unpack_from('<Q', header, 16)[0] & 0x7fffffffffffffff if heap else 22
    if not len(_FEED) <= length <= 4096 or capacity < length:
        return None
    data = struct.unpack_from('<Q', header)[0] if heap else node + 0xe8
    # Deliberately exclude the query and fragment from the read and returned identity.
    prefix = read(data, len(_FEED) + (length > len(_FEED)))
    if prefix not in (_FEED, _FEED + b'?') or read(node + 0xe8, 24) != header:
        return None
    return header, data, prefix


def _tree(read, root, count):
    if type(count) is not int or not 1 <= count <= 128:
        raise ValueError('invalid registry size')
    pending, seen, rows = [(root, None, None, None)], set(), []
    while pending:
        node, parent, low, high = pending.pop()
        if not node:
            continue
        if len(seen) >= count or node in seen or not 0x10000 <= node < 0x800000000000 - 0x100:
            raise ValueError('invalid registry nodes')
        seen.add(node)
        header = read(node, 0x30)
        if len(header) != 0x30:
            raise ValueError('incomplete registry')
        left, right, actual_parent = struct.unpack_from('<QQQ', header)
        key = struct.unpack_from('<I', header, 0x20)[0]
        if parent is not None and actual_parent != parent:
            raise ValueError('registry parent changed')
        if (low is not None and key <= low) or (high is not None and key >= high):
            raise ValueError('invalid registry order')
        rows.append((node, key, struct.unpack_from('<Q', header, 0x28)[0], header))
        if right:
            pending.append((right, node, key, high))
        if left:
            pending.append((left, node, low, key))
    if len(seen) != count:
        raise ValueError('registry changed')
    return rows


def _frame_binding(read, host):
    manager = _pointer(read, host + 0x5d3720)
    if not manager or _pointer(read, manager) != host + 0x4e71e0 or _pointer(read, host + 0x4e71e0 + 0x58) != host + 0x1cb6b0:
        raise ValueError('host manager unavailable')
    entries, handlers = read(manager + 0xa0, 24), read(manager + 0x40, 24)
    if len(entries) != 24 or len(handlers) != 24:
        raise ValueError('incomplete host registries')
    _, root, count = struct.unpack('<QQQ', entries)
    _, handler_root, handler_count = struct.unpack('<QQQ', handlers)
    creation = _tree(read, root, count)
    live = _tree(read, handler_root, handler_count)
    matches = []
    for node, key, table, header in creation:
        if not 0 < key <= 0x7fffffff or table != host + 0x4ea3e0:
            continue
        url = _feed_string(read, node)
        selected = [row for row in live if row[1] == key and row[2]]
        if url and len(selected) == 1:
            matches.append((node, key, header, url, selected[0]))
    if len(matches) != 1:
        raise ValueError('subscription host not unique')
    node, key, header, url, (handler_node, _, _, handler_header) = matches[0]
    if (read(manager + 0xa0, 24) != entries or read(manager + 0x40, 24) != handlers or
            read(node, 0x30) != header or read(handler_node, 0x30) != handler_header or
            _pointer(read, host + 0x5d3720) != manager or _pointer(read, manager) != host + 0x4e71e0 or
            _feed_string(read, node) != url):
        raise ValueError('subscription host changed')
    return key, manager, node, handler_node


@dataclass(frozen=True)
class _Snapshot:
    identity: tuple
    frame: int
    core: int
    host: int
    login_root: int
    login_context: int


def _inputs(pid, account, publisher=None):
    from wxbot_framework.account import validate_account
    if type(pid) is not int or not 0 < pid <= 0xffffffff:
        raise ArticleTransportError('目标微信进程无效', 400)
    try:
        validate_account(account)
        encoded = account.encode('ascii')
    except (ValueError, TypeError, AttributeError, UnicodeError):
        raise ArticleTransportError('所选微信账号无效', 400) from None
    if len(encoded) >= 128:
        raise ArticleTransportError('所选微信账号无效', 400)
    if publisher is not None and (not isinstance(publisher, str) or not re.fullmatch(r'gh_[A-Za-z0-9_.-]{1,124}', publisher)):
        raise ArticleTransportError('公众号身份无效，请重新选择', 400)


def _snapshot(pid, account, *, require_frame=True):
    _inputs(pid, account)
    if sys.platform != 'win32':
        raise ArticleTransportError('公众号主动读取仅支持 Windows', 501)
    from wxbot_framework.account import read_current_account
    from wxbot_framework.moments_reader import _Memory
    from wxbot_framework.native_profiles import profile_for_fingerprint
    from wxbot_framework.official_accounts import _login_snapshot
    modules = _native().modules(pid)
    core, host = modules.get('weixin.dll'), modules.get('wmpf_host_export_x64.dll')
    if not core or not host:
        raise ArticleTransportError('公众号浏览器尚未就绪，请保持微信登录后重试')
    core_file = _fingerprint(core[1], _CORE_SIZE, _CORE_HASH)
    host_file = _fingerprint(host[1], _HOST_SIZE, _HOST_HASH)
    profile = profile_for_fingerprint(_CORE_SIZE, _CORE_HASH)
    memory = _Memory(pid)
    try:
        before = read_current_account(memory.read, core[0], profiles=(profile,))
        login = _login_snapshot(memory.read, core[0])
        if before != account or not login:
            raise ArticleTransportError('微信登录会话已变化，请重新选择账号')
        frame = _frame_binding(memory.read, host[0])[0] if require_frame else 0
        latest_modules = _native().modules(pid)
        if (read_current_account(memory.read, core[0], profiles=(profile,)) != before or
                _login_snapshot(memory.read, core[0]) != login or
                latest_modules.get('weixin.dll') != core or latest_modules.get('wmpf_host_export_x64.dll') != host):
            raise ArticleTransportError('微信登录会话已变化，请重新选择账号')
        # PID reuse changes the process-creation time even if its selected account is identical.
        kernel = memory.kernel
        get_times = kernel.GetProcessTimes
        get_times.argtypes = [c.c_void_p, c.POINTER(c.c_uint64), c.POINTER(c.c_uint64), c.POINTER(c.c_uint64), c.POINTER(c.c_uint64)]
        get_times.restype = c.c_int
        creation, exit_time, kernel_time, user_time = (c.c_uint64() for _ in range(4))
        if not get_times(memory.handle, c.byref(creation), c.byref(exit_time), c.byref(kernel_time), c.byref(user_time)):
            raise ArticleTransportError('无法核对微信会话，请稍后重试')
        identity = (pid, creation.value, core[0], core_file, host[0], host_file, tuple(login))
        return _Snapshot(identity, frame, core[0], host[0], login[0], login[2])
    except ArticleTransportError:
        raise
    except (OSError, ValueError, struct.error, OverflowError):
        raise ArticleTransportError('微信公众号组件尚未就绪，请稍后重试') from None
    finally:
        memory.close()


def session_identity(pid, account):
    """只读会话标识；不包含页面游标、URL 查询或认证凭据。"""
    return _snapshot(pid, account, require_frame=False).identity


def article_capabilities(pid, account, *, bridge_path=ARTICLE_DLL):
    try:
        bridge = _local_path(bridge_path, suffix={'.dll'})
        _exports(bridge)
        _snapshot(pid, account)
        return {'supported': True, 'version': '4.2.0.11', 'reason': ''}
    except ArticleTransportError as error:
        return {'supported': False, 'reason': str(error)}
    except (OSError, ValueError, pefile.PEFormatError):
        return {'supported': False, 'reason': '公众号主动读取通道未安装，请重新构建或更新项目'}


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate field')
        value[key] = item
    return value


def _decode_open(raw, nonce):
    text = raw.decode('utf-8')
    decoder = json.JSONDecoder(object_pairs_hook=_object)
    for match in list(re.finditer(r'\{', text))[:128]:
        try:
            value, _ = decoder.raw_decode(text[match.start():])
        except (ValueError, RecursionError):
            continue
        if isinstance(value, dict) and value.get('__callback_id') == 'helper_' + nonce and value.get('__msg_type') == 'callback':
            result = value.get('__params')
            if isinstance(result, dict) and str(result.get('err_msg', '')).lower() == 'openurlwithextrawebview:ok':
                return {'opened': True}
            raise ArticleTransportError('微信未能打开这篇文章，请稍后重试')
    raise ArticleTransportError('微信文章加载回执无效，请稍后重试')


def _decode_history(raw, publisher):
    outer = json.loads(raw.decode('utf-8'), object_pairs_hook=_object)
    if not isinstance(outer, dict) or not str(outer.get('err_msg', '')).endswith(':ok'):
        raise ArticleTransportError('微信未能读取公众号列表，请稍后重试')
    value = outer.get('jsapi_resp', {}).get('resp_json')
    value = json.loads(value, object_pairs_hook=_object) if isinstance(value, str) else value
    if (not isinstance(value, dict) or value.get('BaseResponse', {}).get('Ret') != 0 or
            value.get('AccountInfo', {}).get('UserName') != publisher or not isinstance(value.get('MsgList'), dict)):
        raise ArticleTransportError('公众号列表来源未通过核对，请重新读取')
    return value


def _cancelled(callback):
    try:
        return bool(callback())
    except Exception:
        return True


def _request(operation, pid, account, publisher, offset=None, url=None, *, cancelled=lambda: False,
             timeout_ms=10000, bridge_path=ARTICLE_DLL):
    _inputs(pid, account, publisher)
    if type(timeout_ms) is not int or not 100 <= timeout_ms <= 15000:
        raise ArticleTransportError('公众号读取超时参数无效', 400)
    if offset is not None and (not isinstance(offset, str) or not 1 <= len(offset.encode('utf-8')) <= MAX_OFFSET or '\0' in offset):
        raise ArticleTransportError('公众号分页信息已失效，请重新读取', 400)
    if operation == 2:
        from wxbot_framework.articles import normalize_article_url
        canonical = normalize_article_url(url)
        if not canonical or canonical != url:
            raise ArticleTransportError('文章链接无效', 400)
    if _cancelled(cancelled):
        raise ArticleTransportError('本次文章读取已取消')
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(pid, threading.Lock())
    if not lock.acquire(blocking=False):
        raise ArticleTransportError('公众号正在读取，请稍候')
    channel = config = mutex = None
    own_load = loaded = None
    dispatch_started = cleanup_ok = False
    try:
        mutex = _ProcessMutex(pid)
        if not mutex.acquire():
            raise ArticleTransportError('公众号正在读取，请稍候')
        before = _snapshot(pid, account)
        bridge = _local_path(bridge_path, suffix={'.dll'})
        exports = _exports(bridge)
        channel = _Channel(pid); channel.verify_client()
        existing = channel.api.modules(pid).get(bridge.name.lower())
        if existing is not None:
            raise ArticleTransportError('公众号原生通道仍在使用，请稍后重试')
        if _cancelled(cancelled) or _snapshot(pid, account).identity != before.identity:
            raise ArticleTransportError('微信登录会话已变化，请重新读取')
        load_result = channel.system(b'LoadLibraryW', channel.allocate((str(bridge) + '\0').encode('utf-16-le')))
        loaded = channel.api.modules(pid).get(bridge.name.lower())
        if loaded is None or Path(loaded[1]).resolve() != bridge:
            # Never invoke exports in a foreign same-named module. Release only a loader
            # reference whose returned low DWORD agrees with the observed module handle.
            if loaded is not None and load_result and (loaded[0] & 0xffffffff) == load_result:
                channel.system(b'FreeLibrary', loaded[0])
            loaded = None
            raise ArticleTransportError('公众号原生通道加载失败', 500)
        own_load = True
        reply = channel.allocate(bytes(MAX_RESPONSE + 1))
        config = ArticleConfig(size=c.sizeof(ArticleConfig), pid=pid, operation=operation, timeout_ms=timeout_ms,
                               # The server rejects JS-safe 53-bit values for BizSessionID.
                               # Positive 31-bit correlation IDs fit both signed/unsigned 32-bit readers.
                               frame=before.frame, request_id=uuid.uuid4().int % 0x7fffffff + 1,
                               response_address=reply, response_capacity=MAX_RESPONSE + 1,
                               offset_present=int(offset is not None), login_root=before.login_root,
                               login_context=before.login_context, account=account.encode('ascii'),
                               publisher=publisher.encode('ascii'), nonce=uuid.uuid4().hex.encode('ascii'),
                               offset=(offset or '').encode('utf-8'), url=(url or '').encode('ascii'))
        parameter = channel.allocate(bytes(config))
        if _cancelled(cancelled) or _snapshot(pid, account).identity != before.identity:
            raise ArticleTransportError('微信登录会话已变化，请重新读取')
        dispatch_started = True
        status = channel.call(loaded[0] + exports[_START], parameter)
        if status:
            if status == 1306:
                raise ArticleTransportError('当前微信或公众号浏览器版本尚未适配主动读取', 501)
            raise ArticleTransportError('公众号原生通道未就绪，请稍后重试')
        until = time.monotonic() + timeout_ms / 1000
        while True:
            if _cancelled(cancelled):
                raise ArticleTransportError('本次文章读取已取消')
            header = channel.read_memory(parameter, 32)
            if len(header) != 32:
                raise ArticleTransportError('公众号读取结果不完整')
            _, _, _, _, _, stage, native_status, size = struct.unpack('<8I', header)
            if stage == 4:
                if native_status or not 0 < size <= MAX_RESPONSE:
                    raise ArticleTransportError('公众号读取结果超过限制或已失效')
                raw = channel.read_memory(reply, size)
                if len(raw) != size:
                    raise ArticleTransportError('公众号读取结果不完整')
                break
            if time.monotonic() >= until:
                raise ArticleTransportError('公众号读取超时，请检查微信网络后重试', 504)
            time.sleep(.03)
        if _snapshot(pid, account).identity != before.identity:
            raise ArticleTransportError('微信登录会话已变化，请重新读取')
        result = _decode_history(raw, publisher) if operation == 1 else _decode_open(raw, config.nonce.decode('ascii'))
        return result
    except ArticleTransportError:
        raise
    except (Exception, KeyboardInterrupt):
        raise ArticleTransportError('公众号原生读取暂时失败，请稍后重试') from None
    finally:
        if channel is not None:
            if own_load and loaded is not None and not channel.pending:
                try:
                    cleanup_ok = channel.call(loaded[0] + exports[_STOP], parameter) == 0
                    if cleanup_ok:
                        channel.system(b'FreeLibrary', loaded[0])
                        cleanup_ok = bridge.name.lower() not in channel.api.modules(pid)
                except Exception:
                    cleanup_ok = False
                if not cleanup_ok:
                    channel.pending = True
            if config is not None:
                c.memset(c.addressof(config), 0, c.sizeof(config))
            try:
                channel.close()
            except Exception:
                pass
        if mutex is not None:
            mutex.close()
        lock.release()
        if own_load and not cleanup_ok:
            raise ArticleTransportError('公众号通道清理尚未完成，请稍后重试')


def request_page(pid, account, publisher_username, offset=None, *, cancelled=lambda: False, timeout_ms=10000):
    return _request(1, pid, account, publisher_username, offset=offset, cancelled=cancelled, timeout_ms=timeout_ms)


request_history = request_page


def open_article(pid, account, publisher_username, url, *, cancelled=lambda: False, timeout_ms=10000):
    return _request(2, pid, account, publisher_username, url=url, cancelled=cancelled, timeout_ms=timeout_ms)
