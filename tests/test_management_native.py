"""原生好友与群管理的入参及编译后导出检查；不注入微信。"""
import ctypes as c
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from wxbot_framework.friend_requests import parse_friend_request

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "wxid_management_robot"
FIRST, SECOND = "wxid_management_first", "wxid_management_second"
ROOM = "123456789@chatroom"
XML = ('<msg fromusername="wxid_management_first" fromnickname="测试好友" '
       'content="测试验证词" scene="30" opcode="2" encryptusername="v3_SYNTHETIC_USER" '
       'ticket="v4_SYNTHETIC_TICKET"/>')


@unittest.skipUnless(sys.platform == "win32", "Windows 原生参数与导出")
class ManagementNativeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if str(ROOT / "native") not in sys.path:
            sys.path.insert(0, str(ROOT / "native"))
        import run_selftest
        cls.native = run_selftest

    def test_abi_offsets_and_sizes(self):
        group = self.native.GroupManagementConfig
        friend = self.native.FriendRequestConfig
        self.assertEqual(c.sizeof(group), 2848)
        self.assertEqual((group.account.offset, group.members.offset, group.operation.offset), (1688, 1816, 2840))
        self.assertEqual(c.sizeof(friend), 18208)
        self.assertEqual((friend.v3.offset, friend.v4.offset, friend.scene.offset), (1816, 10008, 18200))

    def test_invalid_group_operations_never_open_process(self):
        valid = {"account": ACCOUNT, "group_operation": "invite", "group_members": [FIRST], "target": ROOM}
        for changes in ({"account": None}, {"group_members": [ACCOUNT]}, {"group_members": [FIRST, FIRST]},
                        {"group_members": []}, {"group_operation": "unknown"}, {"target": FIRST},
                        {"group_operation": "create", "group_members": [FIRST]},
                        {"group_probe": 1}, {"image_probe": True}, {"download_file": {}},
                        {"friend_request": parse_friend_request(XML)}):
            with self.subTest(changes=changes), patch.object(self.native.K, "OpenProcess") as opened:
                with self.assertRaises((ValueError, TypeError)):
                    self.native.run("--manage-group", bridge_path=self.native.TEXT_DLL, **dict(valid, **changes))
                opened.assert_not_called()

    def test_friend_result_notifications_and_invalid_accounts_never_open_process(self):
        valid = {"account": ACCOUNT, "friend_request": parse_friend_request(XML)}
        for changes in ({"account": None}, {"friend_request": None}, {"friend_probe": 1},
                        {"group_operation": "invite"}, {"file_probe": True},
                        {"friend_request": parse_friend_request(XML.replace('opcode="2"', 'opcode="3"'))}):
            with self.subTest(changes=changes), patch.object(self.native.K, "OpenProcess") as opened:
                with self.assertRaises((ValueError, TypeError)):
                    self.native.run("--accept-friend", bridge_path=self.native.TEXT_DLL, **dict(valid, **changes))
                opened.assert_not_called()

    def test_management_parameters_cannot_silently_become_text(self):
        for options in ({"group_probe": True}, {"friend_probe": True}, {"group_members": [FIRST]},
                        {"friend_request": parse_friend_request(XML)}):
            with self.subTest(options=options), patch.object(self.native.K, "OpenProcess") as opened:
                with self.assertRaises(ValueError):
                    self.native.run("--dispatch", bridge_path=self.native.TEXT_DLL, **options)
                opened.assert_not_called()

    def test_compiled_exports_reject_null_and_wrong_modes_without_weixin(self):
        import pefile
        names = {b"WxInspectGroupManagement", b"WxInviteGroupMembers", b"WxCreateGroup",
                 b"WxInspectFriendRequest", b"WxAcceptFriendRequest"}
        with pefile.PE(str(self.native.TEXT_DLL)) as pe:
            exports = {entry.name for entry in pe.DIRECTORY_ENTRY_EXPORT.symbols}
        self.assertTrue(names.issubset(exports))
        bridge = c.WinDLL(str(self.native.TEXT_DLL))
        kernel = c.WinDLL("kernel32", use_last_error=True)
        kernel.FreeLibrary.argtypes = [c.c_void_p]
        kernel.FreeLibrary.restype = c.c_int
        try:
            for name in names:
                function = getattr(bridge, name.decode())
                function.argtypes = [c.c_void_p]
                function.restype = c.c_uint32
                self.assertEqual(function(None), 87)
            config = self.native.GroupManagementConfig()
            config.size, config.pid = c.sizeof(config), os.getpid()
            config.account, config.members = ACCOUNT.encode(), (FIRST + "," + SECOND).encode()
            config.log_path = "unused-report.json"
            config.operation, config.target = 2, ROOM.encode()
            self.assertEqual(bridge.WxInviteGroupMembers(c.byref(config)), 87)
            config.operation = 1
            self.assertEqual(bridge.WxCreateGroup(c.byref(config)), 87)
        finally:
            kernel.FreeLibrary(bridge._handle)


if __name__ == "__main__":
    unittest.main()
