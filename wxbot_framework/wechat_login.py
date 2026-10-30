"""本机微信 4.2.0.11 登录二维码，按进程隔离且不保存登录凭据。"""
from __future__ import annotations

import base64
import ctypes as c
from ctypes import wintypes as w
import io
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid

from .panel_common import PanelError

_QR_PATTERN = re.compile(rb'(?<![A-Za-z0-9_./:-])http://weixin\.qq\.com/x/[A-Za-z0-9_=-]{10,128}(?![A-Za-z0-9_=-])')
_ID = re.compile(r'^[a-f0-9]{32}$')
_TTL = 300
_CHUNK = 1024 * 1024
_BUDGET = 512 * 1024 * 1024


def _qr_png(value):
    try:
        import qrcode
    except ImportError:
        raise PanelError('请先运行 python -m pip install -r requirements.txt 安装二维码依赖', 409) from None
    result = io.BytesIO()
    qr = qrcode.QRCode(box_size=8, border=4)
    qr.add_data(value)
    qr.make(fit=True)
    qr.make_image(fill_color='black', back_color='white').save(result, format='PNG')
    return base64.b64encode(result.getvalue()).decode('ascii')


def _find_qr(chunks, *, expected=None):
    """核对窗口当前二维码；无窗口证据时只接受唯一候选。"""
    values = set()
    for chunk in chunks:
        values.update(match.group() for match in _QR_PATTERN.finditer(chunk))
        if expected is None and len(values) > 1:
            raise PanelError('登录二维码存在多个候选，请在微信刷新二维码后重试', 409)
    if expected is not None:
        return expected if expected.encode('ascii') in values else None
    return next(iter(values)).decode('ascii') if values else None


def _window_qr(pid):
    """只读取目标进程未最小化的微信窗口；二维码和窗口内容仅在内存中处理。"""
    user = c.windll.user32
    set_context = user.SetThreadDpiAwarenessContext
    set_context.argtypes = [c.c_void_p]
    set_context.restype = c.c_void_p
    previous = set_context(c.c_void_p(-4))  # 每显示器 DPI 感知，避免窗口尺寸被缩放。
    try:
        return _window_qr_at_physical_size(pid)
    finally:
        if previous:
            set_context(previous)


