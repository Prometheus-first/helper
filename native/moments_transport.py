"""朋友圈专用的版本锁定 IPC；生产复用已持有的桥接，不隐式加载或重试。

只读能力检查不执行 DLL。发布只接受完整完成回执；调度成功不等于发布成功。
"""
from __future__ import annotations

import ctypes as c
import hashlib
import json
from pathlib import Path
import re
import threading
import time
import uuid

import pefile

ROOT = Path(__file__).resolve().parent.parent
TEXT_DLL = ROOT / 'native/build_text/text_bridge.dll'
_INSPECT = b'WxInspectMoments'
_PUBLISH = b'WxPublishMoments'
_CAPABILITY = b'WxMomentsCapabilities'
_WAIT = b'WxWaitForTasks'
_REMOVE = b'WxRemoveSelfTest'
_REQUIRED = {_INSPECT, _PUBLISH, _WAIT, _REMOVE}
_INTERMEDIATE = {'accepted'}
_PREFLIGHT_OUTCOMES = {'native_abi_unverified', 'prepare_failed', 'subscribe_failed',
                       'deadline_unavailable'}
_KNOWN_PREFLIGHT_CODES = {21, 50, 87, 170, 1306}
_UNKNOWN = '微信尚未确认发布结果，请到朋友圈核对，禁止自动重发'


class _BaseConfig(c.Structure):
    _fields_ = [('size', c.c_uint32), ('pid', c.c_uint32), ('manager', c.c_uint64),
                ('log_path', c.c_uint16 * 260), ('text', c.c_char * 1024)]


class MomentsConfig(c.Structure):
    _fields_ = [('base', _BaseConfig), ('self_wxid', c.c_char * 128),
                ('visibility', c.c_uint32), ('image_count', c.c_uint32),
                ('timeout_ms', c.c_uint32), ('reserved', c.c_uint32),
                ('image_paths', (c.c_uint16 * 1024) * 9), ('text', c.c_char * 12001)]


assert c.sizeof(_BaseConfig) == 1560 and c.sizeof(MomentsConfig) == 32144
assert MomentsConfig.self_wxid.offset == 1560 and MomentsConfig.visibility.offset == 1688
assert MomentsConfig.image_paths.offset == 1704 and MomentsConfig.text.offset == 20136


def _native():
    import run_selftest
    return run_selftest


def _reparse(path):
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)


def _local_path(value, *, suffix=None):
    path = Path(value)
    if (not path.is_absolute() or path.drive.startswith('\\\\') or '\0' in str(path) or
            any(_reparse(part) for part in (path, *path.parents) if part.exists()) or
            path.resolve() != path or not path.is_file() or (suffix and path.suffix.lower() not in suffix)):
        raise ValueError('朋友圈仅接受已核对的绝对本地文件')
    return path


def _exports(bridge):
    pe = pefile.PE(str(bridge), fast_load=True)
    try:
        if pe.FILE_HEADER.Machine != 0x8664:
            raise ValueError('朋友圈桥接必须为 x64 DLL')
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_EXPORT']])
        symbols = getattr(getattr(pe, 'DIRECTORY_ENTRY_EXPORT', None), 'symbols', ())
        exports = {entry.name: entry.address for entry in symbols if entry.name and not entry.forwarder}
        flags = None
        address = exports.get(_CAPABILITY)
        if address is not None:
            section = pe.get_section_by_rva(address)
            # 能力位必须是显式导出的只读数据，不执行探测函数或依靠导出名称猜门禁。
            if section is not None and section.Characteristics & 0x40000000 and not section.Characteristics & 0xa0000000:
                raw = pe.get_data(address, 4)
                if len(raw) == 4:
                    flags = int.from_bytes(raw, 'little')
            elif section is not None and section.Characteristics & 0x20000000 and not section.Characteristics & 0x80000000:
                # root 约定的纯常量 DWORD 函数：只读机器码，不调用本机/目标 DLL。
                raw = pe.get_data(address, 6)
                if len(raw) == 6 and raw[0] == 0xb8 and raw[5] == 0xc3:
                    flags = int.from_bytes(raw[1:5], 'little')
        return exports, flags
    finally:
        pe.close()


