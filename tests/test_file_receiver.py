"""文件接收队列的失败、账号隔离和清理测试；下载器全为本地伪实现。"""
from pathlib import Path
import hashlib
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch

from wxbot_framework.file_metadata import FileStore
from wxbot_framework.file_receiver import FileReceiver
from wxbot_framework.files import MAX_FILE_BYTES

SCRATCH = Path(__file__).resolve().parent.parent / ".runtime/tests"
ACCOUNT = "wxid_file_receiver_robot"
BODY = b"ordinary file\x00\xff"
MD5 = hashlib.md5(BODY).hexdigest()


def file_event(sequence=1, *, size=len(BODY), md5=MD5, title="document.txt"):
    length = "" if size is None else f"<totallen>{size}</totallen>"
    checksum = "" if not md5 else f"<md5>{md5}</md5>"
    xml = (f"<msg><appmsg><title>{title}</title><type>6</type><appattach>"
           f"{length}{checksum}<aeskey>private-aes</aeskey><cdnattachurl>private-url</cdnattachurl>"
           "<attachid>private-attach</attachid></appattach></appmsg></msg>")
    return {"wx_type": 49, "raw_content": xml, "content": xml, "sequence": sequence,
            "msg": xml,
            "session_id": "original", "chat_id": "wxid_friend", "sender_id": "wxid_friend",
            "file_path": "untrusted-old-path"}


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="file-receiver-", dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.store = FileStore(self.root / "files", ACCOUNT)
        self.current = threading.Event()
        self.current.set()
        self.delivered = queue.Queue()
        self.receivers, self.releases = [], []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for release in self.releases:
            release.set()
        for receiver in self.receivers:
            receiver.close(timeout=1)
            self.assertFalse(receiver.is_alive)
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        self.temporary.cleanup()

    def receiver(self, downloader, **kwargs):
        result = FileReceiver(self.store, downloader, self.delivered.put, self.current.is_set, **kwargs)
        self.receivers.append(result)
        return result

    def wait_event(self):
        return self.delivered.get(timeout=2)

    @staticmethod
    def download(metadata, destination):
        destination.write_bytes(BODY)
        return True

    def blocked_downloader(self):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        calls = []
        def download(metadata, destination):
            calls.append(destination)
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test downloader timed out")
            destination.write_bytes(BODY)
            return True
        return download, entered, release, calls

    def test_success_verifies_bytes_strips_xml_and_keeps_input_unchanged(self):
        receiver = self.receiver(self.download)
        source = file_event(title="../../report.txt")
        original = dict(source)
        self.assertTrue(receiver.submit(source))
        event = self.wait_event()
        path = Path(event["file_path"])
        self.assertEqual(event["file_status"], "ready")
        self.assertEqual((event["file_name"], event["file_size"], event["file_error"]),
                         ("report.txt", len(BODY), ""))
        self.assertEqual(path.read_bytes(), BODY)
        self.assertEqual(path.parent, self.store.directory)
        self.assertNotIn("raw_content", event)
        self.assertEqual(event["content"], "")
        self.assertEqual(event["msg"], "")
        for secret in ("private-aes", "private-url", "private-attach"):
            self.assertNotIn(secret, str(event))
        self.assertEqual(source, original)
        receiver.close()
        self.assertEqual(list(self.store.directory.iterdir()), [path])

    def test_missing_size_and_md5_are_unknown_but_valid_download_is_retained(self):
        receiver = self.receiver(self.download)
        self.assertTrue(receiver.submit(file_event(size=None, md5="")))
        event = self.wait_event()
        self.assertEqual(event["file_status"], "ready")
        self.assertEqual(event["file_size"], len(BODY))
        self.assertIsNone(event["file_metadata"]["size"])
        self.assertEqual(event["file_metadata"]["md5"], "")

    def test_download_larger_than_old_limit_is_verified_and_saved(self):
        size = 21 * 1024 * 1024 + 7
        chunk = b"sample" * 1024
        digest = hashlib.md5()
        def downloader(metadata, destination):
            remaining = size
            with destination.open("wb") as output:
                while remaining:
                    part = chunk[:min(remaining, len(chunk))]
                    output.write(part)
                    digest.update(part)
                    remaining -= len(part)
            return True
        expected = hashlib.md5()
        remaining = size
        while remaining:
            part = chunk[:min(remaining, len(chunk))]
            expected.update(part)
            remaining -= len(part)
        receiver = self.receiver(downloader)
        self.assertTrue(receiver.submit(file_event(size=size, md5=expected.hexdigest())))
        event = self.delivered.get(timeout=15)
        self.assertEqual(event['file_status'], 'ready')
        self.assertEqual(Path(event['file_path']).stat().st_size, size)
        self.assertEqual(digest.hexdigest(), expected.hexdigest())

    def test_size_or_md5_mismatch_is_visible_and_never_committed(self):
        receiver = self.receiver(self.download)
        for event in (file_event(size=len(BODY) + 1), file_event(md5="a" * 32)):
            with self.subTest(md5=event["raw_content"].find("aaaa")):
                self.assertTrue(receiver.submit(event))
                result = self.wait_event()
                self.assertEqual(result["file_status"], "failed")
                self.assertEqual(result["file_path"], "")
        receiver.close()
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_invalid_metadata_and_oversize_fail_without_downloading(self):
        calls = []
        receiver = self.receiver(lambda *args: calls.append(args))
        invalid = file_event()
        invalid["raw_content"] = "not XML containing private-url"
        invalid["content"] = invalid["raw_content"]
        for event in (invalid, file_event(size=MAX_FILE_BYTES + 1), file_event(size=0)):
            self.assertFalse(receiver.submit(event))
            result = self.wait_event()
            self.assertEqual(result["file_status"], "failed")
            self.assertEqual(result["file_path"], "")
            self.assertNotIn("private-url", str(result))
        self.assertEqual(calls, [])
        self.assertFalse(self.store.root.exists())

    def test_other_appmsg_is_ignored_without_failure_or_download(self):
        calls = []
        receiver = self.receiver(lambda *args: calls.append(args))
        event = file_event()
        event["raw_content"] = event["raw_content"].replace("<type>6</type>", "<type>5</type>")
        self.assertFalse(receiver.submit(event))
        self.assertTrue(self.delivered.empty())
        self.assertEqual(calls, [])

    def test_full_queue_reports_failure_and_close_cancels_pending(self):
        download, entered, release, calls = self.blocked_downloader()
        receiver = self.receiver(download, queue_size=1)
        self.assertTrue(receiver.submit(file_event(1)))
        self.assertTrue(entered.wait(1))
        self.assertTrue(receiver.submit(file_event(2)))
        self.assertFalse(receiver.submit(file_event(3)))
        event = self.wait_event()
        self.assertEqual((event["sequence"], event["file_status"]), (3, "failed"))
        self.assertIn("队列", event["file_error"])
        receiver.close(timeout=0)
        self.assertTrue(receiver.is_alive)
        self.assertFalse(receiver.submit(file_event(4)))
        release.set()
        receiver.close(timeout=1)
        self.assertFalse(receiver.is_alive)
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.delivered.empty())
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_session_change_drops_inflight_and_queued_file_without_delivering(self):
        download, entered, release, calls = self.blocked_downloader()
        receiver = self.receiver(download)
        self.assertTrue(receiver.submit(file_event(1)))
        self.assertTrue(entered.wait(1))
        self.assertTrue(receiver.submit(file_event(2)))
        self.current.clear()
        release.set()
        receiver.close(timeout=1)
        self.assertTrue(self.delivered.empty())
        self.assertEqual(list(self.store.directory.iterdir()), [])
        self.assertEqual(len(calls), 1)

    def test_download_exception_is_sanitized_and_worker_survives(self):
        calls = []
        def download(metadata, destination):
            calls.append(destination)
            if len(calls) == 1:
                destination.write_bytes(b"partial")
                raise RuntimeError("private-url private-aes private-attach")
            return self.download(metadata, destination)
        receiver = self.receiver(download)
        self.assertTrue(receiver.submit(file_event(1)))
        first = self.wait_event()
        self.assertEqual(first["file_status"], "failed")
        self.assertNotIn("private-", str(first))
        self.assertTrue(receiver.submit(file_event(2)))
        second = self.wait_event()
        self.assertEqual(second["file_status"], "ready")
        receiver.close()
        self.assertEqual(list(self.store.directory.iterdir()), [Path(second["file_path"])])

    def test_false_missing_empty_directory_and_oversize_downloads_fail(self):
        def false(metadata, destination):
            destination.write_bytes(BODY)
            return False
        def missing(metadata, destination):
            return True
        def empty(metadata, destination):
            destination.touch()
            return True
        def directory(metadata, destination):
            destination.mkdir()
            return True
        def oversized(metadata, destination):
            with destination.open("wb") as output:
                output.truncate(21 * 1024 * 1024)
            return True
        for downloader in (false, missing, empty, directory, oversized):
            with self.subTest(name=downloader.__name__):
                receiver = self.receiver(downloader)
                self.assertTrue(receiver.submit(file_event()))
                self.assertEqual(self.wait_event()["file_status"], "failed")
                receiver.close()
                self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_failed_delivery_removes_committed_file(self):
        called = threading.Event()
        def deliver(event):
            called.set()
            raise RuntimeError("private callback error")
        receiver = FileReceiver(self.store, self.download, deliver, self.current.is_set)
        self.receivers.append(receiver)
        self.assertTrue(receiver.submit(file_event()))
        self.assertTrue(called.wait(1))
        receiver.close()
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_account_directory_redirection_prevents_downloader(self):
        calls = []
        receiver = self.receiver(lambda *args: calls.append(args))
        original = Path.resolve
        outside = self.root / "outside"
        def redirected(path, *args, **kwargs):
            return outside if path == self.store.directory else original(path, *args, **kwargs)
        with patch.object(Path, "resolve", redirected):
            self.assertTrue(receiver.submit(file_event()))
            self.assertEqual(self.wait_event()["file_status"], "failed")
        self.assertEqual(calls, [])
        self.assertFalse(outside.exists())
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_path_validation_failure_cleans_created_download_workspace(self):
        calls = []
        receiver = self.receiver(lambda *args: calls.append(args))
        with patch("wxbot_framework.file_receiver._check_local_path", side_effect=ValueError("path too long")):
            self.assertTrue(receiver.submit(file_event()))
            self.assertEqual(self.wait_event()["file_status"], "failed")
        self.assertEqual(calls, [])
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_account_change_after_save_removes_undelivered_file(self):
        original_save = self.store.save_download
        def save(*args, **kwargs):
            result = original_save(*args, **kwargs)
            self.current.clear()
            return result
        receiver = self.receiver(self.download)
        with patch.object(self.store, "save_download", side_effect=save):
            self.assertTrue(receiver.submit(file_event()))
            receiver._queue.join()
        self.assertTrue(self.delivered.empty())
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_invalid_callbacks_capacity_and_close_timeout_are_rejected(self):
        for value in (0, -1, True, 1.5, 1025):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.receiver(self.download, queue_size=value)
        with self.assertRaises(ValueError):
            FileReceiver(self.store, None, self.delivered.put, self.current.is_set)
        receiver = self.receiver(self.download)
        for value in (-1, None, True, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                receiver.close(value)
        with self.assertRaises(ValueError):
            receiver.submit(None)


if __name__ == "__main__":
    unittest.main()