def _window_qr_at_physical_size(pid):
    try:
        from PIL import Image
        import zxingcpp
    except ImportError:
        raise PanelError('请先运行 python -m pip install -r requirements.txt 安装二维码依赖', 409) from None

    user, gdi = c.windll.user32, c.windll.gdi32
    user.GetWindowThreadProcessId.argtypes = [w.HWND, c.POINTER(w.DWORD)]
    user.IsWindowVisible.argtypes = [w.HWND]
    user.IsIconic.argtypes = [w.HWND]
    user.GetWindowDC.argtypes = [w.HWND]
    user.GetWindowDC.restype = w.HDC
    user.ReleaseDC.argtypes = [w.HWND, w.HDC]
    user.PrintWindow.argtypes = [w.HWND, w.HDC, w.UINT]
    gdi.CreateCompatibleDC.argtypes = [w.HDC]
    gdi.CreateCompatibleDC.restype = w.HDC
    gdi.CreateCompatibleBitmap.argtypes = [w.HDC, c.c_int, c.c_int]
    gdi.CreateCompatibleBitmap.restype = w.HBITMAP
    gdi.SelectObject.argtypes = [w.HDC, w.HGDIOBJ]
    gdi.SelectObject.restype = w.HGDIOBJ
    gdi.DeleteObject.argtypes = [w.HGDIOBJ]
    gdi.DeleteDC.argtypes = [w.HDC]
    gdi.GetDIBits.argtypes = [w.HDC, w.HBITMAP, w.UINT, w.UINT, c.c_void_p, c.c_void_p, w.UINT]

    class Rect(c.Structure):
        _fields_ = [('left', w.LONG), ('top', w.LONG), ('right', w.LONG), ('bottom', w.LONG)]

    class BitmapHeader(c.Structure):
        _fields_ = [('size', w.DWORD), ('width', w.LONG), ('height', w.LONG), ('planes', w.WORD),
                    ('bits', w.WORD), ('compression', w.DWORD), ('image_size', w.DWORD),
                    ('xppm', w.LONG), ('yppm', w.LONG), ('used', w.DWORD), ('important', w.DWORD)]

    windows = []
    callback_type = c.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    @callback_type
    def collect(hwnd, _):
        owner = w.DWORD()
        user.GetWindowThreadProcessId(hwnd, c.byref(owner))
        if owner.value == pid and user.IsWindowVisible(hwnd) and not user.IsIconic(hwnd):
            windows.append(hwnd)
        return True
    user.EnumWindows(collect, 0)
    values = set()
    for hwnd in windows[:16]:
        rect = Rect()
        if not user.GetWindowRect(hwnd, c.byref(rect)):
            continue
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if not 150 <= width <= 2048 or not 150 <= height <= 2048:
            continue
        dc = user.GetWindowDC(hwnd)
        if not dc:
            continue
        memory = bitmap = old = None
        try:
            memory = gdi.CreateCompatibleDC(dc)
            bitmap = gdi.CreateCompatibleBitmap(dc, width, height)
            if not memory or not bitmap:
                continue
            old = gdi.SelectObject(memory, bitmap)
            if not old or not user.PrintWindow(hwnd, memory, 2):
                continue
            header = BitmapHeader(c.sizeof(BitmapHeader), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
            buffer = c.create_string_buffer(width * height * 4)
            if gdi.GetDIBits(memory, bitmap, 0, height, buffer, c.byref(header), 0) != height:
                continue
            owner = w.DWORD()
            user.GetWindowThreadProcessId(hwnd, c.byref(owner))
            if owner.value != pid:
                continue
            image = Image.frombytes('RGB', (width, height), buffer.raw, 'raw', 'BGRX')
            result = zxingcpp.read_barcode(image, formats=zxingcpp.BarcodeFormat.QRCode)
            if result and result.text.isascii() and _QR_PATTERN.fullmatch(result.text.encode('ascii')):
                values.add(result.text)
        finally:
            if old and memory:
                gdi.SelectObject(memory, old)
            if bitmap:
                gdi.DeleteObject(bitmap)
            if memory:
                gdi.DeleteDC(memory)
            user.ReleaseDC(hwnd, dc)
    if len(values) > 1:
        raise PanelError('微信窗口中有多个登录二维码，请检查登录窗口', 409)
    return next(iter(values)) if values else None


def _native(root):
    if sys.platform != 'win32' or c.sizeof(c.c_void_p) != 8:
        raise PanelError('微信登录需要 Windows 和 64 位 Python')
    path = str(root / 'native')
    if path not in sys.path:
        sys.path.insert(0, path)
    import run_selftest
    return run_selftest


def _verified_core(path):
    from native.moments_transport import _core_profile
    profile = _core_profile(Path(path))
    if profile is None or profile.version != '4.2.0.11':
        raise PanelError('登录二维码目前仅支持已验证的微信 4.2.0.11 构建', 409)


def _install_path(root):
    """从运行中的客户端或微信注册表定位，不接受请求指定的可执行文件。"""
    import winreg
    api = _native(root)
    candidates = set()
    for pid in api.wechat_candidates():
        try:
            core = api.modules(pid).get('weixin.dll')
            if core:
                core_path = Path(core[1]).resolve()
                _verified_core(core_path)
                candidates.add(core_path.parent.parent / 'Weixin.exe')
        except (OSError, ValueError, PanelError):
            continue
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for key in (r'Software\Tencent\Weixin', r'SOFTWARE\WOW6432Node\Tencent\Weixin'):
            try:
                with winreg.OpenKey(hive, key) as registry:
                    value, _ = winreg.QueryValueEx(registry, 'InstallPath')
                folder = Path(value).resolve()
                exe, dll = folder / 'Weixin.exe', folder / '4.2.0.11' / 'Weixin.dll'
                if exe.is_file() and dll.is_file():
                    _verified_core(dll)
                    candidates.add(exe)
            except (OSError, TypeError, ValueError, PanelError):
                continue
    candidates = {path.resolve() for path in candidates if path.is_file()}
    if len(candidates) != 1:
        raise PanelError('未找到唯一的微信 4.2.0.11 安装位置，请先自行打开微信', 409)
    return next(iter(candidates))


def _launch(root):
    exe = _install_path(root)
    try:
        return subprocess.Popen([str(exe)], cwd=str(exe.parent))
    except OSError:
        raise PanelError('启动微信失败，请检查安装和权限', 409) from None


class _WindowsTarget:
    def __init__(self, root, pid):
        from .moments_reader import _Memory
        api = _native(root)
        try:
            core = api.modules(pid).get('weixin.dll')
        except OSError:
            core = None
        if not core:
            raise PanelError('微信登录进程尚未就绪', 409)
        _verified_core(core[1])
        self.pid, self.core = pid, core[0]
        self.memory = _Memory(pid)
        try:
            path = c.create_unicode_buffer(32768)
            size = w.DWORD(len(path))
            if not api.K.QueryFullProcessImageNameW(self.memory.handle, 0, path, c.byref(size)):
                raise PanelError('无法核对微信进程路径', 409)
            if Path(path.value).name.lower() != 'weixin.exe':
                raise PanelError('所选进程不是微信客户端', 409)
        except Exception:
            self.memory.close()
            raise

    def alive(self):
        k = self.memory.kernel
        k.GetExitCodeProcess.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]
        k.GetExitCodeProcess.restype = w.BOOL
        value = w.DWORD()
        return bool(k.GetExitCodeProcess(self.memory.handle, c.byref(value)) and value.value == 259)

    def account(self):
        from .account import read_current_account
        return read_current_account(self.memory.read, self.core)

    def qr(self):
        from .moments_reader import _Region
        displayed = _window_qr(self.pid)
        if not displayed:
            return None
        memory, region = self.memory, _Region()
        address, total, deadline = 0, 0, time.monotonic() + 10
        def chunks():
            nonlocal address, total
            while address < 0x800000000000:
                if time.monotonic() > deadline:
                    raise PanelError('读取登录二维码超时，请重试', 503)
                if memory.kernel.VirtualQueryEx(memory.handle, address, c.byref(region), c.sizeof(region)) != c.sizeof(region):
                    break
                following = region.base + region.size
                if following <= address or following > 0x800000000000:
                    break
                if (region.state == 0x1000 and region.kind == 0x20000 and
                        region.protect & 0xff in (2, 4, 8, 0x20, 0x40, 0x80) and not region.protect & 0x100):
                    previous = b''
                    for offset in range(0, region.size, _CHUNK):
                        if time.monotonic() > deadline:
                            raise PanelError('读取登录二维码超时，请重试', 503)
                        length = min(_CHUNK, region.size - offset)
                        total += length
                        if total > _BUDGET:
                            raise PanelError('登录进程内存超过读取预算，请重试', 503)
                        try:
                            block = memory.read(region.base + offset, length)
                        except OSError:
                            previous = b''
                            continue
                        yield previous + block
                        previous = block[-256:]
                address = following
        value = _find_qr(chunks(), expected=displayed)
        if not self.alive():
            raise PanelError('微信登录进程已退出', 409)
        # 提取期间微信可能刷新二维码；只返回仍与窗口一致的结果。
        return value if value and _window_qr(self.pid) == value else None

    def close(self):
        self.memory.close()


