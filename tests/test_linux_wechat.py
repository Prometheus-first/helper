import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wxbot_framework.linux_wechat import discover_wechat


class LinuxWechatDiscoveryTests(unittest.TestCase):
    def test_main_process_fingerprint_and_subprocess_filter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "wechat"
            binary.write_bytes(b"fixture")
            proc = root / "proc"
            proc.mkdir()
            for pid, command in [("10", b"wechat\0"), ("11", b"wechat\0--type=renderer\0")]:
                (proc / pid).mkdir()
                (proc / pid / "exe").symlink_to(binary)
                (proc / pid / "cmdline").write_bytes(command)
            fingerprint = hashlib.sha256(b"fixture").hexdigest()
            with patch("wxbot_framework.linux_wechat.KNOWN_EXECUTABLES", {fingerprint: "fixture"}):
                rows = discover_wechat(proc)
            self.assertEqual([row["pid"] for row in rows], [10])
            self.assertEqual(rows[0]["version"], "fixture")
            self.assertIsNone(rows[0]["account"])
            self.assertFalse(rows[0]["bridge_supported"])

    def test_unknown_build_and_disappeared_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / "wechat"
            binary.write_bytes(b"unknown")
            proc = root / "proc"
            (proc / "1").mkdir(parents=True)
            (proc / "2").mkdir()
            (proc / "1" / "exe").symlink_to(binary)
            (proc / "1" / "cmdline").write_bytes(b"wechat\0")
            (proc / "2" / "exe").symlink_to(root / "gone")
            rows = discover_wechat(proc)
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]["version"])
            self.assertFalse(rows[0]["bridge_supported"])


if __name__ == "__main__":
    unittest.main()
