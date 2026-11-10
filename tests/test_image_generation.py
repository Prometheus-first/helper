import base64
from contextlib import contextmanager, nullcontext
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from plugins.image_generation import Plugin
from plugins.image_generation.client import GenerationClient, GenerationError, GenerationCancelled, _public_url
from wxbot_framework.sdk import Message


class Response(BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {'Content-Length': str(len(data))}


def png():
    out = BytesIO()
    Image.new('RGB', (20, 30), 'blue').save(out, 'PNG')
    return out.getvalue()


class ImageGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        config = self.root / '.runtime/image_generation.json'
        config.parent.mkdir()
        config.write_text(json.dumps({'base_url': 'https://api.example.com', 'api_key': 'TEST_KEY',
                                      'base_urls': ['https://api.example.com', 'https://two.example.com',
                                                    'https://three.example.com', 'https://four.example.com'],
                                      'model': 'gpt-image-2-4k'}), encoding='utf-8')
        self.client = GenerationClient(config, self.root / 'images')
        self.client.opener = Mock()

    def test_request_and_base64_image_cleanup(self):
        response = {'data': [{'b64_json': base64.b64encode(png()).decode()}]}
        self.client.opener.open.return_value = Response(json.dumps(response).encode())
        with self.client.generate('橘猫') as image:
            path = image.path
            with Image.open(path) as result:
                self.assertEqual(result.size, (20, 30))
            request = self.client.opener.open.call_args.args[0]
            self.assertEqual(request.full_url, 'https://api.example.com/v1/images/generations')
            self.assertEqual(json.loads(request.data)['model'], 'gpt-image-2-4k')
        self.assertFalse(path.exists())

    def test_invalid_response_does_not_leak_or_send(self):
        self.client.opener.open.return_value = Response(b'{"data":[{"b64_json":"!!!"}]}')
        with self.assertRaises(GenerationError):
            with self.client.generate('test'):
                pass
        self.assertFalse((self.root / 'images').exists())

    def test_private_download_url_is_blocked(self):
        self.client.opener.open.return_value = Response(b'{"data":[{"url":"https://127.0.0.1/image"}]}')
        with self.assertRaises(GenerationError):
            with self.client.generate('test'):
                pass
        self.assertEqual(self.client.opener.open.call_count, 1)

    def test_proxy_fake_ip_is_not_treated_as_public(self):
        fake = [(2, 1, 6, '', ('198.18.24.69', 443))]
        with patch('socket.getaddrinfo', return_value=fake):
            with self.assertRaises(GenerationError):
                _public_url('https://other.example/image')

    def test_plugin_command_and_session_guard(self):
        ctx = SimpleNamespace(project_root=self.root, members=SimpleNamespace(groups={'group@chatroom'}),
                              plugin_is_active=lambda name: True, _service=SimpleNamespace(session_is_current=lambda s: s == 'ok'),
                              logger=Mock())
        sent = []
        def text(message, value, delivery_guard=None):
            with delivery_guard() if delivery_guard else nullcontext():
                sent.append(value)
        def image(message, path, delivery_guard=None):
            with delivery_guard():
                sent.append(Path(path).is_file())
            return True
        ctx.reply, ctx.reply_image = text, image
        plugin = Plugin()
        plugin.on_load(ctx)
        self.addCleanup(plugin.on_unload, ctx)
        done = threading.Event()
        @contextmanager
        def generate(prompt, current, base_url=None):
            self.assertEqual(prompt, '橘猫')
            self.assertTrue(current())
            path = self.root / 'image.jpg'
            path.write_bytes(png())
            try:
                yield SimpleNamespace(path=path, retain=lambda: None)
            finally:
                done.set()
        plugin.client.generate = generate
        plugin.on_message(Message('/生图 橘猫', 'group@chatroom', is_group=True, session_id='ok'), ctx)
        self.assertTrue(done.wait(3))
        self.assertEqual(sent, ['开始生图，请稍候。', True])
        plugin.on_message(Message('/生图 橘猫', 'other@chatroom', is_group=True, session_id='ok'), ctx)
        plugin.on_message(Message('/生图 橘猫', 'other', session_id='old'), ctx)
        self.assertEqual(len(sent), 2)

    def test_parallel_channels_exhaust_retries(self):
        ctx = SimpleNamespace(project_root=self.root, members=SimpleNamespace(groups=set()),
                              plugin_is_active=lambda name: True, _service=SimpleNamespace(session_is_current=lambda s: True),
                              logger=Mock(), reply=Mock(), reply_image=Mock(return_value=True))
        plugin = Plugin()
        plugin.on_load(ctx)
        self.addCleanup(plugin.on_unload, ctx)
        message = Message('/生图 测试', 'one', session_id='ok')
        attempts = []
        @contextmanager
        def fail(prompt, current, base_url=None):
            attempts.append(base_url)
            raise GenerationError('temporary')
            yield
        plugin.client.generate = fail
        with patch('plugins.image_generation.RETRY_DELAYS', (0, 0, 0, 0, 0)):
            plugin._generate(message, '测试', ctx)
        self.assertEqual(len(attempts), 24)
        for base in plugin.client.channels():
            self.assertEqual(attempts.count(base), 6)
        self.assertEqual(ctx.reply.call_args.args[1], '生图失败')
        ctx.reply_image.assert_not_called()

    def test_parallel_first_success_sends_once_and_cancels_others(self):
        ctx = SimpleNamespace(project_root=self.root, members=SimpleNamespace(groups=set()),
                              plugin_is_active=lambda name: True, _service=SimpleNamespace(session_is_current=lambda s: True),
                              logger=Mock(), reply=Mock(), reply_image=Mock(return_value=True))
        plugin = Plugin()
        plugin.on_load(ctx)
        self.addCleanup(plugin.on_unload, ctx)
        started = threading.Barrier(4)
        attempts = []
        release = threading.Event()
        finished = threading.Event()
        done = []

        @contextmanager
        def generate(prompt, current, base_url=None):
            attempts.append(base_url)
            started.wait(timeout=3)
            if base_url != 'https://api.example.com':
                release.wait(timeout=3)
                if not current():
                    done.append(base_url)
                    if len(done) == 3:
                        finished.set()
                    raise GenerationCancelled()
            yield SimpleNamespace(path=self.root / 'image.jpg', retain=lambda: None)

        plugin.client.generate = generate
        plugin._generate(Message('/生图 测试', 'one', session_id='ok'), '测试', ctx)
        ctx.reply_image.assert_called_once()
        self.assertEqual(len(attempts), 4)
        release.set()
        self.assertTrue(finished.wait(3))
        self.assertEqual(len(attempts), 4)
        ctx.reply.assert_not_called()

    def test_concurrent_requests_and_unload_cancel_pending(self):
        ctx = SimpleNamespace(project_root=self.root, members=SimpleNamespace(groups=set()),
                              plugin_is_active=lambda name: True, _service=SimpleNamespace(session_is_current=lambda s: True),
                              logger=Mock(), reply=Mock(), reply_image=Mock(return_value=True))
        plugin = Plugin()
        plugin.on_load(ctx)
        self.addCleanup(plugin.on_unload, ctx)
        entered = threading.Barrier(6)
        release = threading.Event()
        finished = threading.Event()
        seen = []
        seen_lock = threading.Lock()

        @contextmanager
        def generate(prompt, current, base_url=None):
            with seen_lock:
                first = prompt not in seen
                if first:
                    seen.append(prompt)
            if first:
                entered.wait(timeout=3)
            release.wait(timeout=3)
            if not current():
                finished.set()
                raise GenerationCancelled()
            yield SimpleNamespace(path=self.root / 'unused', retain=lambda: None)

        plugin.client.generate = generate
        for index in range(5):
            plugin.on_message(Message(f'/生图 图{index}', f'user{index}', session_id='ok'), ctx)
        entered.wait(timeout=3)
        for index in range(8):
            plugin.on_message(Message(f'/生图 排队的图{index}', f'queue{index}', session_id='ok'), ctx)
        plugin.on_message(Message('/生图 超出容量', 'overflow', session_id='ok'), ctx)
        self.assertEqual(ctx.reply.call_args.args[1], '生图任务较多，请稍后再试。')
        plugin.on_unload(ctx)
        release.set()
        self.assertTrue(finished.wait(3))
        for worker in plugin.workers:
            worker.join(timeout=3)
        self.assertCountEqual(seen, [f'图{index}' for index in range(5)])
        ctx.reply_image.assert_not_called()

    def test_uncertain_send_does_not_regenerate(self):
        ctx = SimpleNamespace(project_root=self.root, members=SimpleNamespace(groups=set()),
                              plugin_is_active=lambda name: True, _service=SimpleNamespace(session_is_current=lambda s: True),
                              logger=Mock(), reply=Mock(), reply_image=Mock(side_effect=RuntimeError('unknown send state')))
        plugin = Plugin()
        plugin.on_load(ctx)
        self.addCleanup(plugin.on_unload, ctx)
        retained = Mock()
        generation = Mock()

        @contextmanager
        def generate(prompt, current, base_url=None):
            generation()
            yield SimpleNamespace(path=self.root / 'image.jpg', retain=retained)

        plugin.client.generate = generate
        plugin._generate(Message('/生图 测试', 'one', session_id='ok'), '测试', ctx)
        self.assertGreaterEqual(generation.call_count, 1)
        self.assertLessEqual(generation.call_count, 4)
        ctx.reply_image.assert_called_once()
        retained.assert_called_once()

    def test_five_requests_reach_all_four_channels_in_parallel(self):
        ctx = SimpleNamespace(project_root=self.root, members=SimpleNamespace(groups=set()),
                              plugin_is_active=lambda name: True, _service=SimpleNamespace(session_is_current=lambda s: True),
                              logger=Mock(), reply=Mock(), reply_image=Mock(return_value=True))
        plugin = Plugin()
        plugin.on_load(ctx)
        self.addCleanup(plugin.on_unload, ctx)
        started = threading.Barrier(21)
        release = threading.Event()
        calls = []
        call_lock = threading.Lock()

        @contextmanager
        def generate(prompt, current, base_url=None):
            with call_lock:
                calls.append((prompt, base_url))
            started.wait(timeout=5)
            release.wait(timeout=5)
            if not current():
                raise GenerationCancelled()
            yield SimpleNamespace(path=self.root / 'image.jpg', retain=lambda: None)

        plugin.client.generate = generate
        try:
            for index in range(5):
                plugin.on_message(Message(f'/生图 图{index}', f'user{index}', session_id='ok'), ctx)
            started.wait(timeout=5)
            self.assertEqual(len(calls), 20)
            for index in range(5):
                self.assertCountEqual([base for prompt, base in calls if prompt == f'图{index}'],
                                      plugin.client.channels())
        finally:
            plugin.on_unload(ctx)
            release.set()
            for worker in plugin.workers:
                worker.join(timeout=5)
        ctx.reply_image.assert_not_called()

    def test_group_reports_start(self):
        ctx = SimpleNamespace(project_root=self.root, members=SimpleNamespace(groups={'group@chatroom'}),
                              plugin_is_active=lambda name: True, _service=SimpleNamespace(session_is_current=lambda s: True),
                              logger=Mock(), reply=Mock(), reply_image=Mock(return_value=True))
        plugin = Plugin()
        plugin.on_load(ctx)
        self.addCleanup(plugin.on_unload, ctx)
        @contextmanager
        def generate(prompt, current, base_url=None):
            yield SimpleNamespace(path=self.root / 'image.jpg', retain=lambda: None)

        plugin.client.generate = generate
        plugin._generate(Message('/生图 测试', 'group@chatroom', is_group=True, session_id='ok'), '测试', ctx)
        self.assertEqual([call.args[1] for call in ctx.reply.call_args_list], ['开始生图，请稍候。'])
        ctx.reply_image.assert_called_once()


if __name__ == '__main__':
    unittest.main()
