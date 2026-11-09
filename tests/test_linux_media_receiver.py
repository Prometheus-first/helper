"""合成缓存验证 Linux 防撤回接收，不启动微信或发送消息。"""
from dataclasses import replace
import hashlib
import base64
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import patch

from wxbot_framework.linux_media_receiver import LinuxMediaReceiver, parse_linux_media_payload
from wxbot_framework.sdk import Message

ACCOUNT = 'wxid_media_self'
PEER = 'wxid_media_peer'
ROOM = '123@chatroom'
SESSION = 'media-session'


def file_xml(body, name='example.txt', kind=6):
    return (f'<msg><appmsg><type>{kind}</type><title>{name}</title>'
        f'<md5>{hashlib.md5(body).hexdigest()}</md5><appattach><totallen>{len(body)}</totallen>'
        '<aeskey>SECRET_FIXTURE_KEY</aeskey><cdnattachurl>SECRET_FIXTURE_URL</cdnattachurl>'
        '</appattach></appmsg></msg>')


class PayloadTests(unittest.TestCase):
    def test_payload_removes_download_secrets_and_preserves_type74(self):
        raw = PEER + ':\n' + file_xml(b'fixture', kind=74)
        payload = parse_linux_media_payload(49, raw, sender_id=PEER, is_group=True)
        self.assertEqual(payload.app_type, 74)
        self.assertEqual(payload.metadata.size, 7)
        self.assertEqual(payload.metadata.aeskey, '')
        self.assertEqual(payload.metadata.cdnattachurl, '')
        self.assertNotIn('SECRET_FIXTURE', repr(payload))
        image = parse_linux_media_payload(3,
            '<msg><img md5="' + 'a'*32 + '" length="40" aeskey="SECRET_FIXTURE_KEY" cdnmidimgurl="SECRET_FIXTURE_URL"/></msg>')
        self.assertEqual(image.metadata.aeskey, '')
        self.assertEqual(image.metadata.cdnmidimgurl, '')

    def test_missing_checksums_paths_and_xml_entities_are_rejected(self):
        for xml in (file_xml(b'x', name='../escape.txt'), file_xml(b'x').replace('<md5>', '<md5>bad'),
                '<!DOCTYPE msg><msg><appmsg><type>6</type></appmsg></msg>',
                '<msg><appmsg><type>6</type><title>x</title><appattach><totallen>1</totallen></appattach></appmsg></msg>'):
            with self.assertRaises(ValueError):
                parse_linux_media_payload(49, xml)
        with self.assertRaises(ValueError):
            parse_linux_media_payload(3, '<msg><img md5="' + 'a'*32 + '"/></msg>')
        self.assertIsNone(parse_linux_media_payload(49, '<msg><appmsg><type>5</type></appmsg></msg>'))


