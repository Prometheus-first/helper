"""图片原生适配边界：不连接微信，ABI 检查仅在本测试进程加载桥接。"""
from __future__ import annotations

import ctypes as c
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

from wxbot_framework.image_metadata import parse_image_metadata

ROOT = Path(__file__).resolve().parents[1]
SELF = "wxid_image_test_self"
FRIEND = "wxid_image_test_friend"
ROOM = "12345678@chatroom"
XML = '<msg><img aeskey="' + 'a' * 32 + '" cdnmidimgurl="test-file-key" /></msg>'


@unittest.skipUnless(sys.platform == "win32", "Windows 桥接 ABI")
class NativeImageABITests(unittest.TestCase):
    def test_all_poll_exports_respect_their_callers_buffer(self):
        bridge = c.WinDLL(str(ROOT / "native/build_text/text_bridge.dll"))
        kernel = c.WinDLL("kernel32", use_last_error=True)
        kernel.FreeLibrary.argtypes = [c.c_void_p]
        kernel.FreeLibrary.restype = c.c_int
        try:
            for name in ("WxSendImage", "WxInspectImagePath", "WxDownloadImage"):
                self.assertTrue(getattr(bridge, name))
            for name, code in (("WxSendImage", 170), ("WxInspectImagePath", 170), ("WxDownloadImage", 87)):
                function = getattr(bridge, name)
                function.argtypes = [c.c_void_p]
                function.restype = c.c_uint32
                self.assertEqual(function(None), code)
            bridge.WxWaitForTasks.argtypes = [c.c_void_p]
            bridge.WxWaitForTasks.restype = c.c_uint32
            self.assertEqual(bridge.WxWaitForTasks(None), 0)
            for name, size in (("WxPollFileHelperMessage", 1312), ("WxPollTextMessage", 1312),
                               ("WxPollTextMessageV2", 5408), ("WxPollMediaMessageV3", 21792),
                               ("WxPollMediaMessageV4", 267552), ("WxPollMediaMessageV5", 267560)):
                with self.subTest(export=name):
                    function = getattr(bridge, name)
                    function.argtypes = [c.c_void_p]
                    function.restype = c.c_uint32
                    buffer = (c.c_ubyte * (size + 128))(*([0xA5] * (size + 128)))
                    self.assertEqual(function(c.byref(buffer)), 0)
                    self.assertEqual(c.c_uint32.from_buffer(buffer).value, size)
                    self.assertEqual(c.c_uint32.from_buffer(buffer, 4).value, 0)
                    self.assertEqual(bytes(buffer[size:]), b"\xa5" * 128)
                    self.assertEqual(function(None), 87)
        finally:
            self.assertTrue(kernel.FreeLibrary(bridge._handle))

    def test_python_structures_match_the_versioned_native_boundaries(self):
        sys.path.insert(0, str(ROOT / "native"))
        from filehelper_service import ReceivedMessage, ReceivedTextMessage, ReceivedMediaMessage
        from filehelper_service import ReceivedMediaMessageV4, ReceivedMediaMessageV5
        from run_selftest import ImageDownloadConfig
        self.assertEqual(c.sizeof(ReceivedMessage), 1312)
        self.assertEqual(c.sizeof(ReceivedTextMessage), 5408)
        self.assertEqual((ReceivedMediaMessage.image_xml.offset, c.sizeof(ReceivedMediaMessage)),
                         (5408, 21792))
        self.assertEqual(c.sizeof(ImageDownloadConfig), 2656)
        self.assertEqual((ReceivedMediaMessageV4.image_xml.offset, c.sizeof(ReceivedMediaMessageV4)),
                         (5408, 267552))
        self.assertEqual((ReceivedMediaMessageV5.native_message_id.offset, c.sizeof(ReceivedMediaMessageV5)),
                         (267552, 267560))


