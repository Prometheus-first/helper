"""发送文件校验/快照测试；不连接微信或网络。"""
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wxbot_framework.files import (
    FileInfo, MAX_FILE_BYTES, inspect_file, prepare_file, safe_file_name,
)

SCRATCH = Path(__file__).resolve().parent.parent / ".runtime/tests"


class FileTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="send-file-", dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self._cleanup)
        self.source = self.root / "中文 测试.txt"
        self.source.write_bytes(b"ordinary file\x00\xff")

    def _cleanup(self):
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        self.temporary.cleanup()

    def test_inspection_accepts_any_bytes_without_opening_file(self):
        result = inspect_file(self.source)
        self.assertEqual(result, FileInfo(self.source, self.source.stat().st_size, self.source.name))
        with self.assertRaises(FrozenInstanceError):
            result.size = 1
        self.assertFalse(hasattr(result, "__dict__"))

    def test_rejects_invalid_network_url_and_non_regular_paths(self):
        invalid = (None, b"filename", 1, "", " ", "bad\0path", "bad\ud800path",
                   "https://example.test/a.txt", "//server/share/a.txt", r"\\server\share\a.txt",
                   self.root, self.root / "missing.txt", str(self.source) + ":stream")
        for value in invalid:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                inspect_file(value)

    def test_zero_rejected_and_file_above_old_limit_allowed(self):
        empty = self.root / "empty.txt"
        empty.touch()
        with self.assertRaisesRegex(ValueError, "非空"):
            inspect_file(empty)
        with self.source.open("wb") as output:
            output.truncate(21 * 1024 * 1024)
        self.assertEqual(inspect_file(self.source).size, 21 * 1024 * 1024)
        with prepare_file(self.source, self.root / "snapshots") as snapshot:
            self.assertEqual(snapshot.size, 21 * 1024 * 1024)
            self.assertEqual(snapshot.path.stat().st_size, snapshot.size)

    def test_snapshot_retains_name_and_bytes_and_leaves_source_unchanged(self):
        original = self.source.read_bytes()
        root = self.root / "snapshots"
        root.mkdir()
        unrelated = root / "keep.txt"
        unrelated.write_bytes(b"keep")
        with prepare_file(self.source, root) as result:
            self.assertEqual((result.name, result.path.name, result.size),
                             (self.source.name, self.source.name, len(original)))
            self.assertEqual(result.path.read_bytes(), original)
            self.assertTrue(result.path.parent.is_relative_to(root))
            self.assertNotEqual(result.path, self.source)
            owned = result.path.parent
            self.source.write_bytes(b"changed after snapshot")
            self.assertEqual(result.path.read_bytes(), original)
        self.assertFalse(owned.exists())
        self.assertEqual(unrelated.read_bytes(), b"keep")
        self.assertEqual(list(root.iterdir()), [unrelated])
        self.assertEqual(self.source.read_bytes(), b"changed after snapshot")

    def test_context_failure_removes_only_owned_directory(self):
        root = self.root / "snapshots"
        with self.assertRaisesRegex(RuntimeError, "consumer"):
            with prepare_file(self.source, root) as result:
                owned = result.path.parent
                raise RuntimeError("consumer failed")
        self.assertFalse(owned.exists())
        self.assertEqual(list(root.iterdir()), [])
        self.assertTrue(self.source.exists())

    def test_send_keeps_long_original_filename_without_receiver_truncation(self):
        source = self.root / ("a" * 180 + ".txt")
        source.write_bytes(b"contents")
        self.assertEqual(inspect_file(source).name, source.name)
        with prepare_file(source, self.root / "snapshots") as result:
            self.assertEqual(result.path.name, source.name)
            self.assertEqual(result.name, source.name)
            self.assertEqual(result.path.read_bytes(), b"contents")

    def test_snapshot_write_failure_cleans_directory(self):
        root = self.root / "snapshots"
        original_open = Path.open
        def fail_open(path, *args, **kwargs):
            if path.parent.parent == root:
                raise OSError("simulated full disk")
            return original_open(path, *args, **kwargs)
        with patch.object(Path, "open", fail_open), self.assertRaises(OSError):
            with prepare_file(self.source, root):
                self.fail("snapshot must not be yielded")
        self.assertEqual(list(root.iterdir()), [])
        self.assertTrue(self.source.exists())

    def test_source_change_during_read_is_rejected(self):
        import os
        original_fstat = os.fstat
        calls = []
        def changed(descriptor):
            current = original_fstat(descriptor)
            calls.append(current)
            if len(calls) == 2:
                self.source.write_bytes(b"changed")
                return original_fstat(descriptor)
            return current
        with patch("wxbot_framework.files.os.fstat", side_effect=changed), self.assertRaises(ValueError):
            inspect_file(self.source)

    def test_utf8_path_limit_is_checked_before_read(self):
        # 模拟解析后的过长绝对路径，避免要求 Windows 开启长路径策略。
        long_path = self.root / ("文" * 400)
        with patch.object(Path, "resolve", return_value=long_path), self.assertRaisesRegex(ValueError, "1023"):
            inspect_file(self.source)

    def test_external_names_cannot_add_directories_or_windows_devices(self):
        expected = {"../../outside.txt": "outside.txt", r"C:\secret\a.txt": "a.txt",
                    "CON.txt": "_CON.txt", "NUL": "_NUL", "..": "file", " ": "file",
                    'bad:na*me?.txt': "bad_na_me_.txt", "line\nname.txt": "line_name.txt"}
        for name, result in expected.items():
            with self.subTest(name=name):
                self.assertEqual(safe_file_name(name), result)
        result = safe_file_name("文" * 300 + ".pdf")
        self.assertLessEqual(len(result.encode("utf-8")), 160)
        self.assertTrue(result.endswith(".pdf"))


if __name__ == "__main__":
    unittest.main()
