"""朋友圈任务的离线测试：仅用内存图片与假发布器，不启动或连接微信。"""
import base64
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zlib

from PIL import Image

from wxbot_framework.moments import MomentsPublisher
from wxbot_framework.panel_common import PanelError


def image_row(kind='PNG', *, name=None, color='red'):
    output = BytesIO()
    Image.new('RGB', (16, 12), color).save(output, format=kind)
    return {'name': name or ('photo.png' if kind == 'PNG' else 'photo.jpg'),
            'data_url': 'data:image/' + ('png' if kind == 'PNG' else 'jpeg') +
                        ';base64,' + base64.b64encode(output.getvalue()).decode('ascii')}


def request(identifier='request_moments_0001', **values):
    return dict({'request_id': identifier, 'session_id': 'session_test', 'content': '离线测试',
                 'visibility': 'public', 'images': []}, **values)


class MomentsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.session = {'session_id': 'session_test'}
        self.calls = []
        self.publishers = []

    def tearDown(self):
        for publisher in self.publishers:
            publisher.close()

    def publisher(self, deliver=None, connection=None, **kwargs):
        def publish(payload):
            self.calls.append(payload)
            return {'status': 'published', 'sns_id': '1234567890'}
        publisher = MomentsPublisher(self.root, deliver or publish, connection or (lambda: self.session), **kwargs)
        self.publishers.append(publisher)
        return publisher

    def wait(self, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(.005)
        self.fail('离线任务未在限定时间内完成')

    def final(self, publisher, identifier='request_moments_0001'):
        return self.wait(lambda: (row if (row := publisher.status(identifier))['status'] != 'pending' else None))

    def assert_rejected(self, publisher, data, status=400):
        with self.assertRaises(PanelError) as caught:
            publisher.submit(data)
        self.assertEqual(caught.exception.status, status)

    def test_text_only_published_requires_receipt_and_returns_no_content(self):
        publisher = self.publisher()
        publisher.submit(request())
        result = self.final(publisher)
        self.assertEqual(result, {'request_id': 'request_moments_0001', 'status': 'published',
                                  'error': '', 'sns_id': '1234567890'})
        self.assertEqual(self.calls[0]['content'], '离线测试')
        self.assertEqual(self.calls[0]['images'], [])
        self.assertFalse((self.root / '.runtime/moments').exists())

    def test_png_jpeg_use_real_format_and_uuid_names_and_cleanup(self):
        entered, release = threading.Event(), threading.Event()
        def publish(payload):
            self.calls.append(payload)
            entered.set()
            release.wait(4)
            return {'status': 'published', 'sns_id': '556677'}
        publisher = self.publisher(publish)
        # 展示扩展名不能决定保存类型。
        png, jpeg = image_row(name='not-really.jpeg'), image_row('JPEG', name='not-really.png')
        try:
            self.assertEqual(publisher.submit(request(content='', images=[png, jpeg], visibility='private'))['status'], 'pending')
            self.assertTrue(entered.wait(3))
            payload = self.calls[0]
            paths = [Path(path) for path in payload['images']]
            self.assertEqual(payload['visibility'], 'private')
            self.assertEqual([path.suffix for path in paths], ['.png', '.jpg'])
            for path in paths:
                self.assertRegex(path.stem, r'^[a-f0-9]{32}$')
                self.assertEqual(path.parent.parent, self.root / '.runtime/moments')
                self.assertTrue(path.is_file())
            for path, kind in zip(paths, ('PNG', 'JPEG')):
                with Image.open(path) as opened:
                    self.assertEqual(opened.format, kind)
        finally:
            release.set()
        self.assertEqual(self.final(publisher)['status'], 'published')
        self.assertFalse(any(path.exists() for path in paths))
        self.assertFalse(paths[0].parent.exists())

    def test_validation_rejects_empty_wrong_types_and_unsupported_fields(self):
        publisher = self.publisher()
        values = [None, [], request(content='', images=[]), request(content=' \n\t'),
                  request(content=123), request(content='x\0y'), request(content='\ud800'),
                  request(content='a' * 1001), request(content='😀' * 1001),
                  request(images={}), request(images=[{}]), request(images=[image_row()] * 10),
                  request(visibility='friends'), request(content='x', path='C:/private.png')]
        for data in values:
            with self.subTest(data=repr(data)[:70]):
                self.assert_rejected(publisher, data)
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / '.runtime').exists())

    def test_text_character_and_utf8_boundaries_are_framework_limits(self):
        publisher = self.publisher()
        publisher.submit(request(content='😀' * 1000))
        self.assertEqual(self.final(publisher)['status'], 'published')
        self.assertEqual(len(self.calls[0]['content'].encode('utf-8')), 4000)

    def test_request_and_session_ids_are_validated(self):
        publisher = self.publisher()
        for identifier in ('short', 'a' * 81, '../' + 'x' * 20, 'x' * 16 + '\n', 123, None):
            with self.subTest(identifier=identifier):
                self.assert_rejected(publisher, request(identifier))
        for session in ('', None, 123, 'x' * 81, '../session', 'session\n'):
            with self.subTest(session=session):
                self.assert_rejected(publisher, request(session_id=session), 409)

    def test_image_names_and_sources_cannot_be_paths_urls_or_bare_base64(self):
        publisher = self.publisher()
        good = image_row()
        images = [{'name': 'p.png', 'path': 'C:/private/p.png'},
                  {'name': 'p.png', 'url': 'https://example.invalid/p.png'},
                  {'name': 'p.png', 'data': good['data_url']},
                  dict(good, data_url=good['data_url'].split(',', 1)[1]),
                  dict(good, data_url='https://example.invalid/p.png'),
                  dict(good, data_url='data:image/png;base64,%%%'),
                  dict(good, data_url='data:image/png;base64,'),
                  dict(good, name='../source.png'), dict(good, name='C:\\private.png'),
                  dict(good, name='x\0.png'), dict(good, name='\ud800'),
                  dict(good, name=''), dict(good, name='a' * 256)]
        for row in images:
            with self.subTest(row=repr(row)[:80]):
                self.assert_rejected(publisher, request(images=[row]))
        self.assertFalse((self.root / '.runtime').exists())

    def test_bad_mime_corrupt_truncated_and_animated_images_are_rejected(self):
        publisher = self.publisher()
        png = image_row()
        raw = base64.b64decode(png['data_url'].split(',')[1])
        frames = BytesIO()
        Image.new('RGB', (8, 8), 'red').save(frames, format='PNG', save_all=True,
                                           append_images=[Image.new('RGB', (8, 8), 'blue')], duration=50, loop=0)
        gif = BytesIO()
        Image.new('RGB', (8, 8)).save(gif, format='GIF')
        urls = [png['data_url'].replace('image/png', 'image/jpeg'),
                'data:image/png;base64,' + base64.b64encode(b'not a picture').decode(),
                'data:image/png;base64,' + base64.b64encode(raw[:-18]).decode(),
                'data:image/png;base64,' + base64.b64encode(frames.getvalue()).decode(),
                'data:image/png;base64,' + base64.b64encode(gif.getvalue()).decode()]
        for url in urls:
            with self.subTest(url=url[:60]):
                self.assert_rejected(publisher, request(images=[dict(png, data_url=url)]))
        self.assertEqual(self.calls, [])

    def test_pixel_bomb_is_rejected_before_pixel_decode(self):
        publisher = self.publisher()
        png = image_row()
        raw = bytearray(base64.b64decode(png['data_url'].split(',')[1]))
        raw[16:24] = struct.pack('>II', 8000, 6000)
        raw[29:33] = struct.pack('>I', zlib.crc32(raw[12:29]))
        row = dict(png, data_url='data:image/png;base64,' + base64.b64encode(raw).decode())
        with patch.object(Image.Image, 'load', side_effect=AssertionError('不能解码过大像素图')) as decode:
            self.assert_rejected(publisher, request(images=[row]))
        decode.assert_not_called()

    def test_per_image_and_aggregate_decoded_byte_limits(self):
        publisher = self.publisher()
        good = image_row()
        raw = base64.b64decode(good['data_url'].split(',')[1])
        with patch('wxbot_framework.moments.MAX_IMAGE_BYTES', len(raw) - 1):
            self.assert_rejected(publisher, request(images=[good]))
        with patch('wxbot_framework.moments.MAX_TOTAL_IMAGE_BYTES', len(raw) * 2 - 1):
            self.assert_rejected(publisher, request(images=[good, good]))
        publisher.submit(request(images=[good] * 9))
        self.assertEqual(self.final(publisher)['status'], 'published')

    def test_disconnected_and_stale_session_are_rejected_without_delivery(self):
        publisher = self.publisher()
        self.session = None
        self.assert_rejected(publisher, request(), 409)
        self.session = {'session_id': 'a_new_session'}
        self.assert_rejected(publisher, request(), 409)
        self.assertEqual(self.calls, [])
        with self.assertRaises(PanelError) as caught:
            publisher.status('request_moments_0001')
        self.assertEqual(caught.exception.status, 404)

    def test_session_expires_after_acceptance_and_before_native_call(self):
        count = 0
        def connection():
            nonlocal count
            count += 1
            return {'session_id': 'session_test'} if count == 1 else None
        publisher = self.publisher(connection=connection)
        publisher.submit(request(images=[image_row()]))
        self.assertEqual(self.final(publisher)['status'], 'failed')
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / '.runtime').exists())

    def test_session_expiry_during_staging_prevents_native_submission_and_cleans(self):
        publisher = self.publisher()
        original = publisher._stage
        def stage(images):
            result = original(images)
            self.session = None
            return result
        with patch.object(publisher, '_stage', side_effect=stage):
            publisher.submit(request(images=[image_row()]))
            self.assertEqual(self.final(publisher)['status'], 'failed')
        self.assertEqual(self.calls, [])
        self.assertEqual(list((self.root / '.runtime/moments').iterdir()), [])

    def test_duplicate_payload_reuses_pending_and_completed_job(self):
        entered, release = threading.Event(), threading.Event()
        def publish(payload):
            self.calls.append(payload)
            entered.set()
            release.wait(4)
            return {'status': 'published', 'sns_id': '1234'}
        publisher = self.publisher(publish)
        data = request(images=[image_row()])
        try:
            first = publisher.submit(data)
            self.assertTrue(entered.wait(3))
            self.assertEqual(publisher.submit(deepcopy(data)), first)
        finally:
            release.set()
        finished = self.final(publisher)
        self.session = None
        self.assertEqual(publisher.submit(data), finished)
        self.assertEqual(len(self.calls), 1)

    def test_reused_id_with_different_text_image_visibility_or_session_conflicts(self):
        publisher = self.publisher()
        data = request(images=[image_row()])
        publisher.submit(data)
        self.final(publisher)
        for changed in (dict(data, content='不同文字'), dict(data, images=[image_row(color='blue')]),
                        dict(data, visibility='private'), dict(data, session_id='different_session'),
                        dict(data, images=[dict(data['images'][0], name='renamed.png')])):
            with self.subTest(changed=repr(changed)[:60]):
                self.assert_rejected(publisher, changed, 409)
        self.assertEqual(len(self.calls), 1)

    def test_parallel_repeat_calls_submit_once_and_second_id_is_blocked(self):
        entered, release = threading.Event(), threading.Event()
        def publish(payload):
            self.calls.append(payload)
            entered.set()
            release.wait(4)
            return {'status': 'published', 'sns_id': '1234'}
        publisher = self.publisher(publish)
        barrier = threading.Barrier(3)
        results, errors = [], []
        def submit():
            barrier.wait()
            try:
                results.append(publisher.submit(request()))
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=submit) for _ in range(2)]
        try:
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(3)
            self.assertEqual(errors, [])
            self.assertEqual([row['status'] for row in results], ['pending', 'pending'])
            self.assertTrue(entered.wait(3))
            self.assert_rejected(publisher, request('request_moments_0002'), 409)
            self.assertEqual(len(self.calls), 1)
        finally:
            release.set()
        self.assertEqual(self.final(publisher)['status'], 'published')

    def test_only_explicit_preflight_failure_is_definitively_failed(self):
        publisher = self.publisher(lambda payload: {'status': 'failed', 'phase': 'preflight',
                                                   'error': 'C:\\secret\\TEST_ONLY_SECRET'})
        publisher.submit(request(images=[image_row()]))
        result = self.final(publisher)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('secret', json.dumps(result))
        self.assertEqual(list((self.root / '.runtime/moments').iterdir()), [])

    def test_ambiguous_results_and_native_exceptions_never_claim_published(self):
        results = [None, True, False, {'status': 'published'}, {'status': 'published', 'sns_id': ''},
                   {'status': 'failed'}, {'status': 'submitted'}, {'status': 'unknown'},
                   {'status': 'published', 'sns_id': 1234},
                   {'status': 'published', 'sns_id': 'C:\\private\\photo.png'},
                   {'status': 'published', 'sns_id': 'sk-test'}]
        for index, result in enumerate(results):
            publisher = self.publisher(lambda payload, value=result: value)
            identifier = f'request_result_{index:04d}'
            publisher.submit(request(identifier))
            self.assertEqual(self.final(publisher, identifier)['status'], 'unknown')
        def broken(payload):
            raise ConnectionError('正文和 C:\\private\\picture.png TEST_ONLY_SECRET')
        publisher = self.publisher(broken)
        publisher.submit(request(images=[image_row()]))
        row = self.final(publisher)
        self.assertEqual(row['status'], 'unknown')
        self.assertNotIn('private', json.dumps(row))
        self.assertNotIn('TEST_ONLY', json.dumps(row))
        self.assertEqual(list((self.root / '.runtime/moments').iterdir()), [])

    def test_disconnect_after_submit_makes_receipt_unknown(self):
        def publish(payload):
            self.session = None
            return {'status': 'published', 'sns_id': '1234'}
        publisher = self.publisher(publish)
        publisher.submit(request())
        self.assertEqual(self.final(publisher)['status'], 'unknown')

    def test_timeout_keeps_files_and_busy_until_native_returns_no_automatic_retry(self):
        entered, release = threading.Event(), threading.Event()
        def publish(payload):
            self.calls.append(payload)
            entered.set()
            release.wait(4)
            return {'status': 'published', 'sns_id': '1234'}
        publisher = self.publisher(publish, timeout=.1)
        data = request(images=[image_row()])
        try:
            publisher.submit(data)
            self.assertTrue(entered.wait(3))
            path = Path(self.calls[0]['images'][0])
            row = self.final(publisher)
            self.assertEqual(row['status'], 'unknown')
            self.assertTrue(path.is_file())
            self.assertEqual(publisher.submit(data), row)
            self.assert_rejected(publisher, request('request_moments_0002'), 409)
        finally:
            release.set()
        self.wait(lambda: not path.exists())
        self.assertEqual(publisher.status(data['request_id'])['status'], 'unknown')
        self.assertEqual(len(self.calls), 1)

    def test_close_during_native_call_keeps_files_until_return_and_blocks_new_jobs(self):
        entered, release = threading.Event(), threading.Event()
        def publish(payload):
            self.calls.append(payload)
            entered.set()
            release.wait(4)
            return {'status': 'published', 'sns_id': '1234'}
        publisher = self.publisher(publish)
        try:
            publisher.submit(request(images=[image_row()]))
            self.assertTrue(entered.wait(3))
            path = Path(self.calls[0]['images'][0])
            publisher.close()
            self.assertEqual(publisher.status('request_moments_0001')['status'], 'unknown')
            self.assertTrue(path.exists())
            self.assert_rejected(publisher, request('request_moments_0002'), 409)
        finally:
            release.set()
        self.wait(lambda: not path.exists())
        self.assertEqual(publisher.status('request_moments_0001')['status'], 'unknown')

    def test_native_lease_guard_retains_images_and_account_slot_after_deliver_returns(self):
        for index, flag in enumerate(('unload_blocked', 'retain_images')):
            with self.subTest(flag=flag):
                calls = []
                def publish(payload):
                    calls.append(payload)
                    return {'status': 'published', 'sns_id': '1234', flag: True,
                            'private_path': 'C:\\private\\picture.png'}
                publisher = self.publisher(publish)
                identifier = f'request_retained_{index:04d}'
                data = request(identifier, images=[image_row()])
                publisher.submit(data)
                row = self.final(publisher, identifier)
                self.assertEqual(row['status'], 'unknown')
                self.assertEqual(row['sns_id'], '')
                path = Path(calls[0]['images'][0])
                self.assertTrue(path.is_file())
                self.assertEqual(publisher.submit(data), row)
                self.assert_rejected(publisher, request('request_moments_next'), 409)
                publisher.close()
                self.assertTrue(path.exists())
                self.assertNotIn('private_path', row)
                self.assertNotIn(str(path), json.dumps(row))


    def test_staging_error_is_preflight_failed_and_does_not_expose_paths(self):
        publisher = self.publisher()
        with patch.object(publisher, '_stage', side_effect=OSError('C:\\private\\photo.png TEST_ONLY_SECRET')):
            publisher.submit(request(images=[image_row()]))
            result = self.final(publisher)
        self.assertEqual(result['status'], 'failed')
        self.assertNotIn('private', json.dumps(result))
        self.assertEqual(self.calls, [])

    def test_cleanup_does_not_delete_source_images_or_unrelated_files(self):
        source = self.root / 'source.png'
        raw = base64.b64decode(image_row()['data_url'].split(',')[1])
        source.write_bytes(raw)
        unrelated = self.root / '.runtime/moments/user-photos/original.png'
        unrelated.parent.mkdir(parents=True)
        unrelated.write_bytes(raw)
        publisher = self.publisher()
        publisher.submit(request(images=[{'name': source.name,
            'data_url': 'data:image/png;base64,' + base64.b64encode(source.read_bytes()).decode()}]))
        self.assertEqual(self.final(publisher)['status'], 'published')
        self.assertEqual(source.read_bytes(), raw)
        self.assertEqual(unrelated.read_bytes(), raw)
        self.assertEqual(list(unrelated.parent.parent.iterdir()), [unrelated.parent])

    def test_reparse_runtime_directory_is_rejected_without_deleting_target(self):
        target = self.root / 'user-photos'
        target.mkdir()
        marker = target / 'keep.txt'
        marker.write_text('用户文件', encoding='utf-8')
        runtime = self.root / '.runtime'
        try:
            runtime.symlink_to(target, target_is_directory=True)
        except OSError:
            if sys.platform != 'win32':
                self.skipTest('本机不允许创建符号链接')
            import _winapi
            _winapi.CreateJunction(str(target), str(runtime))
            self.addCleanup(lambda: runtime.rmdir() if runtime.exists() else None)
        else:
            self.addCleanup(lambda: runtime.unlink(missing_ok=True))
        publisher = self.publisher()
        publisher.submit(request(images=[image_row()]))
        self.assertEqual(self.final(publisher)['status'], 'failed')
        self.assertEqual(self.calls, [])
        self.assertEqual(marker.read_text(encoding='utf-8'), '用户文件')
        self.assertEqual(list(target.iterdir()), [marker])

    def test_history_keeps_only_last_64_finished_records_and_snapshot_is_copy(self):
        publisher = self.publisher()
        for index in range(66):
            identifier = f'request_history_{index:04d}'
            publisher.submit(request(identifier))
            self.final(publisher, identifier)
        for index in range(2):
            with self.assertRaises(PanelError) as caught:
                publisher.status(f'request_history_{index:04d}')
            self.assertEqual(caught.exception.status, 404)
        snapshot = publisher.status('request_history_0065')
        snapshot.update(status='invented', content='cannot mutate task')
        self.assertEqual(publisher.status('request_history_0065')['status'], 'published')
        self.assertNotIn('content', publisher.status('request_history_0065'))
        self.assertEqual(len(publisher._jobs), 64)

    def test_timeout_before_delivery_does_not_submit_after_staging_finishes(self):
        entered, release = threading.Event(), threading.Event()
        publisher = self.publisher(timeout=.05)
        original = publisher._stage
        def stage(images):
            result = original(images)
            entered.set()
            release.wait(4)
            return result
        try:
            with patch.object(publisher, '_stage', side_effect=stage):
                publisher.submit(request(images=[image_row()]))
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.final(publisher)['status'], 'failed')
                release.set()
                self.wait(lambda: list((self.root / '.runtime/moments').iterdir()) == [])
        finally:
            release.set()
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
