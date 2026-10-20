"""Fish 协议与流式 WAV 长度校正；所有 HTTP 使用内存响应。"""
from io import BytesIO
import http.client
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse
import wave

from wxbot_framework.tts import FishTTS, TTSError, normalize_wav


def wav(seconds=0.1, rate=24000, channels=1):
    data = BytesIO()
    with wave.open(data, 'wb') as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b'\0\0' * int(rate * seconds) * channels)
    return data.getvalue()


class TTSTests(unittest.TestCase):
    def setUp(self):
        self.client = FishTTS('https://api.fish.audio', 'test-only-placeholder', default_voice='测试音色')

    def test_streaming_wav_becomes_actual_duration_without_changing_pcm(self):
        original = wav()
        for placeholder in (0xffffff00, 0xffffffff):
            stream = bytearray(original)
            struct.pack_into('<I', stream, 4, min(placeholder + 36, 0xffffffff))
            struct.pack_into('<I', stream, 40, placeholder)
            self.assertEqual(normalize_wav(bytes(stream)), original)
        with wave.open(BytesIO(normalize_wav(original))) as audio:
            self.assertEqual(audio.getnframes(), 2400)

    def test_rejects_truncated_oversized_stereo_and_non_audio(self):
        for data in (wav()[:-2], wav(60.01), wav(channels=2), b'not-audio' * 10):
            with self.subTest(size=len(data)), self.assertRaises(TTSError):
                normalize_wav(data)

    def test_catalog_filters_untrained_and_paginates_without_losing_ids(self):
        a = {'_id': 'a' * 32, 'title': '甲', 'state': 'trained', 'type': 'tts'}
        b = {**a, '_id': 'b' * 32, 'title': '乙'}
        with patch.object(FishTTS, '_request', side_effect=[json.dumps({'items': [a, {**b, 'state': 'training'}], 'has_more': True}).encode(),
                json.dumps({'items': [a, b], 'has_more': False}).encode()]) as request:
            voices = self.client.voices()
        self.assertEqual({v.id for v in voices}, {'a' * 32, 'b' * 32})
        self.assertIn('page_number=2', request.call_args.args[0])

    def test_tts_request_has_reference_wav_and_correct_sample_rate(self):
        with patch.object(FishTTS, '_request', return_value=wav()) as request:
            self.client.synthesize('你好', 'a' * 32)
        self.assertEqual(request.call_args.kwargs['payload'],
                         {'text': '你好', 'reference_id': 'a' * 32, 'format': 'wav', 'sample_rate': 24000})

    def test_http_failure_does_not_expose_credentials_or_response(self):
        errors = [urllib.error.HTTPError('secret-url', 401, 'secret-body', {}, None),
                  http.client.IncompleteRead(b'secret-partial'), OSError('secret-error')]
        for failure in errors:
            with self.subTest(error=type(failure)), patch('urllib.request.build_opener') as opener:
                opener.return_value.open.side_effect = failure
                with self.assertRaises(TTSError) as caught:
                    self.client.voices()
                self.assertNotIn('secret', str(caught.exception))
                self.assertNotIn(self.client.api_key, str(caught.exception))
        self.assertNotIn(self.client.api_key, repr(self.client))

    def test_public_search_encodes_query_and_filters_nonpublic_unavailable_models(self):
        voice = {'_id': 'a' * 32, 'title': '温柔', 'state': 'trained', 'type': 'tts', 'visibility': 'public'}
        items = [voice, {**voice, '_id': 'b' * 32, 'visibility': 'private'},
                 {**voice, '_id': 'c' * 32, 'state': 'training'},
                 {**voice, '_id': 'd' * 32, 'dmca_taken_down': True},
                 {**voice, '_id': 'e' * 32, 'type': 'svc'}, None]
        with patch.object(FishTTS, '_request', return_value=json.dumps(
                {'items': items, 'has_more': True, 'window_limited': True}).encode()) as request:
            result = self.client.public_voices(page=2, keyword='温柔&self=true')
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.call_args.args[0]).query)
        self.assertEqual(query['self'], ['false'])
        self.assertEqual(query['title'], ['温柔&self=true'])
        self.assertEqual(query['page_number'], ['2'])
        self.assertEqual(query['language'], ['zh'])
        self.assertEqual(query['title_language'], ['zh'])
        self.assertEqual([v.id for v in result.voices], ['a' * 32])
        self.assertTrue(result.has_more and result.window_limited)

    def test_all_language_public_search_does_not_apply_chinese_filter(self):
        with patch.object(FishTTS, '_request', return_value=b'{"items": [], "has_more": false}') as request:
            self.client.public_voices(all_languages=True)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.call_args.args[0]).query)
        self.assertNotIn('language', query)
        self.assertNotIn('title_language', query)

    def test_public_id_lookup_requires_matching_trained_public_voice(self):
        voice = {'_id': 'a' * 32, 'title': '测试公开', 'state': 'trained', 'type': 'tts', 'visibility': 'public'}
        with patch.object(FishTTS, '_request', return_value=json.dumps(voice).encode()) as request:
            self.assertEqual(self.client.public_voice('a' * 32).name, '测试公开')
        self.assertEqual(request.call_args.args[0], '/model/' + 'a' * 32)
        for invalid in ({'visibility': 'unlist'}, {'_id': 'b' * 32}, {'dmca_taken_down': True}):
            with self.subTest(invalid=invalid), patch.object(FishTTS, '_request', return_value=json.dumps({**voice, **invalid}).encode()):
                with self.assertRaises(TTSError):
                    self.client.public_voice('a' * 32)

    def test_invalid_public_search_parameters_fail_before_network(self):
        with patch.object(FishTTS, '_request') as request:
            for options in ({'page': 0}, {'page': True}, {'keyword': 'bad\nname'}, {'page_size': 101}):
                with self.subTest(options=options), self.assertRaises(TTSError):
                    self.client.public_voices(**options)
            with self.assertRaises(TTSError):
                self.client.public_voice('../private')
            request.assert_not_called()

    def test_config_rejects_redirect_credentials_invalid_model_and_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'tts.json'
            valid = {'api_key': 'placeholder', 'default_voice': ''}
            for invalid in ({'base_url': 'http://example.test'}, {'base_url': 'https://user:pass@example.test'},
                            {'api_key': 'bad\r\nkey'}, {'model': 'typo'}, {'max_chars_per_clip': True}):
                path.write_text(json.dumps({**valid, **invalid}), encoding='utf-8')
                with self.assertRaises(TTSError):
                    FishTTS.from_file(path)
            path.write_text(json.dumps(valid), encoding='utf-8')
            self.assertEqual(FishTTS.from_file(path).model, 's2.1-pro-free')


if __name__ == '__main__':
    unittest.main()
