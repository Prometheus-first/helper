"""语音桥接的参数、导出和调度边界；不连接或操作微信。"""
from __future__ import annotations

import ctypes as c
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native"
ACCOUNT = "wxid_voice_test_self"
FRIEND = "wxid_voice_test_friend"
ROOM = "123456789@chatroom"
SILK = b"\x02#!SILK_V3" + b"\x03\0abc" * 50  # 仅结构测试，不用于播放


@unittest.skipUnless(sys.platform == "win32", "Windows 原生桥接参数")
class VoiceNativeRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if str(NATIVE) not in sys.path:
            sys.path.insert(0, str(NATIVE))
        import run_selftest
        cls.native = run_selftest

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.silk = self.root / "example.silk"
        self.silk.write_bytes(SILK)

    def test_config_size_offsets_and_duration_match_native(self):
        config = self.native.VoiceConfig()
        self.assertEqual(c.sizeof(config), 1824)
        self.assertEqual(self.native.VoiceConfig.account.offset, 1688)
        self.assertEqual(self.native.VoiceConfig.send.offset, 1820)
        self.assertEqual(self.native.VoiceConfig.duration_ms.offset, 1816)
        self.assertEqual(config.duration_ms, 0)

    def reject(self, **options):
        values = dict(bridge_path=self.native.TEXT_DLL, voice_path=self.silk, voice_duration_ms=1000, account=ACCOUNT)
        values.update(options)
        with patch.object(self.native.K, "OpenProcess") as open_process:
            with self.assertRaises((ValueError, TypeError)):
                self.native.run("--dispatch", **values)
        open_process.assert_not_called()

    def test_expected_personal_account_is_required_before_process_access(self):
        for account in (None, "", "filehelper", ROOM, "gh_example", "微信名", "x\0y"):
            with self.subTest(account=account):
                self.reject(account=account)

    def test_invalid_target_is_rejected_before_process_access(self):
        for target in ("", "a" * 128, "联系人名称", "x\0y", "a b"):
            with self.subTest(target=target):
                self.reject(target=target)

    def test_duration_must_match_the_packet_count_and_cannot_be_a_boolean(self):
        for duration in (None, True, 0, -20, 1001, 2000, 60020, "1000"):
            with self.subTest(duration=duration):
                self.reject(voice_duration_ms=duration)

    def test_media_and_mentions_cannot_be_mixed(self):
        for options in ({"image_path": self.silk}, {"image_probe": True},
                        {"mentions": [FRIEND]}, {"download_image": {}},
                        {"bridge_path": self.native.DLL}, {"voice_probe": 1}):
            with self.subTest(options=options):
                self.reject(**options)

    def test_missing_or_non_silk_files_do_not_access_process(self):
        png = self.root / "example.png"
        png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 30)
        for path in (self.root / "missing.silk", png, self.root):
            with self.subTest(path=path):
                self.reject(voice_path=path)

    def test_account_and_probe_parameters_cannot_silently_become_text(self):
        for options in ({"account": ACCOUNT}, {"voice_probe": True}):
            with self.subTest(options=options), patch.object(self.native.K, "OpenProcess") as open_process:
                with self.assertRaises(ValueError):
                    self.native.run("--dispatch", bridge_path=self.native.TEXT_DLL, **options)
                open_process.assert_not_called()

    def simulate(self, *, target=FRIEND, probe=False, keep_loaded=True, cleanup=0, report_changes=None):
        native = self.native
        client = self.root / "Weixin.exe"
        core = self.root / "Weixin.dll"
        core.write_bytes(b"synthetic compatible core fixture")
        (self.root / "native").mkdir(exist_ok=True)
        (self.root / "native/compatibility.json").write_text(
            json.dumps({"size": core.stat().st_size, "sha256": hashlib.sha256(core.read_bytes()).hexdigest()}), encoding="utf-8")
        loaded = keep_loaded
        allocations = {}
        calls = []
        configurations = []
        kernel = Mock()
        kernel.OpenProcess.return_value = 12345

        def query_path(process, flag, buffer, length):
            buffer.value = str(client)
            return 1

        def allocate(process, pointer, size, allocation, protect):
            return 0x500000 + len(allocations) * 0x10000

        def write(process, pointer, buffer, length, written):
            allocations[pointer] = c.string_at(buffer, length)
            written._obj.value = length
            return 1

        def modules(pid):
            result = {"weixin.dll": (0x180000000, str(core))}
            if loaded:
                result[native.TEXT_DLL.name.lower()] = (0x190000000, str(native.TEXT_DLL.resolve()))
            return result

        addresses = {b"WxSendVoice": 0x1000, b"WxInspectVoicePath": 0x2000,
                     b"WxRemoveSelfTest": 0x3000, b"WxWaitForTasks": 0x4000}

        def pe(path):
            if Path(path) == client:
                return SimpleNamespace(VS_FIXEDFILEINFO=[SimpleNamespace(
                    FileVersionMS=0x40001, FileVersionLS=0xf000d)])
            return SimpleNamespace(DIRECTORY_ENTRY_EXPORT=SimpleNamespace(symbols=[
                SimpleNamespace(name=name, address=address) for name, address in addresses.items()]))

        def remote(process, address, parameter):
            nonlocal loaded
            calls.append(address)
            if address == 0x70001000:
                loaded = True
                return 0
            if address == 0x70002000:
                loaded = False
                return 0
            if address in (0x190003000, 0x190004000):
                return cleanup
            config = native.VoiceConfig.from_buffer_copy(allocations[parameter])
            configurations.append(config)
            report = {"operation": "voice_probe" if probe else "voice_send",
                      "message_type": 34, "sent": not probe, "completion_success": True,
                      "target": config.target.decode("ascii"), "text": config.text.decode("utf-8"),
                      "voice_path": config.text.decode("utf-8"), "duration_ms": config.duration_ms}
            report.update(report_changes or {})
            Path(config.log_path).write_text(json.dumps(report), encoding="utf-8")
            return 0

        kernel.QueryFullProcessImageNameW.side_effect = query_path
        kernel.VirtualAllocEx.side_effect = allocate
        kernel.WriteProcessMemory.side_effect = write
        with (patch.object(native, "ROOT", self.root), patch.object(native, "K", kernel),
              patch.object(native, "modules", side_effect=modules),
              patch.object(native.pefile, "PE", side_effect=pe),
              patch.object(native, "remote_call", side_effect=remote),
              patch.object(native, "remote_system_function", side_effect=lambda pid, name:
                           0x70001000 if name == b"LoadLibraryW" else 0x70002000)):
            result = native.run("--dispatch", bridge_path=native.TEXT_DLL, pid=123,
                target=target, account=ACCOUNT, voice_path=self.silk, voice_duration_ms=1000,
                voice_probe=probe, keep_loaded=keep_loaded)
        self.assertEqual(len(configurations), 1)
        return result, configurations[0], calls, kernel

    def test_send_export_receives_general_target_account_and_owned_config(self):
        result, config, calls, kernel = self.simulate(target=ROOM)
        self.assertTrue(result["success"])
        self.assertEqual(calls, [0x190001000, 0x190004000])
        self.assertEqual((config.size, config.pid, config.target, config.account, config.send, config.duration_ms),
                         (1824, 123, ROOM.encode(), ACCOUNT.encode(), 1, 1000))
        self.assertEqual(config.text.decode("utf-8"), str(self.silk.resolve()))
        self.assertFalse(result["dll_unloaded"])
        kernel.VirtualFreeEx.assert_called_once()

    def test_probe_uses_separate_non_sending_export_and_mode(self):
        result, config, calls, _ = self.simulate(probe=True)
        self.assertTrue(result["success"])
        self.assertEqual(calls, [0x190002000, 0x190004000])
        self.assertEqual(config.send, 0)
        self.assertFalse(result["report"]["sent"])

    def test_owned_dll_is_freed_only_after_successful_task_cleanup(self):
        result, _, calls, _ = self.simulate(probe=True, keep_loaded=False)
        self.assertTrue(result["success"])
        self.assertEqual(calls, [0x70001000, 0x190002000, 0x190003000, 0x70002000])
        self.assertTrue(result["dll_unloaded"])

    def test_busy_cleanup_keeps_dll_loaded_and_does_not_report_success(self):
        result, _, calls, _ = self.simulate(keep_loaded=False, cleanup=170)
        self.assertFalse(result["success"])
        self.assertNotIn(0x70002000, calls)
        self.assertFalse(result["dll_unloaded"])

    def test_wrong_message_kind_or_sent_state_cannot_pass_as_type_34(self):
        for changes in ({"message_type": 3}, {"sent": False}, {"operation": "image_send"}, {"duration_ms": 2000}, {"voice_path": "wrong.silk"}):
            with self.subTest(changes=changes):
                result, _, _, _ = self.simulate(report_changes=changes)
                self.assertFalse(result["success"])