def _core_profile(path):
    from wxbot_framework.native_profiles import profile_for_fingerprint
    path = _local_path(path, suffix={'.dll'})
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    profile = profile_for_fingerprint(path.stat().st_size, digest.hexdigest())
    return profile if profile is not None and profile.version == '4.1.15.56' else None


def moments_capabilities(pid=None, bridge_path=TEXT_DLL, *, core_path=None):
    """只读本地 PE/核心指纹；WxMomentsCapabilities 为只读 uint32 位标志。

    bit0=inspect ABI 可用，bit1=发布接口开启，bit2=研究用事件探针开启。
    导出缺失、格式未知或 submit 门禁关闭时，supported 必须为 False。
    """
    result = {'supported': False, 'inspect_supported': False, 'native_submit_enabled': False,
              'event_probe_enabled': False, 'reason': '当前桥接尚未开放朋友圈发布接口'}
    try:
        bridge = _local_path(bridge_path, suffix={'.dll'})
        exports, flags = _exports(bridge)
        if not _REQUIRED <= exports.keys():
            return result
        if core_path is None and pid is not None:
            if type(pid) is not int or not 0 < pid <= 0xffffffff:
                return result
            core = _native().modules(pid).get('weixin.dll')
            core_path = core[1] if core else None
        if core_path is None or _core_profile(core_path) is None:
            result['reason'] = '当前微信核心指纹尚无朋友圈原生适配'
            return result
        if flags is None or flags & ~7 or not flags & 1:
            result['reason'] = '当前桥接不能只读确认朋友圈提交门禁，发布保持关闭'
            return result
        result['native_submit_enabled'] = bool(flags & 2)
        result['event_probe_enabled'] = bool(flags & 4)
        result['inspect_supported'] = not bool(flags & 4)
        result['supported'] = bool(flags & 2)
        result['reason'] = '' if result['supported'] else '当前桥接尚未开启朋友圈发布，请更新桥接 DLL'
    except Exception:
        result['reason'] = '当前连接暂时无法确认朋友圈原生能力'
    return result


def _wide(target, value):
    raw = value.encode('utf-16-le')
    if len(raw) // 2 >= len(target):
        raise ValueError('朋友圈本地路径超过原生接口长度')
    c.memmove(c.addressof(target), raw, len(raw))


def _validate(pid, account, content, images, visibility, timeout_ms):
    from wxbot_framework.account import validate_account
    account = validate_account(account)
    if type(pid) is not int or not 0 < pid <= 0xffffffff:
        raise ValueError('朋友圈目标进程无效')
    if type(timeout_ms) is not int or not 10000 <= timeout_ms <= 300000:
        raise ValueError('朋友圈原生超时应为 10000–300000 毫秒')
    if not isinstance(content, str) or '\0' in content:
        raise ValueError('朋友圈正文无效')
    try:
        encoded = content.encode('utf-8')
    except UnicodeEncodeError:
        raise ValueError('朋友圈正文含无效 Unicode 字符') from None
    if len(encoded) > 4000:
        raise ValueError('朋友圈正文超过本框架 4000 UTF-8 字节限制')
    if visibility not in ('public', 'private'):
        raise ValueError('朋友圈仅支持公开或仅自己可见')
    if not isinstance(images, (tuple, list)) or len(images) > 9:
        raise ValueError('朋友圈最多支持九张临时图片')
    paths, total = [], 0
    for value in images:
        try:
            path = _local_path(value, suffix={'.png', '.jpg', '.jpeg'})
            size = path.stat().st_size
            if not 0 < size <= 5 * 1024 * 1024:
                raise ValueError()
            total += size
            if total > 20 * 1024 * 1024 or len(str(path).encode('utf-16-le')) // 2 >= 1024 or path in paths:
                raise ValueError()
            with path.open('rb') as source:
                signature = source.read(8)
            if not (signature.startswith(b'\xff\xd8\xff') or signature == b'\x89PNG\r\n\x1a\n'):
                raise ValueError()
        except (OSError, ValueError, TypeError):
            raise ValueError('朋友圈图片必须为已核对的本地 PNG/JPEG 临时文件') from None
        paths.append(path)
    if not content.strip() and not paths:
        raise ValueError('朋友圈需要正文或图片')
    return account, encoded, paths