@unittest.skipUnless(sys.platform == "win32", "Windows 原生服务适配")
class NativeImageServiceTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(ROOT / "native"))
        from text_service import NativeTextService
        self.service = NativeTextService(pid=123, account=SELF, image_allowlist=("filehelper", ROOM))
        self.service.router.set_account(SELF)
        self.service._owns_bridge = True
        self.service._read_account = Mock(return_value=SELF)
        self.service._sender_wechat_name = Mock(return_value=None)
        self.service._image_receiver = Mock()
        self.service._run = Mock(return_value={"success": True})

    def raw_image(self, sender, body=XML):
        from filehelper_service import ReceivedMediaMessage
        return ReceivedMediaMessage(type=3, sequence=42, sender=sender.encode(),
                                    receiver=SELF.encode(), image_xml=body.encode())

    def test_group_image_is_queued_without_sending_xml_to_text_handlers(self):
        result = self.service._map_message(self.raw_image(ROOM, FRIEND + ":\n" + XML))
        self.assertIsNone(result)
        data = self.service._image_receiver.submit.call_args.args[0]
        self.assertEqual((data["wx_type"], data["chat_id"], data["sender_id"], data["raw_content"]),
                         (3, ROOM, FRIEND, XML))
        self.service._run.assert_not_called()

    def test_image_allowlist_is_applied_before_download(self):
        self.assertIsNone(self.service._map_message(self.raw_image(FRIEND)))
        self.service._image_receiver.submit.assert_not_called()
        self.assertIn(FRIEND, self.service.chats)
        self.assertIsNone(self.service._map_message(self.raw_image("filehelper")))
        self.service._image_receiver.submit.assert_called_once()

    def test_download_rechecks_account_before_and_after_native_work(self):
        path = ROOT / ".runtime/tests/not-created-image.bin"
        self.assertTrue(self.service._download_image(parse_image_metadata(XML), path))
        self.assertEqual(self.service._read_account.call_count, 2)
        args, options = self.service._run.call_args
        self.assertEqual(args, ("--download-image",))
        self.assertTrue(options["keep_loaded"])
        self.assertEqual(options["download_image"]["destination"], str(path))

    def test_changed_account_cannot_download_old_image(self):
        self.service._read_account.return_value = "wxid_other_login"
        with self.assertRaisesRegex(RuntimeError, "当前账号发生变化"):
            self.service._download_image(parse_image_metadata(XML), ROOT / ".runtime/tests/not-created.bin")
        self.service._run.assert_not_called()
        self.assertTrue(self.service._stop.is_set())

    def test_missing_download_keys_do_not_enter_native_code(self):
        self.assertFalse(self.service._download_image(parse_image_metadata('<msg><img /></msg>'), Path("unused")))
        self.service._run.assert_not_called()

    def test_reference_quality_selects_the_matching_native_download_type(self):
        variants = (("cdnmidimgurl", "aeskey", 2), ("cdnbigimgurl", "aeskey", 1),
                    ("cdnthumburl", "cdnthumbaeskey", 3))
        for url_field, aes_field, kind in variants:
            with self.subTest(quality=url_field):
                metadata = parse_image_metadata(f'<msg><img {url_field}="test-key" {aes_field}="' + 'a' * 32 + '" /></msg>')
                self.assertTrue(self.service._download_image(metadata, Path("unused")))
                self.assertEqual(self.service._run.call_args.kwargs["download_image"]["image_type"], kind)

    def test_image_send_rejects_another_login_before_native_call(self):
        scratch = ROOT / ".runtime/tests"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch) as directory:
            image = Path(directory) / "sample.png"
            image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
            self.service._read_account.return_value = "wxid_other_login"
            with self.assertRaisesRegex(RuntimeError, "当前账号发生变化"):
                self.service.send_image("filehelper", image)
        self.service._run.assert_not_called()

    def test_busy_image_worker_prevents_bridge_unload(self):
        self.service._image_receiver.is_alive = True
        with self.assertRaisesRegex(RuntimeError, "图片原生下载尚未结束"):
            self.service.stop()
        self.assertTrue(self.service._owns_bridge)
        self.service._run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
