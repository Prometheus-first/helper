"""Private Xvfb display and in-memory QR capture for headless Linux WeChat."""
import base64
import io
import os
from pathlib import Path
import re
import secrets
import select
import shutil
import subprocess
import sys
import tempfile
import threading

from .panel_common import PanelError


class HeadlessDisplay:
    def __init__(self, directory):
        for program in ('Xvfb', 'xauth', 'ffmpeg'):
            if not shutil.which(program):
                raise PanelError('无桌面模式需要安装 Xvfb、xauth 和 ffmpeg', 409)
        self._dir = tempfile.TemporaryDirectory(prefix='display-', dir=directory)
        self.authority = Path(self._dir.name) / 'Xauthority'
        self.authority.touch(mode=0o600)
        self.process = None
        self.lock = threading.RLock()
        self._quick_login_switched = False
        read_fd, write_fd = os.pipe()
        try:
            self.process = subprocess.Popen(['Xvfb', '-displayfd', str(write_fd), '-screen', '0',
                '1024x768x24', '-nolisten', 'tcp', '-auth', str(self.authority), '-noreset'],
                pass_fds=(write_fd,), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True)
            os.close(write_fd)
            write_fd = -1
            ready, _, _ = select.select([read_fd], [], [], 5)
            if not ready:
                raise RuntimeError('虚拟显示启动超时')
            number = os.read(read_fd, 32).strip()
            if not number.isdigit() or self.process.poll() is not None:
                raise RuntimeError('虚拟显示未就绪')
            self.display = ':' + number.decode('ascii')
            cookie = secrets.token_hex(16)
            result = subprocess.run(['xauth', '-f', str(self.authority), '-q'],
                input=f'add {self.display} MIT-MAGIC-COOKIE-1 {cookie}\n', text=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
            if result.returncode:
                raise RuntimeError('虚拟显示认证初始化失败')
        except BaseException:
            self.close()
            raise
        finally:
            os.close(read_fd)
            if write_fd >= 0:
                os.close(write_fd)

    def environment(self):
        env = {'DISPLAY': self.display, 'XAUTHORITY': str(self.authority), 'QT_QPA_PLATFORM': 'xcb'}
        return env

    @staticmethod
    def _quick_login_target(image, windows):
        """识别固定尺寸的快捷登录窗口；不在未知页面上模拟点击。"""
        for x, y, width, height in windows:
            if (width, height) != (292, 396) or x < 0 or y < 0 or x + width > image.width or y + height > image.height:
                continue
            def colors(box, predicate):
                crop = image.crop(box).convert("RGB")
                pixels = crop.get_flattened_data() if hasattr(crop, "get_flattened_data") else crop.getdata()
                return sum(1 for rgb in pixels if predicate(*rgb))
            green = colors((x + 58, y + 289, x + 233, y + 326),
                           lambda r,g,b: g > 120 and g > r * 1.5 and g > b * 1.12 and r < 90)
            blue = lambda r,g,b: b > r + 12 and b > g + 3 and b < 220
            left = colors((x + 5, y + 345, x + 135, y + 379), blue)
            right = colors((x + 150, y + 345, x + 290, y + 379), blue)
            if green > 4500 and left > 100 and right > 100:
                return x + 60, y + 360
        return None

    def _switch_quick_login(self, image, env):
        # 每个私有显示只处理一次；任何识别不确定的情况都不点击。
        with self.lock:
            if self._quick_login_switched or not shutil.which("xwininfo"):
                return False
            try:
                result = subprocess.run(["xwininfo", "-root", "-tree"], env=env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3)
                windows = []
                for line in result.stdout.splitlines():
                    match = re.search(r'"Weixin": \("wechat" "wechat"\)\s+(\d+)x(\d+)\+(-?\d+)\+(-?\d+)', line)
                    if match:
                        w,h,x,y = map(int, match.groups())
                        windows.append((x,y,w,h))
                point = self._quick_login_target(image, windows) if result.returncode == 0 and len(windows) == 1 else None
                if point is None:
                    return False
                script = """import ctypes,sys
x=ctypes.CDLL('libX11.so.6');t=ctypes.CDLL('libXtst.so.6')
x.XOpenDisplay.argtypes=[ctypes.c_char_p];x.XOpenDisplay.restype=ctypes.c_void_p
d=x.XOpenDisplay(None)
if not d:sys.exit(1)
t.XTestFakeMotionEvent.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_ulong]
t.XTestFakeButtonEvent.argtypes=[ctypes.c_void_p,ctypes.c_uint,ctypes.c_int,ctypes.c_ulong]
x.XSync.argtypes=[ctypes.c_void_p,ctypes.c_int];x.XCloseDisplay.argtypes=[ctypes.c_void_p]
ok=t.XTestFakeMotionEvent(d,-1,int(sys.argv[1]),int(sys.argv[2]),0) and t.XTestFakeButtonEvent(d,1,1,0) and t.XTestFakeButtonEvent(d,1,0,0)
x.XSync(d,0);x.XCloseDisplay(d);sys.exit(0 if ok else 1)
"""
                result = subprocess.run([sys.executable, "-c", script, str(point[0]), str(point[1])], env=env,
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
                self._quick_login_switched = result.returncode == 0
                return self._quick_login_switched
            except (OSError, subprocess.TimeoutExpired, ValueError):
                return False

    def capture_qr(self):
        """Decode a QR on this private display and return a fresh PNG Base64 in memory."""
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                raise RuntimeError('虚拟显示已关闭')
            env = dict(os.environ, **self.environment())
            env.pop('WAYLAND_DISPLAY', None)
            cmd = ['ffmpeg', '-nostdin', '-loglevel', 'error', '-f', 'x11grab', '-video_size',
                   '1024x768', '-i', self.display, '-frames:v', '1', '-f', 'image2pipe',
                   '-vcodec', 'png', 'pipe:1']
            try:
                result = subprocess.run(cmd, env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=8)
            except (OSError, subprocess.TimeoutExpired):
                return None
        if result.returncode or not result.stdout or len(result.stdout) > 8 * 1024 * 1024:
            return None
        try:
            import zxingcpp
            from PIL import Image
            import qrcode
            image = Image.open(io.BytesIO(result.stdout))
            candidates = zxingcpp.read_barcodes(image, formats=zxingcpp.BarcodeFormat.QRCode)
            # A login code must use the expected WeChat login host and be unique.
            values = {item.text for item in candidates if re.fullmatch(
                r'https?://weixin\.qq\.com/x/[A-Za-z0-9_=-]{10,128}', item.text or '')}
            if len(values) != 1:
                if not candidates:
                    self._switch_quick_login(image, env)
                return None
            output = io.BytesIO()
            qr = qrcode.QRCode(box_size=8, border=4)
            qr.add_data(next(iter(values)))
            qr.make(fit=True)
            qr.make_image(fill_color='black', back_color='white').save(output, format='PNG')
            return base64.b64encode(output.getvalue()).decode('ascii')
        except (OSError, ValueError, ImportError):
            return None

    def close(self):
        with self.lock:
            if self.process is not None:
                process, self.process = self.process, None
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=3)
            self._dir.cleanup()