@unittest.skipUnless(os.name == 'posix', 'Linux 私有缓存与 nofollow')
class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='helper-linux-media-receiver-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.storage = self.root / 'storage'
        self.storage.mkdir(mode=0o700)
        self.updates = queue.Queue()
        self.current = True

    def receiver(self, **kwargs):
        receiver = LinuxMediaReceiver(account=ACCOUNT, session_id=SESSION, pid=123,
            storage_root=self.storage, cache_root=self.root/'private',
            is_current=lambda:self.current, callback=self.updates.put, timeout=.15, **kwargs)
        self.addCleanup(receiver.close)
        return receiver

    def message(self, xml, kind=49, identifier='123', **kwargs):
        payload = parse_linux_media_payload(kind, xml)
        return Message('', ROOM, PEER, ACCOUNT, type=kind, is_group=True, session_id=SESSION,
            sequence=int(identifier), data={'native_message_id':identifier, 'self_wxid':ACCOUNT,
                '_linux_media':payload, 'raw_content':xml, 'arbitrary_private_value':'SECRET_FIXTURE_EXTRA'}, **kwargs)

    def cached_file(self, body, name='example.txt', *, account=ACCOUNT):
        path = self.storage / account / 'msg/file/2026-11' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        return path

    def test_image_download_receipt_requires_verified_bytes_and_requests_once(self):
        body = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII=')
        md5 = hashlib.md5(body).hexdigest()
        xml = f'<msg><img md5="{md5}" length="{len(body)}"/></msg>'
        calls = []
        def download(message, metadata):
            calls.append((message.session_id, message.chat_id, metadata.md5))
            path = self.storage / ACCOUNT / 'msg/attach/image.png'
            path.parent.mkdir(parents=True)
            path.write_bytes(body)
            return True
        receiver = self.receiver(request_image=download)
        message = self.message(xml, kind=3)
        receiver.submit(message)
        result = self.updates.get(timeout=3)
        self.assertEqual(result.data['image_status'], 'ready')
        self.assertEqual(result.data['image_integrity'], 'md5_verified')
        # The metadata checksum validates downloaded bytes; it does not prove
        # that WeChat supplied the sender's uncompressed original.
        self.assertEqual(result.data['image_quality'], 'unknown')
        self.assertEqual(Path(result.image_path).read_bytes(), body)
        receiver.submit(message)
        self.assertEqual(calls, [(SESSION, ROOM, md5)])

    def test_native_download_success_without_file_is_not_reported_ready(self):
        calls = []
        receiver = self.receiver(request_image=lambda message, metadata: calls.append(metadata.md5) or True)
        receiver.submit(self.message('<msg><img md5="' + 'a'*32 + '" length="20"/></msg>', kind=3))
        result = self.updates.get(timeout=3)
        self.assertEqual(result.data['image_status'], 'failed')
        self.assertNotIn('image_integrity', result.data)
        self.assertEqual(calls, ['a'*32])

    def test_verified_file74_ready_is_private_and_contains_no_raw_metadata(self):
        body = b'complete fixture file'
        self.cached_file(body)
        receiver = self.receiver()
        pending = receiver.submit(self.message(file_xml(body, kind=74)))
        self.assertEqual(pending.data['file_status'], 'pending')
        ready = self.updates.get(timeout=3)
        path = Path(ready.file_path)
        self.assertEqual(path.read_bytes(), body)
        self.assertEqual(ready.session_id, SESSION)
        self.assertEqual((ready.data['app_type'], ready.data['native_app_type']), (6, 74))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        for message in (pending, ready):
            serialized = json.dumps(dict(message.data))
            for forbidden in ('SECRET_FIXTURE', 'raw_content', '_linux_media', 'aeskey', 'cdnattachurl'):
                self.assertNotIn(forbidden, serialized)
        receiver.close()
        self.assertFalse(path.exists())

    def test_collision_cache_keeps_original_title_and_requires_full_checksum(self):
        body = b'complete fixture file'
        self.cached_file(b'x' * len(body), name='report(1).md')
        collision = self.cached_file(body, name='report(2).md')
        receiver = self.receiver()
        receiver.submit(self.message(file_xml(body, name='report.md', kind=74)))
        ready = self.updates.get(timeout=3)
        self.assertEqual(ready.data['file_status'], 'ready')
        self.assertEqual(ready.data['file_name'], 'report.md')
        self.assertEqual(Path(ready.file_path).read_bytes(), body)
        self.assertTrue(collision.exists())

    def test_unrelated_cache_name_with_identical_bytes_is_not_accepted(self):
        body = b'complete fixture file'
        self.cached_file(body, name='report-copy.md')
        receiver = self.receiver()
        receiver.submit(self.message(file_xml(body, name='report.md')))
        self.assertEqual(self.updates.get(timeout=3).data['file_status'], 'failed')

    def test_image_requires_full_checksum_and_v2_key_stays_in_memory(self):
        try:
            from tests.test_linux_media import CachedImageTests
        except ModuleNotFoundError:
            from test_linux_media import CachedImageTests
        body, key, encrypted, md5 = CachedImageTests.fixture()
        path = self.storage / (ACCOUNT + '_ab12') / 'msg/attach/image.dat'
        path.parent.mkdir(parents=True)
        path.write_bytes(encrypted)
        receiver = self.receiver()
        xml = f'<msg><img md5="{md5}" length="{len(body)}" aeskey="SECRET_FIXTURE_KEY"/></msg>'
        with patch.object(receiver, '_key', return_value=key) as read_key:
            receiver.submit(self.message(xml, kind=3))
            ready = self.updates.get(timeout=3)
            read_key.assert_called_once()
        self.assertEqual(ready.data['image_status'], 'ready')
        self.assertEqual(Path(ready.image_path).read_bytes(), body)
        self.assertEqual([p.name for p in path.parent.iterdir()], ['image.dat'])
        self.assertNotIn(key.hex(), repr(dict(ready.data)))

    def test_image_hardlinks_in_two_wechat_conversations_are_verified(self):
        try:
            from tests.test_linux_media import CachedImageTests
        except ModuleNotFoundError:
            from test_linux_media import CachedImageTests
        body, key, encrypted, md5 = CachedImageTests.fixture()
        attach = self.storage / ACCOUNT / 'msg/attach'
        first = attach / ('a' * 32) / '2026-11/Img' / (('b' * 32) + '_h.dat')
        second = attach / ('c' * 32) / '2026-11/Img' / (('d' * 32) + '_h.dat')
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        first.write_bytes(encrypted)
        os.link(first, second)
        receiver = self.receiver()
        xml = f'<msg><img md5="{md5}" length="{len(body)}"/></msg>'
        with patch.object(receiver, '_key', return_value=key):
            receiver.submit(self.message(xml, kind=3))
            ready = self.updates.get(timeout=3)
        self.assertEqual(ready.data['image_status'], 'ready')
        self.assertEqual(Path(ready.image_path).read_bytes(), body)

    def test_image_hardlink_outside_verified_cache_is_rejected(self):
        body = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII=')
        original = self.root / 'outside.png'
        original.write_bytes(body)
        candidate = self.storage / ACCOUNT / 'msg/attach' / ('a' * 32) / '2026-11/Img' / (('b' * 32) + '_h.dat')
        candidate.parent.mkdir(parents=True)
        os.link(original, candidate)
        receiver = self.receiver()
        xml = f'<msg><img md5="{hashlib.md5(body).hexdigest()}" length="{len(body)}"/></msg>'
        receiver.submit(self.message(xml, kind=3))
        self.assertEqual(self.updates.get(timeout=3).data['image_status'], 'failed')

    def test_image_pair_with_extra_hardlink_is_rejected(self):
        body = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII=')
        attach = self.storage / ACCOUNT / 'msg/attach'
        first = attach / ('a' * 32) / '2026-11/Img' / (('b' * 32) + '_h.dat')
        second = attach / ('c' * 32) / '2026-11/Img' / (('d' * 32) + '_h.dat')
        first.parent.mkdir(parents=True)
        second.parent.mkdir(parents=True)
        first.write_bytes(body)
        os.link(first, second)
        os.link(first, self.root / 'outside.png')
        receiver = self.receiver()
        xml = f'<msg><img md5="{hashlib.md5(body).hexdigest()}" length="{len(body)}"/></msg>'
        receiver.submit(self.message(xml, kind=3))
        self.assertEqual(self.updates.get(timeout=3).data['image_status'], 'failed')

    def test_wrong_account_wrong_hash_and_symlink_sources_never_become_ready(self):
        body=b'expected content'
        other=self.cached_file(body, account='wxid_other')
        wrong=self.cached_file(b'wrong but same!!')
        receiver=self.receiver()
        receiver.submit(self.message(file_xml(body)))
        self.assertEqual(self.updates.get(timeout=3).data['file_status'],'failed')
        wrong.unlink();wrong.symlink_to(other)
        receiver.submit(self.message(file_xml(body),identifier='124'))
        self.assertEqual(self.updates.get(timeout=3).data['file_status'],'failed')
        self.assertFalse(any(receiver._workspace.rglob('file-*.txt')))

    def test_stale_account_or_session_and_own_messages_do_not_enqueue(self):
        receiver=self.receiver()
        message=self.message(file_xml(b'fixture'))
        for invalid in (replace(message, session_id='old'), replace(message,is_self=True),
                replace(message,data=dict(message.data,self_wxid='wxid_other'))):
            self.assertEqual(receiver.submit(invalid).data['file_status'],'unavailable')
        self.current=False
        self.assertEqual(receiver.submit(message).data['file_status'],'unavailable')
        self.assertTrue(self.updates.empty())
        self.assertTrue(receiver._queue.empty())

    def test_old_job_cannot_publish_after_account_switch_or_close(self):
        receiver=self.receiver()
        entered, release=threading.Event(),threading.Event()
        self.addCleanup(release.set)
        def find(*args):
            entered.set();release.wait(2);return b'fixture'
        with patch.object(receiver,'_find',side_effect=find):
            receiver.submit(self.message(file_xml(b'fixture')))
            self.assertTrue(entered.wait(1))
            self.current=False
            release.set()
            receiver.close()
        self.assertFalse(receiver.is_alive)
        self.assertTrue(self.updates.empty())
        self.assertFalse(receiver._workspace.exists())

    def test_duplicate_message_is_not_saved_or_delivered_twice(self):
        body=b'fixture';self.cached_file(body)
        receiver=self.receiver()
        message=self.message(file_xml(body))
        receiver.submit(message)
        first=self.updates.get(timeout=3)
        second=receiver.submit(message)
        self.assertEqual(first.file_path,second.file_path)
        self.assertTrue(self.updates.empty())
        self.assertEqual(len(receiver._saved),1)

    def test_cache_file_and_byte_budgets_evict_only_this_session_files(self):
        body=b'fixture';self.cached_file(body)
        receiver=self.receiver(max_files=1,max_cache_bytes=10)
        receiver.submit(self.message(file_xml(body)))
        first=Path(self.updates.get(timeout=3).file_path)
        receiver.submit(self.message(file_xml(body),identifier='124'))
        second=Path(self.updates.get(timeout=3).file_path)
        self.assertFalse(first.exists());self.assertTrue(second.exists())
        self.assertTrue(self.cached_file(body).exists())
        self.assertEqual(receiver.submit(self.message(file_xml(b'x'*11),identifier='125')).data['file_status'],'failed')
        self.assertEqual(len(receiver._saved),1)

    def test_queue_is_bounded_and_raw_submit_uses_same_sanitized_contract(self):
        receiver=self.receiver(queue_size=1, worker_count=1)
        entered,release=threading.Event(),threading.Event()
        def find(*args):
            entered.set();release.wait(2);return None
        with patch.object(receiver,'_find',side_effect=find):
            raw=file_xml(b'fixture')
            first=replace(self.message(raw),data={'native_message_id':'123','self_wxid':ACCOUNT})
            result=receiver.submit(first,raw)
            self.assertEqual(result.data['file_status'],'pending')
            self.assertTrue(entered.wait(1))
            self.assertEqual(receiver.submit(self.message(raw,identifier='124')).data['file_status'],'pending')
            rejected=receiver.submit(self.message(raw,identifier='125'))
            self.assertEqual(rejected.data['file_status'],'failed')
            self.assertIn('队列已满',rejected.data['file_error'])
            receiver._stop.set();release.set();receiver.close()

    def test_slow_media_does_not_block_following_cached_file(self):
        first_entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        receiver = self.receiver(worker_count=2)
        original_find = receiver._find
        body = b'available now'
        self.cached_file(body, name='second.txt')
        def slow_first(payload, deadline):
            if payload.metadata.title == 'first.txt':
                first_entered.set()
                release.wait(2)
                return None
            return original_find(payload, deadline)
        with patch.object(receiver, '_find', side_effect=slow_first):
            receiver.submit(self.message(file_xml(b'not ready', name='first.txt')))
            self.assertTrue(first_entered.wait(1))
            receiver.submit(self.message(file_xml(body, name='second.txt'), identifier='124'))
            ready = self.updates.get(timeout=1)
            self.assertEqual(ready.data['native_message_id'], '124')
            self.assertEqual(ready.data['file_status'], 'ready')
            self.assertEqual(Path(ready.file_path).read_bytes(), body)
            release.set()

    def test_scan_budget_and_exception_details_are_not_exposed(self):
        self.cached_file(b'fixture')
        receiver=self.receiver(scan_bytes=1)
        receiver.submit(self.message(file_xml(b'fixture')))
        failed=self.updates.get(timeout=3)
        self.assertEqual(failed.data['file_status'],'failed')
        with patch.object(receiver,'_find',side_effect=RuntimeError('SECRET_FIXTURE_KEY')):
            receiver.submit(self.message(file_xml(b'fixture'),identifier='124'))
            failed=self.updates.get(timeout=3)
        self.assertNotIn('SECRET_FIXTURE',repr(dict(failed.data)))

    def test_linked_or_nonprivate_output_directory_is_rejected(self):
        real=self.root/'real';real.mkdir(mode=0o700)
        link=self.root/'linked';link.symlink_to(real,target_is_directory=True)
        for directory in (link,self.root/'public'):
            if directory.name=='public':directory.mkdir(mode=0o755)
            with self.assertRaises(ValueError):
                LinuxMediaReceiver(account=ACCOUNT,session_id=SESSION,pid=123,
                    storage_root=self.storage,cache_root=directory,is_current=lambda:True)


if __name__=='__main__':unittest.main()
