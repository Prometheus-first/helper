"""文本节奏、统一发送入口及等待期间的退出/重连验证；不连接微信。"""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import Mock, patch

from wxbot_framework.pacing import TextSendDelay
from wxbot_framework.sdk import CapabilityError, LegacyService, Message, PluginContext, ServiceFacade


class MemoryBackend:
    def __init__(self, session="original"):
        self.session_id = session
        self.sent = []
        self.stopped = False

    def send_text(self, chat, text):
        self.sent.append((chat, text, time.monotonic()))
        return True

    def stop(self):
        self.stopped = True
        return {"dll_unloaded": True}


class RecordingCancel:
    def __init__(self):
        self.waits = []
        self.cancelled = False

    def wait(self, seconds):
        self.waits.append(seconds)
        return self.cancelled

    def is_set(self):
        return self.cancelled

    def set(self):
        self.cancelled = True


class TextPacingTests(unittest.TestCase):
    def test_character_groups_include_chinese_symbols_and_short_text(self):
        pacing = TextSendDelay.from_config()
        with patch("wxbot_framework.pacing.random.uniform", return_value=0.2):
            self.assertAlmostEqual(pacing.seconds_for("你好🌸 abc！"), 0.2)
            self.assertAlmostEqual(pacing.seconds_for("字" * 10), 0.2)
            self.assertAlmostEqual(pacing.seconds_for("字" * 11), 0.4)
            self.assertAlmostEqual(pacing.seconds_for("字" * 100), 2.0)
        self.assertEqual(TextSendDelay.from_config({"enabled": False}).seconds_for("你好"), 0)

    def test_configuration_rejects_invalid_random_ranges_and_group_size(self):
        for options in ([], {"enabled": 1}, {"characters_per_group": 0},
                        {"characters_per_group": True}, {"min_seconds": -1},
                        {"max_seconds": float("inf")}, {"max_seconds": float("nan")},
                        {"min_seconds": True}, {"min_seconds": 0.4, "max_seconds": 0.3}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                TextSendDelay.from_config(options)

    def test_all_plugin_text_apis_wait_and_other_payloads_do_not(self):
        backend = MemoryBackend()
        service = ServiceFacade()
        service.bind(backend)
        cancel = RecordingCancel()
        service._cancel = cancel
        ctx = PluginContext("test", {}, service, {"text.send"})
        legacy = LegacyService(service, backend.session_id)
        message = Message(text="test", chat_id="wxid_friend", session_id=backend.session_id)
        with patch("wxbot_framework.pacing.random.uniform", return_value=0.2):
            ctx.reply(message, "你好")
            ctx.send_text("12345@chatroom", "群聊")
            legacy.send_text("filehelper", "旧插件")
            legacy.send_payload({"type": 11237, "data": {"to_wxid": "filehelper", "content": "文本"}})
            with self.assertRaises(CapabilityError):
                legacy.send_payload({"type": 99999})
        self.assertEqual(cancel.waits, [0.2] * 4)
        self.assertEqual(len(backend.sent), 4)
        service.close()

    def test_invalid_text_is_rejected_before_wait_or_native_send(self):
        backend = MemoryBackend()
        service = ServiceFacade()
        service.bind(backend)
        cancel = RecordingCancel()
        service._cancel = cancel
        for text in (None, "", "含\0字符"):
            with self.subTest(text_type=type(text)), self.assertRaises(ValueError):
                service.send_text("filehelper", text)
        self.assertEqual(cancel.waits, [])
        self.assertEqual(backend.sent, [])
        service.close()

    def test_two_concurrent_plugins_each_wait_between_native_sends(self):
        backend = MemoryBackend()
        service = ServiceFacade(text_delay={"min_seconds": 0.08, "max_seconds": 0.08})
        service.bind(backend)
        threads = [threading.Thread(target=service.send_text, args=("filehelper", str(i))) for i in range(2)]
        started = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(backend.sent), 2)
        self.assertGreaterEqual(backend.sent[0][2] - started, 0.07)
        self.assertGreaterEqual(backend.sent[1][2] - backend.sent[0][2], 0.07)
        service.close()

    def test_close_interrupts_wait_without_blocking_connection_queries(self):
        backend = MemoryBackend()
        service = ServiceFacade()
        service.bind(backend)
        waiting, errors = threading.Event(), []

        def delay(_text):
            waiting.set()
            return 30

        service.text_delay = Mock(seconds_for=delay)
        thread = self._start_send(service, errors)
        self.assertTrue(waiting.wait(1))
        self.assertTrue(service.connected)
        service.close()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(backend.sent, [])
        self.assertTrue(backend.stopped)

    def test_pending_send_cannot_move_to_new_connection_even_without_session_argument(self):
        original, replacement = MemoryBackend(), MemoryBackend("replacement")
        service = ServiceFacade()
        service.bind(original)
        waiting, errors = threading.Event(), []

        def delay(_text):
            waiting.set()
            return 30

        service.text_delay = Mock(seconds_for=delay)
        thread = self._start_send(service, errors)
        self.assertTrue(waiting.wait(1))
        service.close()
        service.bind(replacement)
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(original.sent, [])
        self.assertEqual(replacement.sent, [])
        service.text_delay = TextSendDelay.from_config({"enabled": False})
        service.send_text("filehelper", "新连接")
        self.assertEqual(replacement.sent[0][:2], ("filehelper", "新连接"))
        service.close()

    def test_shutdown_cancellation_rejects_new_sends_before_backend_cleanup(self):
        backend = MemoryBackend()
        service = ServiceFacade(text_delay={"enabled": False})
        service.bind(backend)
        service.cancel_pending()
        with self.assertRaises(RuntimeError):
            service.send_text("filehelper", "退出期间不再发消息")
        self.assertEqual(backend.sent, [])
        self.assertFalse(backend.stopped)
        service.close()
        self.assertTrue(backend.stopped)

    @staticmethod
    def _start_send(service, errors):
        def send():
            try:
                service.send_text("filehelper", "等待发送")
            except RuntimeError as error:
                errors.append(error)
        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        return thread


if __name__ == "__main__":
    unittest.main()