@unittest.skipUnless(sys.platform == "win32", "Windows 编译后导出 ABI")
class VoiceCompiledABITests(unittest.TestCase):
    def test_exports_reject_null_and_wrong_modes_without_weixin(self):
        if str(NATIVE) not in sys.path:
            sys.path.insert(0, str(NATIVE))
        from run_selftest import VoiceConfig, TEXT_DLL
        import pefile
        with pefile.PE(str(TEXT_DLL)) as pe:
            exports = {entry.name for entry in pe.DIRECTORY_ENTRY_EXPORT.symbols}
        self.assertTrue({b"WxSendVoice", b"WxInspectVoicePath"}.issubset(exports))
        bridge = c.WinDLL(str(TEXT_DLL))
        kernel = c.WinDLL("kernel32", use_last_error=True)
        kernel.FreeLibrary.argtypes = [c.c_void_p]
        kernel.FreeLibrary.restype = c.c_int
        try:
            for name, expected_mode in (("WxSendVoice", 1), ("WxInspectVoicePath", 0)):
                with self.subTest(export=name):
                    function = getattr(bridge, name)
                    function.argtypes = [c.c_void_p]
                    function.restype = c.c_uint32
                    self.assertEqual(function(None), 87)
                    config = VoiceConfig()
                    config.size, config.pid = c.sizeof(config), os.getpid()
                    config.text, config.target, config.account = b"unused.silk", FRIEND.encode(), ACCOUNT.encode()
                    config.log_path = "unused-report.json"
                    config.duration_ms = 1000
                    config.send = 1 - expected_mode
                    self.assertEqual(function(c.byref(config)), 87)
                    config.send, config.duration_ms = expected_mode, 0
                    self.assertEqual(function(c.byref(config)), 87)
            bridge.WxWaitForTasks.argtypes = [c.c_void_p]
            bridge.WxWaitForTasks.restype = c.c_uint32
            self.assertEqual(bridge.WxWaitForTasks(None), 0)
        finally:
            self.assertTrue(kernel.FreeLibrary(bridge._handle))


if __name__ == "__main__":
    unittest.main()
