"""Bounded I420 source for the experimental Windows call-video bridge."""
from __future__ import annotations

import ctypes as c
import mmap
import os
from pathlib import Path
import subprocess
import shutil
import threading
import time
import uuid

from run_selftest import TEXT_DLL
from video_control import _call_video_control


class MapHeader(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in ('magic', 'abi', 'bytes', 'width', 'height',
        'format', 'slot_bytes', 'slot_count', 'owner_pid', 'reserved')] + [
        ('generation', c.c_uint64 * 2), ('heartbeat', c.c_uint64), ('closed', c.c_uint32),
        ('padding', c.c_ubyte * 60)]


class SourceControl(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in ('size', 'abi', 'operation', 'owner_pid')] + [
        ('mapping_name', c.c_wchar * 96), ('generation', c.c_uint64 * 2)]
    _fields_ += [('account', c.c_char * 128)]
    _fields_ += [('quality', c.c_uint32), ('reserved', c.c_uint32)]


class PublishControl(c.Structure):
    _fields_ = [('size', c.c_uint32), ('abi', c.c_uint32), ('mapping', c.c_void_p),
        ('mapping_bytes', c.c_uint64), ('pixels', c.c_void_p),
        ('pixel_bytes', c.c_uint64), ('sequence', c.c_uint64)]


assert c.sizeof(MapHeader) == 128 and c.sizeof(SourceControl) == 360
assert c.sizeof(PublishControl) == 48


class VideoFrameWriter:
    """A single producer. Frames must be packed I420 at the configured dimensions."""
    def __init__(self, width, height):
        if (type(width) is not int or type(height) is not int or
                not 16 <= width <= 1920 or not 16 <= height <= 1920 or
                width % 2 or height % 2 or width * height > 2073600):
            raise ValueError('视频尺寸须为偶数，边长 16–1920，且不超过 2073600 像素')
        self.width, self.height = width, height
        self.payload_bytes = width * height * 3 // 2
        self.slot_bytes = 64 + ((self.payload_bytes + 63) & ~63)
        self.mapping_bytes = 128 + 3 * self.slot_bytes
        self.name = 'Local\\wxbot-video-' + uuid.uuid4().hex
        self.generation = (c.c_uint64 * 2).from_buffer_copy(uuid.uuid4().bytes)
        self.mapping = mmap.mmap(-1, self.mapping_bytes, tagname=self.name)
        self.address = c.addressof(c.c_ubyte.from_buffer(self.mapping))
        self.header = MapHeader.from_buffer(self.mapping)
        self.header.magic, self.header.abi = 0x31564257, 1
        self.header.bytes, self.header.width, self.header.height = self.mapping_bytes, width, height
        self.header.format, self.header.slot_bytes, self.header.slot_count = 1, self.slot_bytes, 3
        self.header.owner_pid = os.getpid()
        self.header.generation[:] = self.generation[:]
        self._lock = threading.Lock()
        self._sequence = 0
        self._bridge = c.WinDLL(str(TEXT_DLL))
        self._publish = self._bridge.WxPublishVideoFrame
        self._publish.argtypes = [c.c_void_p]
        self._publish.restype = c.c_uint32

    def control(self, operation, account='', quality='native'):
        if quality not in ('native', 'high'):
            raise ValueError('视频清晰度须为 native 或 high')
        control = SourceControl(size=c.sizeof(SourceControl), abi=1, operation=operation,
                                owner_pid=os.getpid(), mapping_name=self.name)
        control.account = account.encode('ascii')
        control.quality = int(quality == 'high')
        control.generation[:] = self.generation[:]
        return control

    def capture_descriptor(self):
        return {'name': self.name, 'width': self.width, 'height': self.height,
                'bytes': self.mapping_bytes, 'owner_pid': os.getpid(),
                'generation': list(self.generation)}

    def submit(self, pixels):
        if not isinstance(pixels, (bytes, bytearray)) or len(pixels) != self.payload_bytes:
            raise ValueError('需要与配置尺寸一致的完整 I420 帧')
        with self._lock:
            if self.mapping is None or self.header.closed:
                raise RuntimeError('视频源已关闭')
            storage = (c.c_ubyte * len(pixels)).from_buffer_copy(pixels)
            self._sequence += 1
            control = PublishControl(size=c.sizeof(PublishControl), abi=1, mapping=self.address,
                mapping_bytes=self.mapping_bytes, pixels=c.addressof(storage),
                pixel_bytes=len(pixels), sequence=self._sequence)
            code = self._publish(c.byref(control))
            if code == 170:  # All slots are currently owned; drop, never queue.
                return False
            if code:
                raise RuntimeError(f'视频帧发布失败：{code}')
            return True

    def disarm(self):
        with self._lock:
            if self.mapping is not None:
                self.header.closed = 1

    def close(self):
        with self._lock:
            if self.mapping is None:
                return
            self.header.closed = 1
            del self.header
            self.mapping.close()
            self.mapping = None
            kernel = c.WinDLL('kernel32')
            kernel.FreeLibrary.argtypes = [c.c_void_p]
            kernel.FreeLibrary(self._bridge._handle)
            self._bridge = self._publish = None