class _Channel:
    def __init__(self, pid):
        self.api = _native()
        self.pid = pid
        self.process = self.api.check(self.api.K.OpenProcess(0x43a, False, pid), '打开朋友圈原生通道')
        self.allocations = []
        self.pending = False

    def read_memory(self, address, size):
        buffer, count = c.create_string_buffer(size), c.c_size_t()
        self.api.check(self.api.K.ReadProcessMemory(self.process, address, buffer, size, c.byref(count)), '只读核对朋友圈账号')
        if count.value != size:
            raise ValueError('朋友圈账号读取不完整')
        return buffer.raw

    def verify_client(self):
        buffer, count = c.create_unicode_buffer(32768), c.c_uint32(32768)
        self.api.check(self.api.K.QueryFullProcessImageNameW(self.process, 0, buffer, c.byref(count)), '核对朋友圈目标进程')
        if Path(buffer.value).name.lower() != 'weixin.exe':
            raise ValueError('朋友圈目标不是微信进程')

    def allocate(self, raw):
        pointer = self.api.check(self.api.K.VirtualAllocEx(self.process, None, len(raw), 0x3000, 4), '分配朋友圈参数')
        self.allocations.append((pointer, len(raw)))
        buffer, count = c.create_string_buffer(raw), c.c_size_t()
        self.api.check(self.api.K.WriteProcessMemory(self.process, pointer, buffer, len(raw), c.byref(count)), '写入朋友圈参数')
        if count.value != len(raw):
            raise ValueError('朋友圈参数写入不完整')
        return pointer

    def call(self, address, parameter=None):
        try:
            return self.api.remote_call(self.process, address, parameter, timeout=30000)
        except (TimeoutError, KeyboardInterrupt):
            self.pending = True
            raise

    def system(self, name, parameter):
        return self.call(self.api.remote_system_function(self.pid, name), parameter)

    def close(self):
        try:
            if not self.pending:
                for pointer, size in self.allocations:
                    zeros, count = c.create_string_buffer(size), c.c_size_t()
                    self.api.K.WriteProcessMemory(self.process, pointer, zeros, size, c.byref(count))
                    self.api.K.VirtualFreeEx(self.process, pointer, 0, 0x8000)
        finally:
            self.api.K.CloseHandle(self.process)


def _report_path(workspace):
    workspace = Path(workspace or ROOT).resolve()
    if not workspace.is_dir():
        raise ValueError('朋友圈工作区尚未建立')
    directory = workspace
    for part in ('.runtime', 'moments-reports'):
        directory = directory / part
        directory.mkdir(exist_ok=True)
        if _reparse(directory) or directory.resolve() != directory:
            raise ValueError('朋友圈报告目录不在当前工作区')
    path = directory / (uuid.uuid4().hex + '.json')
    if len(str(path).encode('utf-16-le')) // 2 >= 260:
        raise ValueError('朋友圈报告路径超过原生接口长度')
    return path


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('朋友圈报告含重复字段')
        result[key] = value
    return result


def _read_report(path):
    if _reparse(path) or path.stat().st_size > 65536:
        raise ValueError('朋友圈报告无效')
    with path.open('rb') as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ValueError('朋友圈报告无效')
    report = json.loads(raw.decode('utf-8'), object_pairs_hook=_json_object)
    if not isinstance(report, dict):
        raise ValueError('朋友圈报告无效')
    return report


def _delete_report(path):
    try:
        if (path is not None and re.fullmatch(r'[a-f0-9]{32}\.json', path.name) and
                path.parent.name == 'moments-reports' and path.parent.resolve() == path.parent and
                not _reparse(path.parent)):
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _watch_late_report(path, pid):
    """仅监看本次 UUID 报告和进程退出；不调度、重发或卸载任何原生操作。"""
    def cleanup():
        api, handle = None, None
        try:
            api = _native()
            handle = api.K.OpenProcess(0x100000, False, pid)
            while handle and api.K.WaitForSingleObject(handle, 0) == 258:
                _delete_report(path)
                time.sleep(1)
            _delete_report(path)
        except Exception:
            _delete_report(path)
        finally:
            if api is not None and handle:
                api.K.CloseHandle(handle)
    threading.Thread(target=cleanup, name='MomentsReportCleanup', daemon=True).start()


