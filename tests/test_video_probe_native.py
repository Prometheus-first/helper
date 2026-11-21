"""Video observer boundaries; never connect to or call a real WeChat process."""
import ctypes as c
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'win32', 'Windows native ABI')
class VideoProbeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / 'native'))

    def test_invalid_abi_and_unsupported_build_preserve_guard_bytes(self):
        from video_control import VideoProbeControl
        bridge = c.WinDLL(str(ROOT / 'native/build_text/text_bridge.dll'))
        kernel = c.WinDLL('kernel32')
        kernel.FreeLibrary.argtypes = [c.c_void_p]
        try:
            fn = bridge.WxVideoProbe
            fn.argtypes = [c.c_void_p]
            fn.restype = c.c_uint32
            self.assertEqual(fn(None), 87)
            for size, abi, op, expected in ((0, 1, 1, 87), (104, 2, 1, 87),
                                            (104, 1, 4, 87), (104, 1, 1, 50),
                                            (104, 1, 2, 0), (104, 1, 3, 0)):
                with self.subTest(size=size, abi=abi, operation=op):
                    raw = (c.c_ubyte * 232)(*([0xa5] * 232))
                    control = VideoProbeControl.from_buffer(raw)
                    control.size, control.abi, control.operation = size, abi, op
                    original = bytes(raw)
                    self.assertEqual(fn(c.byref(control)), expected)
                    self.assertEqual(bytes(raw[104:]), b'\xa5' * 128)
                    if expected:
                        self.assertEqual(bytes(raw), original)
                    else:
                        self.assertEqual((control.size, control.abi, control.armed), (104, 1, 0))
        finally:
            self.assertTrue(kernel.FreeLibrary(bridge._handle))

    def test_disconnected_or_changed_account_cannot_arm_probe(self):
        from text_service import NativeTextService
        service = NativeTextService(pid=123)
        with patch('video_control.call_video_probe') as native:
            with self.assertRaises(RuntimeError):
                service.video_probe('start')
            service._owns_bridge, service._channel = True, Mock()
            service.router.set_account('wxid_video_test')
            service._read_account = Mock(return_value='wxid_changed')
            with self.assertRaises(RuntimeError):
                service.video_probe('start')
            native.assert_not_called()
        service._owns_bridge = False

    def test_source_and_quality_exports_respect_their_boundaries(self):
        from video_control import VideoQuality
        from video_source import SourceControl
        bridge = c.WinDLL(str(ROOT / 'native/build_text/text_bridge.dll'))
        kernel = c.WinDLL('kernel32')
        kernel.FreeLibrary.argtypes = [c.c_void_p]
        try:
            for name, structure, operation, expected in (
                    ('WxVideoQuality', VideoQuality, None, 0),
                    ('WxVideoSource', SourceControl, 1, 50),
                    ('WxVideoSource', SourceControl, 2, 0),
                    ('WxVideoCapture', SourceControl, 1, 50),
                    ('WxVideoCapture', SourceControl, 2, 0)):
                fn = getattr(bridge, name)
                fn.argtypes, fn.restype = [c.c_void_p], c.c_uint32
                size = c.sizeof(structure)
                raw = (c.c_ubyte * (size + 64))(*([0xa5] * (size + 64)))
                value = structure.from_buffer(raw)
                value.size, value.abi = size, 1
                if operation is not None:
                    value.operation = operation
                self.assertEqual(fn(c.byref(value)), expected)
                self.assertEqual(bytes(raw[size:]), b'\xa5' * 64)
                value.size -= 1
                before = bytes(raw)
                self.assertEqual(fn(c.byref(value)), 87)
                self.assertEqual(bytes(raw), before)
        finally:
            self.assertTrue(kernel.FreeLibrary(bridge._handle))


if __name__ == '__main__':
    unittest.main()
