import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest

from tools.linux_text_roundtrip_test import RoundtripPlugin, TARGETS
from wxbot_framework.linux_sender import LinuxSendIndeterminate

ROOT = Path(__file__).resolve().parents[1]


class FakeSender:
    def __init__(self):
        self.allowed_targets = frozenset({"filehelper"})
        self.calls = []
        self.error = None
        self.closed = False

    def send_text(self, chat_id, text):
        if chat_id not in self.allowed_targets:
            raise RuntimeError("target not authorized")
        self.calls.append((chat_id, text))
        if self.error:
            raise self.error
        return True

    def close(self):
        self.closed = True


def event(group=False, text=None, author="wxid_friend_fixture", receiver="wxid_self_fixture"):
    body = TARGETS[group][0] if text is None else text
    return {"type": 1, "sequence": 1, "sender": "123456789@chatroom" if group else author,
            "receiver": receiver, "text": author + ":\n" + body if group else body}


class RoundtripPluginTests(unittest.TestCase):
    def setUp(self):
        self.sender = FakeSender()
        self.logs = []
        self.plugin = RoundtripPlugin(self.sender, report=self.logs.append)
        self.addCleanup(self.plugin.service.close)

    def test_private_and_group_use_original_reply_and_session(self):
        self.assertTrue(self.plugin.on_event(event()))
        self.assertTrue(self.plugin.on_event(event(True)))
        self.assertEqual(self.sender.calls, [("wxid_friend_fixture", "WX_LINUX_FRIEND_REPLY_01"),
                                             ("123456789@chatroom", "WX_LINUX_GROUP_REPLY_01")])
        self.assertEqual(self.plugin.succeeded, {False, True})
        self.assertNotIn("wxid_friend_fixture", "".join(self.logs))
        state = self.plugin.status()
        self.assertNotIn("wxid_", json.dumps(state))
        self.assertTrue(state["friend_callback_success"] and state["group_callback_success"])

    def test_duplicate_or_unrelated_message_is_not_replied(self):
        self.assertFalse(self.plugin.on_event(event(text="ordinary chat")))
        self.assertIsNone(self.plugin.adapter)
        self.plugin.on_event(event())
        for _ in range(3):
            self.assertFalse(self.plugin.on_event(event()))
        self.assertEqual(len(self.sender.calls), 1)

    def test_markers_must_match_message_kind_and_entire_text(self):
        for msg in (event(text=TARGETS[True][0]), event(True, text=TARGETS[False][0]),
                    event(text="prefix " + TARGETS[False][0])):
            self.assertFalse(self.plugin.on_event(msg))
        self.assertEqual(self.sender.calls, [])
        self.assertEqual(self.sender.allowed_targets, {"filehelper"})

    def test_failure_is_not_retried_and_does_not_report_success(self):
        self.sender.error = LinuxSendIndeterminate("fixture timeout")
        self.assertTrue(self.plugin.on_event(event()))
        self.assertFalse(self.plugin.on_event(event()))
        self.assertEqual(len(self.sender.calls), 1)
        self.assertEqual(self.plugin.succeeded, set())
        self.assertEqual(self.plugin.status()["error_types"], {"friend": "LinuxSendIndeterminate"})

    def test_self_group_sync_and_another_account_are_ignored(self):
        self.assertFalse(self.plugin.on_event(event(True, author="wxid_self_fixture")))
        self.assertIsNone(self.plugin.adapter)
        self.plugin.on_event(event())
        with self.assertRaises(RuntimeError):
            self.plugin.on_event(event(True, receiver="wxid_other_fixture"))
        self.assertEqual(len(self.sender.calls), 1)

    def test_filehelper_and_stale_session_are_not_replied(self):
        self.assertFalse(self.plugin.on_event({"type": 1, "sequence": 1, "sender": "filehelper",
            "receiver": "wxid_self_fixture", "text": TARGETS[False][0]}))
        self.assertEqual(self.sender.calls, [])
        self.plugin.service.cancel_pending()
        self.plugin.on_event(event())
        self.assertEqual(self.sender.calls, [])
        self.assertFalse(self.plugin.status()["friend_callback_success"])


@unittest.skipUnless(os.name == "posix" and hasattr(socket, "SO_PEERCRED") and shutil.which("gcc"), "requires Linux/GCC")
class RoundtripNativeTests(unittest.TestCase):
    def test_cli_combines_native_receive_scheduler_plugin_reply_and_shutdown(self):
        with tempfile.TemporaryDirectory(prefix="helper-roundtrip-fixture-") as temporary:
            work = Path(temporary)
            obj, fixture, recv, send = [work / n for n in ("sdk.o", "wechat-fixture", "recv.so", "send.so")]
            subprocess.run(["gcc", "-O2", "-Dmain=send_fixture_main", "-c",
                            str(ROOT / "tests/fixtures/linux_send_fixture.c"), "-o", str(obj)], check=True)
            subprocess.run(["gcc", "-O2", "-rdynamic", "-pthread", "-DHELPER_FIXTURE_ROUNDTRIP",
                            str(ROOT / "tests/fixtures/linux_bridge_cli_fixture.c"), str(obj), "-o", str(fixture)], check=True)
            for source, flag, target in (("text_bridge.c", "-DHELPER_FIXTURE_TEST", recv),
                                         ("send_bridge.c", "-DSDK_FIXTURE", send)):
                subprocess.run(["gcc", "-O2", "-fPIC", "-shared", "-pthread", flag,
                                str(ROOT / "native/linux" / source), "-ldl", "-o", str(target)], check=True)
            state = work / "state.json"
            digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
            script = ("import sys;from tools import linux_text_roundtrip_test as m;"
                      "m.LINUX_WECHAT_SHA256=sys.argv[1];"
                      "sys.argv=['roundtrip','--wechat',sys.argv[2],'--receive-bridge',sys.argv[3],"
                      "'--send-bridge',sys.argv[4],'--timeout','5','--state-file',sys.argv[5]];m.main()")
            result = subprocess.run([sys.executable, "-B", "-c", script, digest, str(fixture), str(recv), str(send), str(state)],
                                    cwd=ROOT, capture_output=True, text=True, timeout=12)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            outcome = json.loads(state.read_text())
            self.assertTrue(outcome["friend_callback_success"])
            self.assertTrue(outcome["group_callback_success"])
            self.assertTrue(outcome["stopped"])
            self.assertNotIn("wxid_", state.read_text())
            self.assertIn("私聊原生回复成功", result.stdout)
            self.assertIn("群聊原生回复成功", result.stdout)
            self.assertIn("收发测试已停用", result.stdout)


if __name__ == "__main__":
    unittest.main()
