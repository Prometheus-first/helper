"""语音发送的插件、会话隔离与控制台边界；不连接微信或网络。"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from wxbot_framework.backend import CAPABILITIES, TEXT_CAPABILITIES, NativeTextBackend
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import CapabilityError, LegacyService, Message, PluginContext, ServiceFacade

FRIEND = "wxid_voice_test_friend"
ROOM = "1234567890@chatroom"
VOICE = Path(__file__).resolve().parent / "not-created-voice.silk"


class VoiceBackend:
    def __init__(self, session="original"):
        self.session_id = session
        self.sent = []
        self.result = True

    def send_voice(self, chat_id, path):
        self.sent.append((chat_id, path))
        return self.result

    def stop(self):
        pass


class WaitingSendLock:
    def __init__(self):
        self.lock = threading.Lock()
        self.waiting = threading.Event()

    def __enter__(self):
        self.waiting.set()
        self.lock.acquire()
        return self

    def __exit__(self, *args):
        self.lock.release()


class VoiceSDKTests(unittest.TestCase):
    def setUp(self):
        # 格式本身由 test_voices 校验；这里用无文件的快照模拟隔离发送边界。
        self.inspect = self.enterContext(patch("wxbot_framework.voices.inspect_voice_file",
                                               return_value=SimpleNamespace(path=VOICE, duration_ms=1000)))
        self.backend = VoiceBackend()
        self.service = ServiceFacade(text_delay={"enabled": False})
        self.service.bind(self.backend)
        self.addCleanup(self.service.close)
        self.context = PluginContext("voice-test", {}, self.service, {"voice.send"})

    def test_plugin_reply_and_legacy_apis_forward_absolute_paths_without_text_delay(self):
        self.service.text_delay = Mock()
        self.assertTrue(self.context.send_voice(FRIEND, VOICE))
        self.assertTrue(self.context.reply_voice(Message("", ROOM, session_id="original"), str(VOICE)))
        self.assertTrue(LegacyService(self.service, "original").send_voice("filehelper", VOICE))
        self.assertEqual(self.backend.sent, [(FRIEND, str(VOICE)), (ROOM, str(VOICE)),
                                            ("filehelper", str(VOICE))])
        self.service.text_delay.seconds_for.assert_not_called()

    def test_backend_failure_is_returned_to_caller(self):
        self.backend.result = False
        self.assertFalse(self.service.send_voice(FRIEND, VOICE))

    def test_invalid_targets_do_not_inspect_file_or_enter_backend(self):
        for chat in ("微信名", "", "a b", "wxid_bad\0"):
            with self.subTest(chat=chat), self.assertRaises(ValueError):
                self.service.send_voice(chat, VOICE)
        self.inspect.assert_not_called()
        self.assertEqual(self.backend.sent, [])

    def test_file_inspection_failure_does_not_send(self):
        self.inspect.side_effect = ValueError("不支持的语音文件")
        with self.assertRaises(ValueError):
            self.service.send_voice(FRIEND, VOICE)
        self.assertEqual(self.backend.sent, [])

    def test_backend_without_voice_capability_is_reported_before_file_work(self):
        self.backend.send_voice = None
        with self.assertRaises(CapabilityError):
            self.context.send_voice(FRIEND, VOICE)
        self.inspect.assert_not_called()

    def test_cancelled_and_disconnected_services_reject_new_sends(self):
        for shutdown in (self.service.cancel_pending, self.service.close):
            shutdown()
            with self.assertRaises(RuntimeError):
                self.service.send_voice(FRIEND, VOICE)
        self.inspect.assert_not_called()
        self.assertEqual(self.backend.sent, [])

    def test_stale_reply_and_legacy_service_cannot_send_from_replacement_connection(self):
        legacy = LegacyService(self.service, "original")
        self.service.close()
        replacement = VoiceBackend("replacement")
        self.service.bind(replacement)
        for send in (lambda: legacy.send_voice(FRIEND, VOICE),
                     lambda: self.context.reply_voice(Message("", ROOM, session_id="original"), VOICE)):
            with self.subTest(send=send), self.assertRaises(RuntimeError):
                send()
        self.inspect.assert_not_called()
        self.assertEqual(replacement.sent, [])

    def test_explicit_empty_session_cannot_bypass_reply_or_legacy_isolation(self):
        for send in (lambda: LegacyService(self.service, "").send_voice(FRIEND, VOICE),
                     lambda: self.context.reply_voice(Message("", ROOM), VOICE),
                     lambda: self.context.send_voice(FRIEND, VOICE, expected_session="")):
            with self.subTest(send=send), self.assertRaises(RuntimeError):
                send()
        self.inspect.assert_not_called()
        self.assertEqual(self.backend.sent, [])

    def test_reconnect_during_initial_validation_cannot_switch_account(self):
        self._reconnect_during_inspection(1)

    def test_reconnect_during_final_validation_cannot_switch_account(self):
        self._reconnect_during_inspection(2)

    def _reconnect_during_inspection(self, change_on):
        replacement = VoiceBackend("replacement")
        calls = []

        def inspect(path):
            calls.append(path)
            if len(calls) == change_on:
                self.service.close()
                self.service.bind(replacement)
            return SimpleNamespace(path=VOICE, duration_ms=1000)

        self.inspect.side_effect = inspect
        with self.assertRaises(RuntimeError):
            self.service.send_voice(FRIEND, VOICE)
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(replacement.sent, [])

    def test_delivery_guard_can_cancel_before_backend_call(self):
        @contextmanager
        def guard():
            raise RuntimeError("语音回复已过期")
            yield

        with self.assertRaises(RuntimeError):
            self.context.reply_voice(Message("", FRIEND, session_id="original"), VOICE,
                                     delivery_guard=guard)
        self.assertEqual(self.backend.sent, [])

    def test_reconnect_inside_delivery_guard_is_checked_before_send(self):
        replacement = VoiceBackend("replacement")

        @contextmanager
        def guard():
            self.service.close()
            self.service.bind(replacement)
            yield

        with self.assertRaises(RuntimeError):
            self.service.send_voice(FRIEND, VOICE, delivery_guard=guard)
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(replacement.sent, [])

    def test_queued_voice_is_cancelled_when_connection_closes(self):
        self._queued_change(reconnect=False)

    def test_queued_voice_cannot_move_to_replacement_connection(self):
        self._queued_change(reconnect=True)

    def _queued_change(self, *, reconnect):
        gate = WaitingSendLock()
        self.service._send_lock = gate
        gate.lock.acquire()
        errors = []

        def send():
            try:
                self.service.send_voice(FRIEND, VOICE)
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        replacement = VoiceBackend("replacement")
        try:
            self.assertTrue(gate.waiting.wait(2))
            self.service.close()
            if reconnect:
                self.service.bind(replacement)
        finally:
            gate.lock.release()
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], RuntimeError)
        self.assertEqual(self.backend.sent, [])
        self.assertEqual(replacement.sent, [])

    def test_file_invalidated_while_queued_cannot_reach_backend(self):
        self.inspect.side_effect = [SimpleNamespace(path=VOICE, duration_ms=1000),
                                    ValueError("语音文件已被替换")]
        with self.assertRaises(ValueError):
            self.service.send_voice(FRIEND, VOICE)
        self.assertEqual(self.backend.sent, [])


class VoiceBackendAndConsoleTests(unittest.TestCase):
    def test_native_text_capabilities_include_friend_and_group_voice_send(self):
        for name in ("voice.send", "friend.voice.send", "group.voice.send"):
            self.assertIn(name, TEXT_CAPABILITIES)
            self.assertNotIn(name, CAPABILITIES)

    def test_native_text_backend_forwards_to_raw_voice_api(self):
        backend = object.__new__(NativeTextBackend)
        backend.raw = Mock()
        backend.raw.send_voice.return_value = False
        self.assertFalse(backend.send_voice(FRIEND, str(VOICE)))
        backend.raw.send_voice.assert_called_once_with(FRIEND, str(VOICE))

    def test_console_preserves_quoted_windows_path_and_calls_sdk(self):
        application = object.__new__(BotApplication)
        application.service = Mock()
        path = r"C:\example directory\语音.wav"
        application.execute('send-voice ' + FRIEND + ' "' + path + '"')
        application.service.send_voice.assert_called_once_with(FRIEND, path)

    def test_console_reports_rejected_native_send(self):
        application = object.__new__(BotApplication)
        application.service = Mock()
        application.service.send_voice.return_value = False
        with self.assertRaisesRegex(RuntimeError, "未接受语音发送"):
            application.execute('send-voice filehelper "C:/example.wav"')

    def test_console_rejects_incomplete_voice_command(self):
        application = object.__new__(BotApplication)
        application.service = Mock()
        with self.assertRaises(ValueError):
            application.execute("send-voice filehelper")
        application.service.send_voice.assert_not_called()


if __name__ == "__main__":
    unittest.main()