def _inspect_diagnostics(result, report, execute):
    if execute or not isinstance(report, dict):
        return result
    diagnostics = {key: report[key] for key in ('stage', 'status', 'exception', 'cdn_ready_inputs',
                                               'native_path_matches', 'local_thumb_ready')
                   if type(report.get(key)) is int and 0 <= report[key] <= 0xffffffff}
    if isinstance(report.get('outcome'), str) and re.fullmatch(r'[a-z_]{1,64}', report['outcome']):
        diagnostics['outcome'] = report['outcome']
    if diagnostics:
        result['diagnostics'] = diagnostics
    return result


def _emit_diagnostics(sink, report, *, cleanup_ok, dispatch_code):
    if sink is None:
        return
    safe = {'cleanup_confirmed': bool(cleanup_ok)}
    if type(dispatch_code) is int and 0 <= dispatch_code <= 0xffffffff:
        safe['dispatch_code'] = dispatch_code
    if isinstance(report, dict):
        safe.update(_inspect_diagnostics({}, report, False).get('diagnostics', {}))
        for key in ('ret_code', 'image_count', 'media_ready', 'text_bytes'):
            if type(report.get(key)) is int and -0x80000000 <= report[key] <= 0xffffffff:
                safe[key] = report[key]
        for key in ('accepted', 'completion_success', 'server_result_known', 'account_matches', 'unload_blocked'):
            if type(report.get(key)) is bool:
                safe[key] = report[key]
    try:
        sink(safe)
    except Exception:
        pass  # 诊断接收方不能改变原生回收或发送结果。


def _result(report, *, execute, account, content_bytes, image_count, visibility, dispatch_code, cleanup_ok):
    if not cleanup_ok:
        return _inspect_diagnostics({'status': 'unknown', 'reason': _UNKNOWN, 'unload_blocked': True}, report, execute)
    valid = (isinstance(report, dict) and report.get('operation') == ('moments_publish' if execute else 'moments_inspect') and
             report.get('account') == account and report.get('visibility') == visibility and
             type(report.get('image_count')) is int and report['image_count'] == image_count and
             type(report.get('text_bytes')) is int and report['text_bytes'] == content_bytes)
    if valid:
        sns_id = report.get('sns_id')
        clean_status = (type(report.get('exception')) is int and report['exception'] == 0 and
                        type(report.get('status')) is int and report['status'] == 0)
        media_ready = type(report.get('media_ready')) is int and report['media_ready'] == image_count
        if (execute and dispatch_code == 0 and cleanup_ok and report.get('outcome') == 'completed' and
                report.get('completion_success') is True and report.get('accepted') is True and
                report.get('server_result_known') is True and type(report.get('ret_code')) is int and report['ret_code'] == 0 and
                report.get('account_matches') is True and clean_status and media_ready and report.get('unload_blocked') is False and
                isinstance(sns_id, str) and re.fullmatch(r'[1-9][0-9]{0,19}', sns_id) and int(sns_id) <= 0xffffffffffffffff):
            return {'status': 'published', 'sns_id': sns_id, 'unload_blocked': False}
        if (not execute and dispatch_code == 0 and cleanup_ok and report.get('outcome') == 'inspect_ready' and
                report.get('inspected') is True and report.get('account_matches') is True and
                report.get('accepted') is False and clean_status and media_ready and
                report.get('unload_blocked') is False):
            result = {'status': 'inspected', 'inspected': True, 'image_count': image_count, 'media_ready': image_count,
                      'text_bytes': content_bytes, 'visibility': visibility, 'unload_blocked': False,
                      'cleanup_confirmed': True}
            for key in ('stage', 'thread_id', 'cdn_ready_inputs', 'native_path_matches', 'local_thumb_ready'):
                if type(report.get(key)) is int and 0 <= report[key] <= 0xffffffff:
                    result[key] = report[key]
            for key in ('native_submit_enabled', 'event_probe_enabled'):
                if type(report.get(key)) is bool:
                    result[key] = report[key]
            return result
        if (cleanup_ok and report.get('outcome') in _PREFLIGHT_OUTCOMES and report.get('accepted') is False and
                report.get('unload_blocked') is False):
            return {'status': 'failed', 'phase': 'preflight', 'reason': '朋友圈原生提交前检查未通过', 'unload_blocked': False}
    if report is None and dispatch_code in _KNOWN_PREFLIGHT_CODES:
        return {'status': 'failed', 'phase': 'preflight', 'reason': '朋友圈原生接口未接受请求', 'unload_blocked': not cleanup_ok}
    return _inspect_diagnostics({'status': 'unknown', 'reason': _UNKNOWN, 'unload_blocked': not cleanup_ok}, report, execute)


