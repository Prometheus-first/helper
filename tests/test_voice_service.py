"""原生语音服务的准备、账号和清理边界，全部模拟原生调用。"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT = "wxid_voice_test_self"
FRIEND = "wxid_voice_test_friend"
ROOM = "1234567890@chatroom"
SOURCE = ROOT / "tests/not-created-source.wav"
PREPARED = ROOT / ".runtime/voices" / ACCOUNT / "not-created-snapshot.silk"


@unittest.skipUnless(sys.platform == "win32", "Windows 原生服务")
class NativeVoiceServiceTests(unittest.TestCase):
    def setUp(self):
        native = str(ROOT / "native")
        if native not in sys.path:
            sys.path.insert(0, native)
        from text_service import NativeTextService
        self.service = NativeTextService(pid=123, account=ACCOUNT, preserve_reports=False)
        self.service.router.set_account(ACCOUNT)
        self.service._owns_bridge = True
        self.service._read_account = Mock(return_value=ACCOUNT)
        self.service._run = Mock(return_value={"success": True, "report": {"sent": True}})
        self.events = []

        @contextmanager
        def prepare(path, directory):
            self.events.append(("prepare", path, directory))
            try:
                yield SimpleNamespace(path=PREPARED, duration_ms=1200)
            finally:
                self.events.append(("release",))

        self.prepare = self.enterContext(patch("wxbot_framework.voices.prepare_voice", side_effect=prepare))

    def test_friend_group_and_filehelper_voice_use_prepared_path_and_duration(self):
        for target in (FRIEND, ROOM, "filehelper"):
            with self.subTest(target=target):
                self.assertTrue(self.service.send_voice(target, SOURCE))
                self.service._run.assert_called_with(
                    "--dispatch", pid=123, keep_loaded=True, preserve_report=False,
                    target=target, voice_path=str(PREPARED), voice_duration_ms=1200,
                    account=ACCOUNT)
                self.assertIs(self.service.last_send_result, self.service.last_result)
        self.assertEqual(self.service._read_account.call_count, 9)

    def test_prepared_files_belong_only_to_current_account(self):
        self.assertTrue(self.service.send_voice(FRIEND, SOURCE))
        self.prepare.assert_called_once_with(SOURCE, ROOT / ".runtime/voices" / ACCOUNT)
        self.assertEqual(self.events[-1], ("release",))

    def test_preparation_is_held_alive_until_native_send_finishes(self):
        def dispatch(*args, **kwargs):
            self.assertEqual(self.events, [("prepare", SOURCE, ROOT / ".runtime/voices" / ACCOUNT)])
            self.events.append(("send",))
            return {"success": True}

        self.service._run.side_effect = dispatch
        self.service.send_voice(FRIEND, SOURCE)
        self.assertEqual([event[0] for event in self.events], ["prepare", "send", "release"])

    def test_false_or_absent_success_is_preserved_without_claiming_delivery(self):
        for result in ({"success": False}, {}):
            with self.subTest(result=result):
                self.service._run.return_value = result
                self.assertFalse(self.service.send_voice(FRIEND, SOURCE))
                self.assertIs(self.service.last_send_result, result)

    def test_invalid_target_never_prepares_or_dispatches(self):
        with self.assertRaises(ValueError):
            self.service.send_voice("昵称", SOURCE)
        self.prepare.assert_not_called()
        self.service._run.assert_not_called()

    def test_disconnected_or_stopped_service_does_not_prepare_files(self):
        self.service._owns_bridge = False
        with self.assertRaisesRegex(RuntimeError, "服务没有运行"):
            self.service.send_voice(FRIEND, SOURCE)
        self.service._owns_bridge = True
        self.service._stop.set()
        with self.assertRaisesRegex(RuntimeError, "服务没有运行"):
            self.service.send_voice(FRIEND, SOURCE)
        self.prepare.assert_not_called()
        self.service._run.assert_not_called()

    def test_current_login_is_checked_before_file_preparation(self):
        self.service._read_account.return_value = "wxid_other_login"
        with self.assertRaisesRegex(RuntimeError, "当前账号发生变化"):
            self.service.send_voice(FRIEND, SOURCE)
        self.prepare.assert_not_called()
        self.service._run.assert_not_called()
        self.assertTrue(self.service._stop.is_set())

    def test_invalid_account_cannot_be_used_as_preparation_subdirectory(self):
        self.service.router.self_wxid = ROOM
        self.service._read_account.return_value = ROOM
        with self.assertRaises(ValueError):
            self.service.send_voice(FRIEND, SOURCE)
        self.prepare.assert_not_called()
        self.service._run.assert_not_called()

    def test_dot_accounts_cannot_escape_or_replace_account_preparation_directory(self):
        for account in (".", ".."):
            with self.subTest(account=account):
                self.service.router.self_wxid = account
                self.service._read_account.return_value = account
                with self.assertRaises(ValueError):
                    self.service.send_voice(FRIEND, SOURCE)
        self.prepare.assert_not_called()
        self.service._run.assert_not_called()

    def test_account_change_during_preparation_cancels_native_send(self):
        self.service._read_account.side_effect = [ACCOUNT, "wxid_other_login"]
        with self.assertRaisesRegex(RuntimeError, "当前账号发生变化"):
            self.service.send_voice(FRIEND, SOURCE)
        self.service._run.assert_not_called()
        self.assertEqual(self.events[-1], ("release",))

    def test_stop_requested_during_preparation_cancels_native_send(self):
        @contextmanager
        def prepare(*args):
            self.service._stop.set()
            yield SimpleNamespace(path=PREPARED, duration_ms=1200)

        self.prepare.side_effect = prepare
        with self.assertRaisesRegex(RuntimeError, "连接已结束"):
            self.service.send_voice(FRIEND, SOURCE)
        self.service._run.assert_not_called()

    def test_invalid_source_or_conversion_failure_does_not_dispatch(self):
        self.prepare.side_effect = ValueError("WAV 转码失败")
        with self.assertRaises(ValueError):
            self.service.send_voice(FRIEND, SOURCE)
        self.service._run.assert_not_called()

    def test_native_error_releases_preparation_context(self):
        self.service._run.side_effect = RuntimeError("原生发送失败")
        with self.assertRaisesRegex(RuntimeError, "原生发送失败"):
            self.service.send_voice(FRIEND, SOURCE)
        self.assertEqual(self.events[-1], ("release",))


if __name__ == "__main__":
    unittest.main()