class VideoFilePlayback:
    def __init__(self, path, writer, *, loop=False, fps=15, session_alive=lambda: True):
        from wxbot_framework.emoticons import _ffmpeg_executable
        if type(loop) is not bool or type(fps) is not int or not 1 <= fps <= 30:
            raise ValueError('loop 必须是布尔值，fps 须为 1–30 的整数')
        path = Path(path).resolve(strict=True)
        if not path.is_file():
            raise ValueError('请选择本地视频文件')
        scale = (f'scale={writer.width}:{writer.height}:force_original_aspect_ratio=decrease,'
                 f'pad={writer.width}:{writer.height}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1')
        command = [shutil.which('ffmpeg') or _ffmpeg_executable(), '-nostdin', '-loglevel', 'error', '-threads', '2',
                   '-protocol_whitelist', 'file,pipe']
        if loop:
            command += ['-stream_loop', '-1']
        command += ['-i', str(path), '-an', '-sn', '-dn', '-vf', scale, '-r', str(fps),
                    '-pix_fmt', 'yuv420p', '-f', 'rawvideo', 'pipe:1']
        self.writer, self.fps = writer, fps
        self.error = None
        self.frames = 0
        self._stop = threading.Event()
        self._session_alive = session_alive
        self._process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                         stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        self._thread = threading.Thread(target=self._run, name='WxCallVideoDecoder', daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        deadline = time.monotonic()
        try:
            while not self._stop.is_set() and self._session_alive():
                frame = bytearray()
                while len(frame) < self.writer.payload_bytes and not self._stop.is_set():
                    block = self._process.stdout.read(self.writer.payload_bytes - len(frame))
                    if not block:
                        break
                    frame.extend(block)
                if self._stop.is_set():
                    break
                if not frame:
                    if self._process.wait(timeout=5):
                        raise RuntimeError('视频解码失败')
                    break
                if len(frame) != self.writer.payload_bytes:
                    raise RuntimeError('视频解码返回了不完整的帧')
                if self.writer.submit(frame):
                    self.frames += 1
                deadline += 1 / self.fps
                self._stop.wait(max(0, deadline - time.monotonic()))
                if time.monotonic() - deadline > 1:
                    deadline = time.monotonic()
        except Exception as error:
            self.error = error
        finally:
            self.writer.disarm()
            if self._process.poll() is None:
                self._process.terminate()

    def stop(self):
        self._stop.set()
        if self._process.poll() is None:
            self._process.terminate()
        if self._thread.ident is not None:
            self._thread.join(timeout=5)
        if self._thread.is_alive():
            raise RuntimeError('视频解码器尚未退出，保留帧通道等待清理重试')
        self._process.wait(timeout=5)
        self._process.stdout.close()


def bind_video_source(pid, writer, account, quality='native'):
    _call_video_control(pid, b'WxVideoSource', writer.control(1, account, quality))


def unbind_video_source(pid):
    _call_video_control(pid, b'WxVideoSource', SourceControl(size=c.sizeof(SourceControl), abi=1, operation=2))


def bind_video_capture(pid, mapping, account):
    _call_video_control(pid, b'WxVideoCapture', mapping.control(1, account))


def unbind_video_capture(pid):
    _call_video_control(pid, b'WxVideoCapture', SourceControl(size=c.sizeof(SourceControl), abi=1, operation=2))