def _request(execute, pid, account, content, images, visibility, bridge_path, *, workspace, keep_loaded, timeout_ms,
             allow_event_probe=False, diagnostic_sink=None):
    account, encoded, paths = _validate(pid, account, content, images, visibility, timeout_ms)
    if type(keep_loaded) is not bool:
        raise ValueError('keep_loaded 必须为布尔值')
    if type(allow_event_probe) is not bool or (execute and allow_event_probe):
        raise ValueError('事件研究探针只能由 Inspect 显式开启')
    if diagnostic_sink is not None and not callable(diagnostic_sink):
        raise ValueError('诊断接收方必须可调用')
    try:
        bridge = _local_path(bridge_path, suffix={'.dll'})
        exports, flags = _exports(bridge)
    except Exception:
        return {'status': 'failed', 'phase': 'preflight', 'reason': '朋友圈桥接文件尚未就绪', 'unload_blocked': False}
    if not _REQUIRED <= exports.keys():
        return {'status': 'failed', 'phase': 'preflight', 'reason': '当前桥接缺少朋友圈接口', 'unload_blocked': False}
    if execute and (flags is None or flags & ~7 or flags & 3 != 3):
        return {'status': 'failed', 'phase': 'preflight', 'reason': '当前桥接未开启朋友圈发布', 'unload_blocked': False}
    if not execute and (flags is None or flags & ~7 or not flags & 1 or
                        (flags & 4 and not allow_event_probe) or (allow_event_probe and flags & 2)):
        return {'status': 'failed', 'phase': 'preflight', 'reason': '无法确认朋友圈 Inspect 未开启事件研究探针', 'unload_blocked': False}
    channel, config, report_path, loaded = None, None, None, None
    own_load, dispatched, cleanup_ok, dispatch_code, report = False, False, True, None, None
    response = None
    try:
        channel = _Channel(pid)
        channel.verify_client()
        loaded_modules = channel.api.modules(pid)
        core = loaded_modules.get('weixin.dll')
        if not core or _core_profile(core[1]) is None:
            raise ValueError('朋友圈核心指纹尚不支持')
        from wxbot_framework.account import read_current_account
        if read_current_account(channel.read_memory, core[0]) != account:
            raise ValueError('朋友圈登录账号与当前机器人不一致')
        existing = loaded_modules.get(bridge.name.lower())
        if existing is not None and Path(existing[1]).resolve() != bridge:
            raise ValueError('同名朋友圈桥接来自其他目录')
        if existing is not None and not keep_loaded:
            raise ValueError('朋友圈探针不能接管或卸载已运行的桥接')
        if existing is None:
            if keep_loaded:
                raise ValueError('当前机器人未持有朋友圈桥接')
            channel.system(b'LoadLibraryW', channel.allocate((str(bridge) + '\0').encode('utf-16-le')))
            loaded = channel.api.modules(pid).get(bridge.name.lower())
            if not loaded or Path(loaded[1]).resolve() != bridge:
                raise ValueError('朋友圈探针桥接未正确加载')
            own_load = True
        else:
            loaded = existing
        report_path = _report_path(workspace)
        config = MomentsConfig(self_wxid=account.encode('ascii'), visibility=1 if visibility == 'public' else 2,
                               image_count=len(paths), timeout_ms=timeout_ms, text=encoded)
        config.base.size, config.base.pid = c.sizeof(config), pid
        _wide(config.base.log_path, str(report_path))
        for index, path in enumerate(paths):
            _wide(config.image_paths[index], str(path))
        parameter = channel.allocate(bytes(config))
        # 在进入导出之前冻结账号核对；导出内部还会在任务和回调分别核对。
        if read_current_account(channel.read_memory, core[0]) != account:
            raise ValueError('朋友圈登录账号已变化')
        dispatched, cleanup_ok = True, False
        dispatch_code = channel.call(loaded[0] + exports[_PUBLISH if execute else _INSPECT], parameter)
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            try:
                candidate = _read_report(report_path)
                if candidate.get('outcome') not in _INTERMEDIATE:
                    report = candidate
                    break
            except (OSError, ValueError, UnicodeError):
                pass
            if dispatch_code != 0 or time.monotonic() >= deadline:
                break
            time.sleep(.05)
        cleanup_name = _WAIT if keep_loaded else _REMOVE
        cleanup_ok = channel.call(loaded[0] + exports[cleanup_name]) == 0
        if cleanup_ok and report is None:
            try:
                candidate = _read_report(report_path)
                if candidate.get('outcome') not in _INTERMEDIATE:
                    report = candidate
            except (OSError, ValueError, UnicodeError):
                pass
        if isinstance(report, dict) and report.get('unload_blocked') is True:
            cleanup_ok = False
        if cleanup_ok and own_load:
            channel.system(b'FreeLibrary', loaded[0])
            cleanup_ok = bridge.name.lower() not in channel.api.modules(pid)
            own_load = not cleanup_ok
        if cleanup_ok and read_current_account(channel.read_memory, core[0]) != account:
            response = {'status': 'unknown', 'reason': _UNKNOWN, 'unload_blocked': False}
        else:
            response = _result(report, execute=execute, account=account, content_bytes=len(encoded), image_count=len(paths),
                               visibility=visibility, dispatch_code=dispatch_code, cleanup_ok=cleanup_ok)
        return response
    except (Exception, KeyboardInterrupt):
        if channel is not None and (channel.pending or dispatched):
            response = {'status': 'unknown', 'reason': _UNKNOWN, 'unload_blocked': True}
        else:
            response = {'status': 'failed', 'phase': 'preflight', 'reason': '朋友圈原生提交前检查未通过', 'unload_blocked': False}
        return response
    finally:
        _emit_diagnostics(diagnostic_sink, report, cleanup_ok=cleanup_ok, dispatch_code=dispatch_code)
        # 初始化中途失败也只能经原生卸载门禁回收自己加载的探针副本。
        if channel is not None and own_load and loaded is not None and not dispatched and not channel.pending:
            try:
                cleanup_ok = channel.call(loaded[0] + exports[_REMOVE]) == 0
                if cleanup_ok:
                    channel.system(b'FreeLibrary', loaded[0])
            except Exception:
                cleanup_ok = False
            if not cleanup_ok and response is not None:
                response.clear()
                response.update(status='unknown', reason=_UNKNOWN, unload_blocked=True)
        if report_path is not None:
            _delete_report(report_path)
            if dispatched and not cleanup_ok:
                try:
                    _watch_late_report(report_path, pid)
                except Exception:
                    pass
        if config is not None:
            c.memset(c.addressof(config), 0, c.sizeof(config))
        if channel is not None:
            try:
                channel.close()
            except Exception:
                # 参数回收失败不回显 Win32 异常中的路径；不会强行卸载原生模块。
                pass


def inspect_moments(pid, account, content='', images=(), visibility='private', bridge_path=TEXT_DLL,
                    *, workspace=None, keep_loaded=True, timeout_ms=120000, allow_event_probe=False):
    return _request(False, pid, account, content, images, visibility, bridge_path,
                    workspace=workspace, keep_loaded=keep_loaded, timeout_ms=timeout_ms,
                    allow_event_probe=allow_event_probe)


def publish_moments(pid, account, content='', images=(), visibility='private', bridge_path=TEXT_DLL,
                    *, workspace=None, keep_loaded=True, timeout_ms=120000, diagnostic_sink=None):
    return _request(True, pid, account, content, images, visibility, bridge_path,
                    workspace=workspace, keep_loaded=keep_loaded, timeout_ms=timeout_ms,
                    diagnostic_sink=diagnostic_sink)