class WechatLoginManager:
    """一次操作绑定一个微信 PID；关闭句柄不退出微信。"""
    def __init__(self, root, *, launch=_launch, target_factory=_WindowsTarget, clock=time.monotonic):
        self.root, self.launch, self.target_factory, self.clock = Path(root), launch, target_factory, clock
        self.attempts = {}
        self.lock = threading.RLock()

    def _prune(self):
        now = self.clock()
        for key, item in list(self.attempts.items()):
            if now >= item['expires']:
                if item['target'] is not None:
                    item['target'].close()
                del self.attempts[key]

    def start(self, data):
        if data:
            raise PanelError('启动微信不接受额外参数')
        with self.lock:
            self._prune()
            if len(self.attempts) >= 8:
                raise PanelError('登录尝试过多，请先取消已有尝试', 409)
            process = self.launch(self.root)
            return self._register(process.pid, None, process=process)

    def attach(self, data):
        pid = data.get('pid')
        if set(data) != {'pid'} or type(pid) is not int or not 0 < pid <= 0xffffffff:
            raise PanelError('请输入有效的微信进程 ID')
        with self.lock:
            self._prune()
            if len(self.attempts) >= 8:
                raise PanelError('登录尝试过多，请先取消已有尝试', 409)
            if any(item['pid'] == pid for item in self.attempts.values()):
                raise PanelError('此微信进程已有登录尝试', 409)
            target = self.target_factory(self.root, pid)
            try:
                if not target.alive() or target.account():
                    raise PanelError('请选择未登录的微信进程', 409)
            except Exception:
                target.close()
                raise
            return self._register(pid, target)

    def _register(self, pid, target, *, process=None):
        identifier = uuid.uuid4().hex
        self.attempts[identifier] = {'pid': pid, 'target': target, 'process': process,
                                     'expires': self.clock() + _TTL}
        return {'attempt_id': identifier, 'pid': pid, 'status': 'starting' if target is None else 'waiting'}

    def status(self, identifier):
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise PanelError('登录尝试 ID 无效')
        with self.lock:
            self._prune()
            item = self.attempts.get(identifier)
            if item is None:
                raise PanelError('登录尝试已过期或不存在', 404)
            if item['target'] is None:
                if item['process'] is not None and item['process'].poll() is not None:
                    return {'pid': item['pid'], 'status': 'exited'}
                try:
                    item['target'] = self.target_factory(self.root, item['pid'])
                except PanelError as error:
                    if str(error) == '微信登录进程尚未就绪':
                        return {'pid': item['pid'], 'status': 'starting'}
                    raise
            target = item['target']
            if not target.alive():
                return {'pid': item['pid'], 'status': 'exited'}
            account = target.account()
            if account:
                return {'pid': item['pid'], 'status': 'logged_in', 'wxid': account}
            value = target.qr()
            account = target.account()
            if account:
                return {'pid': item['pid'], 'status': 'logged_in', 'wxid': account}
            if not value:
                return {'pid': item['pid'], 'status': 'waiting'}
            return {'pid': item['pid'], 'status': 'qr_available', 'mime_type': 'image/png',
                    'qrcode_base64': _qr_png(value)}

    def cancel(self, data):
        identifier = data.get('attempt_id')
        if set(data) != {'attempt_id'} or not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise PanelError('登录尝试 ID 无效')
        with self.lock:
            self._prune()
            item = self.attempts.pop(identifier, None)
            if item is not None and item['target'] is not None:
                item['target'].close()
            return {'ok': True}

    def close(self):
        with self.lock:
            for item in self.attempts.values():
                if item['target'] is not None:
                    item['target'].close()
            self.attempts.clear()
