"""Read bounded, session-scoped WeChat I420 input without opening a camera."""
import ctypes as c
import mmap
from pathlib import Path
import re
import struct


class ReadControl(c.Structure):
    _fields_ = [('size', c.c_uint32), ('abi', c.c_uint32), ('mapping', c.c_void_p),
        ('mapping_bytes', c.c_uint64), ('pixels', c.c_void_p), ('pixel_bytes', c.c_uint64),
        ('sequence', c.c_uint64), ('captured_tick', c.c_uint64)]


assert c.sizeof(ReadControl) == 56


class CapturedFrames:
    def __init__(self, config, cv2):
        import numpy as np
        self.cv2, self.np = cv2, np
        name = config.get('name', '')
        width, height = config.get('width'), config.get('height')
        if (not isinstance(name, str) or not re.fullmatch(r'Local\\wxbot-video-[0-9a-f]{32}', name) or
                type(width) is not int or type(height) is not int or width % 2 or height % 2 or
                not 16 <= width <= 1920 or not 16 <= height <= 1920 or width * height > 2073600):
            raise RuntimeError('invalid_wechat_capture')
        self.width, self.height = width, height
        payload = width * height * 3 // 2
        expected_bytes = 128 + 3 * (64 + ((payload + 63) & ~63))
        if config.get('bytes') != expected_bytes:
            raise RuntimeError('invalid_wechat_capture_size')
        self.mapping = mmap.mmap(-1, expected_bytes, tagname=name)
        header = struct.unpack_from('<10I2Q', self.mapping)
        if (header[:9] != (0x31564257, 1, expected_bytes, width, height, 1,
                64 + ((payload + 63) & ~63), 3, config.get('owner_pid')) or
                list(header[10:12]) != config.get('generation')):
            self.mapping.close()
            raise RuntimeError('wechat_capture_identity_changed')
        self._bridge = c.WinDLL(str(Path(__file__).resolve().parent / 'build_text/text_bridge.dll'))
        self._read = self._bridge.WxReadVideoFrame
        self._read.argtypes, self._read.restype = [c.c_void_p], c.c_uint32
        self._pixels = c.create_string_buffer(payload)
        self._control = ReadControl(size=c.sizeof(ReadControl), abi=1,
            mapping=c.addressof(c.c_ubyte.from_buffer(self.mapping)), mapping_bytes=expected_bytes,
            pixels=c.addressof(self._pixels), pixel_bytes=payload)

    def latest(self, after):
        code = self._read(c.byref(self._control))
        if code == 259:
            return None
        if code:
            raise RuntimeError('wechat_capture_closed')
        if self._control.sequence <= after:
            return None
        pixels = self.np.frombuffer(self._pixels, dtype=self.np.uint8).reshape(self.height * 3 // 2, self.width)
        frame = self.cv2.cvtColor(pixels, self.cv2.COLOR_YUV2BGR_I420)
        return self._control.sequence, self._control.captured_tick * 1000000, frame

    def close(self):
        self.mapping.close()
        kernel = c.WinDLL('kernel32')
        kernel.FreeLibrary.argtypes = [c.c_void_p]
        kernel.FreeLibrary(self._bridge._handle)
        self._bridge = self._read = None
