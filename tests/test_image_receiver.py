"""图片接收异步队列测试；所有下载器均为本地伪实现。"""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch

from wxbot_framework.image_metadata import ImageStore
from wxbot_framework.image_receiver import ImageReceiver
from wxbot_framework.images import MAX_IMAGE_BYTES


ACCOUNT = "wxid_image_receiver_robot"
MD5 = "0123456789abcdef0123456789abcdef"
XML = (f'<msg><img md5="{MD5}" width="640" height="480" length="100" '
       'aeskey="private-aes-key" cdnthumburl="https://private.example.test/secret" '
       'filekey="private-file-key" /></msg>')
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32
SCRATCH = Path(__file__).resolve().parent.parent / ".runtime/tests"


def image_event(sequence=1):
    return {"wx_type": 3, "raw_content": XML, "sequence": sequence, "session_id": "original",
            "chat_id": "wxid_friend", "sender_id": "wxid_friend", "image_path": "untrusted-old-path"}


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="image-receiver-", dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.store = ImageStore(self.root / "images", ACCOUNT)
        self.current = threading.Event()
        self.current.set()
        self.delivered = queue.Queue()
        self.receivers = []
        self.releases = []
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
        receiver = ImageReceiver(self.store, downloader, self.delivered.put, self.current.is_set, **kwargs)
        self.receivers.append(receiver)
        return receiver

    def wait_event(self):
        return self.delivered.get(timeout=2)

    def assert_no_work_files(self):
        if self.store.directory.exists():
            for path in self.store.directory.iterdir():
                self.assertTrue(path.is_file())
                self.assertFalse(path.name.startswith("."))

    def blocked_downloader(self, *, result=True):
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        calls = []
        def download(metadata, destination):
            calls.append((metadata, destination))
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test downloader timed out")
            destination.write_bytes(PNG)
            return result
        return download, entered, release, calls

    def test_submit_returns_while_download_is_blocked_and_keeps_input_snapshot(self):
        download, entered, release, calls = self.blocked_downloader()
        receiver = self.receiver(download)
        original = image_event()
        submitted, outcome = threading.Event(), []
        def submit():
            outcome.append(receiver.submit(original))
            submitted.set()
        thread = threading.Thread(target=submit, daemon=True)
        thread.start()
        try:
            self.assertTrue(submitted.wait(0.5))
            self.assertTrue(entered.wait(1))
            self.assertEqual(outcome, [True])
            self.assertTrue(self.delivered.empty())
            original.update(raw_content="changed", chat_id="changed")
        finally:
            release.set()
            thread.join(1)
        event = self.wait_event()
        self.assertEqual((event["raw_content"], event["chat_id"]), (XML, "wxid_friend"))
        self.assertEqual(len(calls), 1)

    def test_success_uses_unique_absent_target_then_saves_public_metadata(self):
        targets, worker_threads = [], []
        def download(metadata, destination):
            self.assertEqual((metadata.md5, metadata.aeskey), (MD5, "private-aes-key"))
            with self.assertRaises(FrozenInstanceError):
                metadata.width = 1
            self.assertFalse(destination.exists())
            self.assertTrue(destination.is_absolute())
            self.assertEqual(destination.parent.parent, self.store.directory)
            self.assertTrue(destination.parent.name.startswith(".receive-"))
            targets.append(destination)
            worker_threads.append(threading.get_ident())
            destination.write_bytes(PNG)
            return True
        receiver = self.receiver(download)
        for sequence in (4, 4):
            self.assertTrue(receiver.submit(image_event(sequence)))
            event = self.wait_event()
            self.assertEqual((event["image_status"], event["image_error"]), ("ready", ""))
            saved = Path(event["image_path"])
            self.assertEqual(saved.read_bytes(), PNG)
            self.assertEqual(saved.parent, self.store.directory)
            self.assertEqual(event["image_metadata"]["md5"], MD5)
            self.assertEqual(event["image_metadata"]["width"], 640)
            self.assertEqual(event["image_metadata"]["height"], 480)
            public = repr(event["image_metadata"])
            for secret in ("aeskey", "filekey", "cdnthumburl", "private-", "https://", "<msg"):
                self.assertNotIn(secret, public)
        self.assertNotEqual(targets[0], targets[1])
        self.assertEqual(len(set(worker_threads)), 1)
        self.assertNotEqual(worker_threads[0], threading.get_ident())
        self.assertTrue(all(not target.parent.exists() for target in targets))
        self.assert_no_work_files()

    def test_original_reference_quality_and_failure_are_visible(self):
        xml = ('<msg><img aeskey="fixture-key" cdnbigimgurl="original-reference" '
               'cdnmidimgurl="standard-reference" /></msg>')
        def download(_metadata, destination):
            destination.write_bytes(PNG)
            return True
        receiver = self.receiver(download)
        receiver.submit(dict(image_event(), raw_content=xml))
        event = self.wait_event()
        self.assertEqual((event['image_status'], event['image_quality']), ('ready', 'original'))
        failing = self.receiver(lambda _metadata, _destination: False)
        failing.submit(dict(image_event(2), raw_content=xml))
        event = self.wait_event()
        self.assertEqual((event['image_status'], event['image_quality']), ('failed', 'original'))
        self.assertIn('未自动切换普通图', event['image_error'])

    def test_false_downloader_result_is_observable_failure_and_cleans_partial_file(self):
        def download(metadata, destination):
            destination.write_bytes(b"half-written-data")
            return False
        receiver = self.receiver(download)
        self.assertTrue(receiver.submit(image_event()))
        event = self.wait_event()
        self.assertEqual((event["image_status"], event["image_path"]), ("failed", ""))
        self.assertTrue(event["image_error"])
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_private_exception_is_replaced_with_constant_error_and_worker_continues(self):
        calls = []
        def download(metadata, destination):
            calls.append(True)
            if len(calls) == 1:
                destination.write_bytes(b"partial")
                raise RuntimeError(XML + " private-aes-key https://private.example.test/secret")
            destination.write_bytes(PNG)
            return True
        receiver = self.receiver(download)
        self.assertTrue(receiver.submit(image_event()))
        failure = self.wait_event()
        self.assertEqual((failure["image_status"], failure["image_path"]), ("failed", ""))
        for secret in (XML, "private-", "https://", "aeskey", "<msg"):
            self.assertNotIn(secret, failure["image_error"])
        self.assertTrue(receiver.submit(image_event(2)))
        self.assertEqual(self.wait_event()["image_status"], "ready")
        self.assertTrue(receiver.is_alive)
        self.assert_no_work_files()

    def test_bad_xml_fails_before_downloader_and_never_reuses_old_path(self):
        called = []
        receiver = self.receiver(lambda *args: called.append(args))
        event = image_event()
        event["raw_content"] = '<!DOCTYPE msg [<!ENTITY x "private-secret">]><msg><img /></msg>'
        self.assertFalse(receiver.submit(event))
        failure = self.wait_event()
        self.assertEqual((failure["image_status"], failure["image_path"], failure["image_metadata"]),
                         ("failed", "", {}))
        self.assertNotIn("private-secret", failure["image_error"])
        self.assertEqual(called, [])
        self.assertFalse(self.store.root.exists())

    def test_missing_result_directory_result_empty_and_encrypted_results_fail(self):
        results = ("missing", "directory", "empty", "encrypted", "too-large", "truthy-not-bool")
        for result in results:
            with self.subTest(result=result):
                def download(metadata, destination):
                    if result == "directory":
                        destination.mkdir()
                    elif result == "empty":
                        destination.touch()
                    elif result == "encrypted":
                        destination.write_bytes(b"encrypted-dat-content")
                    elif result == "too-large":
                        with destination.open("wb") as output:
                            output.write(PNG)
                            output.truncate(MAX_IMAGE_BYTES + 1)
                    elif result == "truthy-not-bool":
                        destination.write_bytes(PNG)
                        return "success"
                    return True
                receiver = self.receiver(download)
                self.assertTrue(receiver.submit(image_event()))
                self.assertEqual(self.wait_event()["image_status"], "failed")
                receiver.close()
                self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_save_failure_is_observable_and_cleans_download_directory(self):
        def download(metadata, destination):
            destination.write_bytes(PNG)
            return True
        receiver = self.receiver(download)
        with patch.object(self.store, "save", side_effect=OSError("private-key https://private.test/")):
            self.assertTrue(receiver.submit(image_event()))
            failure = self.wait_event()
        self.assertEqual((failure["image_status"], failure["image_path"]), ("failed", ""))
        self.assertNotIn("private", failure["image_error"])
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_session_expiring_during_download_drops_output_and_queued_events(self):
        download, entered, release, calls = self.blocked_downloader()
        receiver = self.receiver(download)
        self.assertTrue(receiver.submit(image_event(1)))
        self.assertTrue(entered.wait(1))
        self.assertTrue(receiver.submit(image_event(2)))
        self.current.clear()
        release.set()
        receiver.close(timeout=1)
        self.assertFalse(receiver.is_alive)
        self.assertTrue(self.delivered.empty())
        self.assertEqual(len(calls), 1)
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_already_stale_or_closed_session_rejects_submit_without_events(self):
        calls = []
        receiver = self.receiver(lambda *args: calls.append(args))
        self.current.clear()
        self.assertFalse(receiver.submit(image_event()))
        self.current.set()
        receiver.close()
        self.assertFalse(receiver.submit(image_event()))
        self.assertEqual(calls, [])
        self.assertTrue(self.delivered.empty())
        self.assertFalse(self.store.root.exists())

    def test_session_expiring_during_save_removes_completed_file_without_delivering(self):
        def download(metadata, destination):
            destination.write_bytes(PNG)
            return True
        saved, finished = [], threading.Event()
        original_save = self.store.save
        def save(*args, **kwargs):
            path = original_save(*args, **kwargs)
            saved.append(path)
            self.current.clear()
            finished.set()
            return path
        receiver = self.receiver(download)
        with patch.object(self.store, "save", side_effect=save):
            self.assertTrue(receiver.submit(image_event()))
            self.assertTrue(finished.wait(1))
            receiver.close(timeout=1)
        self.assertEqual(len(saved), 1)
        self.assertFalse(saved[0].exists())
        self.assertTrue(self.delivered.empty())
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_full_queue_immediately_reports_failure_and_close_is_bounded(self):
        download, entered, release, calls = self.blocked_downloader()
        receiver = self.receiver(download, queue_size=1)
        self.assertTrue(receiver.submit(image_event(1)))
        self.assertTrue(entered.wait(1))
        self.assertTrue(receiver.submit(image_event(2)))
        self.assertFalse(receiver.submit(image_event(3)))
        failure = self.wait_event()
        self.assertEqual((failure["sequence"], failure["image_status"], failure["image_path"]), (3, "failed", ""))
        self.assertIn("队列", failure["image_error"])
        closed = threading.Event()
        thread = threading.Thread(target=lambda: (receiver.close(timeout=0.01), closed.set()), daemon=True)
        thread.start()
        self.assertTrue(closed.wait(0.5))
        thread.join(1)
        self.assertTrue(receiver.is_alive)
        release.set()
        receiver.close(timeout=1)
        self.assertFalse(receiver.is_alive)
        self.assertEqual(len(calls), 1)
        self.assertTrue(self.delivered.empty())
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_deliver_failure_does_not_kill_worker_or_leave_orphan_image(self):
        callback_called = threading.Event()
        def download(metadata, destination):
            destination.write_bytes(PNG)
            return True
        def failing_delivery(event):
            callback_called.set()
            raise RuntimeError("private delivery failure")
        receiver = ImageReceiver(self.store, download, failing_delivery, self.current.is_set)
        self.receivers.append(receiver)
        self.assertTrue(receiver.submit(image_event()))
        self.assertTrue(callback_called.wait(1))
        receiver.close(timeout=1)
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_directory_redirection_is_rejected_before_downloader(self):
        called = []
        receiver = self.receiver(lambda *args: called.append(args))
        original_resolve = Path.resolve
        outside = self.root / "outside"
        def redirected(path, *args, **kwargs):
            return outside if path == self.store.directory else original_resolve(path, *args, **kwargs)
        with patch.object(Path, "resolve", redirected):
            self.assertTrue(receiver.submit(image_event()))
            self.assertEqual(self.wait_event()["image_status"], "failed")
        self.assertEqual(called, [])
        self.assertFalse(outside.exists())
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_bad_capacity_callbacks_and_close_timeout_are_rejected(self):
        for capacity in (0, -1, True, 1.5, 1025):
            with self.subTest(capacity=capacity), self.assertRaises(ValueError):
                self.receiver(lambda *args: True, queue_size=capacity)
        with self.assertRaises(ValueError):
            ImageReceiver(self.store, None, self.delivered.put, self.current.is_set)
        receiver = self.receiver(lambda *args: True)
        for timeout in (-1, None, True, float("inf"), float("nan")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                receiver.close(timeout)
        with self.assertRaises(ValueError):
            receiver.submit(None)


if __name__ == "__main__":
    unittest.main()
