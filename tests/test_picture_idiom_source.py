"""在线题源的契约、下载限制和隐私处理；全部使用本地模拟响应。"""
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

from PIL import Image, PngImagePlugin

from plugins.picture_idiom.source import (
    ENDPOINT, WQWL_ENDPOINT, MAX_IMAGE_BYTES, MAX_JSON_BYTES, FFAPI, WQWL, RepeatedQuestion,
    SourceError, _NoRedirect, clean_image, image_url,
)


ROOT = Path(__file__).resolve().parents[1]
PICTURE_URL = 'http://www.ffapi.cn/int/v1/ktccy?format=picurl&order=a/b+c%3D'


def picture(*, format='PNG', size=(64, 48), metadata=False):
    output = BytesIO()
    kwargs = {}
    if metadata:
        info = PngImagePlugin.PngInfo()
        info.add_text('answer', '一刀两断')
        info.add_text('private', 'test metadata')
        kwargs['pnginfo'] = info
    Image.new('RGB', size, (20, 80, 180)).save(output, format=format, **kwargs)
    return output.getvalue()


def payload(**changes):
    data = {'code': 1, 'answer': '一刀两断', 'imgurl': PICTURE_URL}
    data.update(changes)
    return json.dumps(data, ensure_ascii=False).encode('utf-8')


class SourceTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.runtime/tests'
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.api = FFAPI(self.directory)

    def test_fetch_uses_fixed_endpoint_and_opaque_clean_png(self):
        with patch.object(self.api, '_get', side_effect=[payload(), picture(metadata=True)]) as get:
            result = self.api.fetch()
        self.assertEqual(get.call_args_list[0].args, (ENDPOINT,))
        self.assertEqual(get.call_args_list[0].kwargs['limit'], MAX_JSON_BYTES)
        self.assertEqual(get.call_args_list[1].args,
                         ('https://www.ffapi.cn/int/v1/ktccy?format=picurl&order=a/b+c%3D',))
        self.assertEqual(get.call_args_list[1].kwargs['limit'], MAX_IMAGE_BYTES)
        self.assertEqual(result.answer, '一刀两断')
        self.assertEqual(result.kind, 'online')
        self.assertRegex(result.image_path.name, r'^online-[0-9a-f]{24}\.png$')
        self.assertNotIn(result.answer, result.image_path.name)
        with Image.open(result.image_path) as image:
            self.assertEqual(image.format, 'PNG')
            self.assertEqual(image.size, (64, 48))
            self.assertEqual(image.info, {})
        self.assertEqual(list(self.directory.glob('*.tmp')), [])

    def test_jpeg_is_normalized_to_png(self):
        with Image.open(BytesIO(clean_image(picture(format='JPEG')))) as result:
            self.assertEqual(result.format, 'PNG')
            self.assertEqual(result.mode, 'RGB')

    def test_recent_answer_skips_image_download(self):
        with patch.object(self.api, '_get', return_value=payload()) as get:
            with self.assertRaises(RepeatedQuestion):
                self.api.fetch(exclude=('一刀两断',))
        get.assert_called_once()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_invalid_json_schema_and_answer_fail_without_image_download(self):
        cases = [b'not json', b'\xff', b'[]', b'null', b'{}',
                 payload(code=True), payload(code='1'), payload(code=0),
                 payload(answer='不是成语呀'), payload(answer='一刀两断。'),
                 payload(answer=None), payload(imgurl='https://localhost/image.png')]
        for raw in cases:
            with self.subTest(raw=raw), patch.object(self.api, '_get', return_value=raw) as get:
                with self.assertRaises(SourceError):
                    self.api.fetch()
                get.assert_called_once()

    def test_image_url_is_restricted_and_preserves_query_bytes(self):
        self.assertEqual(image_url(PICTURE_URL),
                         'https://www.ffapi.cn/int/v1/ktccy?format=picurl&order=a/b+c%3D')
        for url in ('http://127.0.0.1/int/v1/ktccy', 'file:///int/v1/ktccy',
                    'https://ffapi.cn.attacker.invalid/int/v1/ktccy',
                    'https://user:password@ffapi.cn/int/v1/ktccy',
                    'https://ffapi.cn:8000/int/v1/ktccy', 'https://ffapi.cn/other',
                    'https://ffapi.cn/int/v1/ktccy#answer',
                    'https://ffapi.cn\\@attacker.invalid/int/v1/ktccy',
                    'https://ffapi.cn/int/v1/ktccy?order=bad\nvalue', None):
            with self.subTest(url=url), self.assertRaises(SourceError):
                image_url(url)

    def test_redirects_are_not_followed(self):
        request = urllib.request.Request(ENDPOINT)
        self.assertIsNone(_NoRedirect().redirect_request(request, None, 302, 'Found', {},
                                                        'https://localhost/private'))

    def test_http_request_timeout_and_bounded_read(self):
        response = Mock()
        response.read.return_value = b'x' * 11
        opened = Mock()
        opened.__enter__ = Mock(return_value=response)
        opened.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = opened
        with patch('plugins.picture_idiom.source.urllib.request.build_opener', return_value=opener):
            with self.assertRaises(SourceError):
                self.api._get(ENDPOINT, limit=10, accept='application/json')
        response.read.assert_called_once_with(11)
        args, kwargs = opener.open.call_args
        self.assertEqual(args[0].full_url, ENDPOINT)
        self.assertEqual(kwargs, {'timeout': 8})
        self.assertEqual(args[0].get_header('Accept'), 'application/json')

    def test_http_errors_do_not_expose_url_or_body(self):
        errors = [urllib.error.HTTPError(ENDPOINT + '?private=test', 503, 'private body', {}, None),
                  urllib.error.URLError('private connection details'), TimeoutError('private timeout')]
        for error in errors:
            opener = Mock()
            opener.open.side_effect = error
            with self.subTest(error=type(error).__name__), \
                    patch('plugins.picture_idiom.source.urllib.request.build_opener', return_value=opener):
                with self.assertRaises(SourceError) as caught:
                    self.api._get(ENDPOINT, limit=100, accept='application/json')
                self.assertNotIn('private', str(caught.exception))
                self.assertNotIn(ENDPOINT, str(caught.exception))

    def test_invalid_truncated_or_oversized_images_are_rejected(self):
        for data in (b'', b'<html>error</html>', picture()[:30], picture(size=(16, 16)),
                     picture(format='GIF'), b'x' * (MAX_IMAGE_BYTES + 1)):
            with self.subTest(length=len(data)), self.assertRaises(SourceError):
                clean_image(data)
        with patch('plugins.picture_idiom.source.MAX_PIXELS', 1000), self.assertRaises(SourceError):
            clean_image(picture())

    def test_animated_png_is_rejected(self):
        output = BytesIO()
        Image.new('RGB', (64, 48), 'red').save(output, format='PNG', save_all=True,
            append_images=[Image.new('RGB', (64, 48), 'blue')], duration=100, loop=0)
        with self.assertRaises(SourceError):
            clean_image(output.getvalue())

    def test_atomic_write_failure_leaves_no_question_file(self):
        with patch.object(self.api, '_get', side_effect=[payload(), picture()]), \
                patch.object(Path, 'replace', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError):
                self.api.fetch()
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_timeout_configuration_validation(self):
        for value in (0, 31, True, '8', float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                FFAPI(self.directory, timeout_seconds=value)

    def test_wqwl_fetch_uses_img_field_and_normalizes_actual_image_format(self):
        api = WQWL(self.directory)
        image_url_value = 'https://free.wqwlkj.cn/wqwlapi/data/ccy/74c02273cd3a0c53e8570f03e3e380e5.jpg'
        body = json.dumps({'code': 1, 'answer': '前功尽弃', 'img': image_url_value}).encode()
        with patch.object(api, '_get', side_effect=[body, picture(metadata=True)]) as get:
            puzzle = api.fetch()
        self.assertEqual(get.call_args_list[0].args, (WQWL_ENDPOINT,))
        self.assertEqual(get.call_args_list[1].args, (image_url_value,))
        self.assertEqual(puzzle.answer, '前功尽弃')
        with Image.open(puzzle.image_path) as image:
            self.assertEqual((image.format, image.info), ('PNG', {}))

    def test_wqwl_invalid_response_and_repeated_answer_skip_download(self):
        api = WQWL(self.directory)
        image = 'https://free.wqwlkj.cn/wqwlapi/data/ccy/example.png'
        valid = {'code': 1, 'img': image, 'answer': '前功尽弃'}
        cases = [b'not json', b'null', payload(),
                 *[json.dumps(dict(valid, **change)).encode() for change in
                   ({'code': -1}, {'code': True}, {'answer': '五个字成语'}, {'img': 'https://127.0.0.1/private'})]]
        for body in cases:
            with self.subTest(body=body), patch.object(api, '_get', return_value=body) as get:
                with self.assertRaises(SourceError):
                    api.fetch()
                get.assert_called_once()
        with patch.object(api, '_get', return_value=json.dumps(valid).encode()) as get:
            with self.assertRaises(RepeatedQuestion):
                api.fetch(exclude=('前功尽弃',))
            get.assert_called_once()

    def test_wqwl_images_must_belong_to_its_question_directory(self):
        expected = 'https://free.wqwlkj.cn/wqwlapi/data/ccy/example.jpg'
        self.assertEqual(image_url(expected.replace('https:', 'http:'), provider='wqwl'), expected)
        for url in ('https://free.wqwlkj.cn.attacker.invalid/wqwlapi/data/ccy/example.jpg',
                    'https://free.wqwlkj.cn/wqwlapi/data/ccy/../example.jpg',
                    'https://free.wqwlkj.cn/wqwlapi/data/ccy/%2e%2e/example.jpg',
                    'https://free.wqwlkj.cn/private/example.jpg',
                    'https://free.wqwlkj.cn/wqwlapi/data/ccy/example.svg',
                    'https://user:password@free.wqwlkj.cn/wqwlapi/data/ccy/example.jpg',
                    'https://free.wqwlkj.cn:8000/wqwlapi/data/ccy/example.jpg',
                    'https://ffapi.cn/int/v1/ktccy', None):
            with self.subTest(url=url), self.assertRaises(SourceError):
                image_url(url, provider='wqwl')


if __name__ == '__main__':
    unittest.main()
