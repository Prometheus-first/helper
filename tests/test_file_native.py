"""原生文件参数拒绝边界与编译后 ABI；不向微信注入。"""
import ctypes as c
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native"
ACCOUNT = "wxid_file_test_robot"


@unittest.skipUnless(sys.platform == "win32", "Windows 原生桥接")
class FileNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if str(NATIVE) not in sys.path:
            sys.path.insert(0, str(NATIVE))
        import run_selftest
        cls.native = run_selftest

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / "test.txt"
        self.source.write_bytes(b"WXBOT_FILE_TEST")

    def test_file_config_offsets_match_native(self):
        config = self.native.FileConfig
        self.assertEqual(c.sizeof(config), 1824)
        self.assertEqual(config.account.offset, 1688)
        self.assertEqual(config.send.offset, 1816)
        self.assertEqual(config.reserved.offset, 1820)
        download = self.native.FileDownloadConfig
        self.assertEqual(c.sizeof(download), 2792)
        self.assertEqual(download.expected_size.offset, 2656)
        self.assertEqual(download.account.offset, 2664)

    def test_download_requires_bounded_size_and_cdn_credentials_before_process_access(self):
        valid = {"destination": str(self.source.parent / "new.txt"), "filekey": "synthetic-file-key",
                 "aeskey": "ab" * 16, "expected_size": 14}
        for changes in ({"expected_size": None}, {"expected_size": True}, {"expected_size": 0},
                        {"expected_size": (1 << 63)}, {"filekey": ""},
                        {"filekey": "abc\n"}, {"aeskey": "invalid"},
                        {"destination": str(self.source)}):
            data = dict(valid, **changes)
            with self.subTest(changes=changes), patch.object(self.native.K, "OpenProcess") as open_process:
                with self.assertRaises(ValueError):
                    self.native.run("--download-file", bridge_path=self.native.TEXT_DLL,
                                    download_file=data, account=ACCOUNT)
                open_process.assert_not_called()

    def test_download_cannot_mix_any_media_send(self):
        data = {"destination": str(self.source.parent / "new.txt"), "filekey": "synthetic-file-key",
                "aeskey": "ab" * 16, "expected_size": 14}
        for options in ({"file_path": self.source}, {"image_path": self.source},
                        {"voice_path": self.source}, {"emoticon_path": self.source}, {"download_image": {}}):
            with self.subTest(options=options), patch.object(self.native.K, "OpenProcess") as open_process:
                with self.assertRaises(ValueError):
                    self.native.run("--download-file", bridge_path=self.native.TEXT_DLL,
                                    download_file=data, account=ACCOUNT, **options)
                open_process.assert_not_called()

    def test_mixed_media_invalid_account_and_invalid_files_never_access_process(self):
        for changes in (
            {"account": None}, {"account": "filehelper"}, {"account": "1@chatroom"},
            {"file_probe": 1}, {"image_probe": True}, {"image_path": self.source},
            {"emoticon_path": self.source}, {"voice_path": self.source},
            {"mentions": ["wxid_friend"]}, {"download_image": {}},
            {"target": "群名"}, {"file_path": self.source.parent},
            {"file_path": self.source.parent / "missing"},
        ):
            values = dict(account=ACCOUNT, file_path=self.source)
            values.update(changes)
            with self.subTest(changes=changes), patch.object(self.native.K, "OpenProcess") as open_process:
                with self.assertRaises((ValueError, TypeError)):
                    self.native.run("--dispatch", bridge_path=self.native.TEXT_DLL, **values)
                open_process.assert_not_called()

    def test_probe_without_file_cannot_silently_send_text(self):
        with patch.object(self.native.K, "OpenProcess") as open_process:
            with self.assertRaises(ValueError):
                self.native.run("--dispatch", bridge_path=self.native.TEXT_DLL, file_probe=True)
            open_process.assert_not_called()

    def test_compiled_exports_reject_wrong_modes_and_null_without_weixin(self):
        import pefile
        with pefile.PE(str(self.native.TEXT_DLL)) as pe:
            exports = {entry.name for entry in pe.DIRECTORY_ENTRY_EXPORT.symbols}
        self.assertTrue({b"WxSendFile", b"WxInspectFilePath", b"WxDownloadFile"}.issubset(exports))
        bridge = c.WinDLL(str(self.native.TEXT_DLL))
        kernel = c.WinDLL("kernel32", use_last_error=True)
        kernel.FreeLibrary.argtypes = [c.c_void_p]
        kernel.FreeLibrary.restype = c.c_int
        try:
            download = bridge.WxDownloadFile
            download.argtypes = [c.c_void_p]
            download.restype = c.c_uint32
            self.assertEqual(download(None), 87)
            for name, mode in (("WxSendFile", 1), ("WxInspectFilePath", 0)):
                function = getattr(bridge, name)
                function.argtypes = [c.c_void_p]
                function.restype = c.c_uint32
                self.assertEqual(function(None), 87)
                config = self.native.FileConfig()
                config.size, config.pid = c.sizeof(config), os.getpid()
                config.text, config.target, config.account = b"unused.txt", b"filehelper", ACCOUNT.encode()
                config.send = 1 - mode
                self.assertEqual(function(c.byref(config)), 87)
        finally:
            kernel.FreeLibrary(bridge._handle)


if __name__ == "__main__":
    unittest.main()
