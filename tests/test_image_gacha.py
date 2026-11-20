"""Use synthetic catalog values and fake generation without contacting accounts."""
from contextlib import contextmanager
from io import BytesIO
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from plugins.image_gacha import Plugin
from plugins.image_gacha.catalog import PromptCatalog
from plugins.image_generation.client import GenerationCancelled, GenerationError
from wxbot_framework.sdk import Message


class Response(BytesIO):
    def __init__(self, data):
        super().__init__(data)
        self.headers = {'Content-Length': str(len(data))}


class ImageGachaTests(unittest.TestCase):
    def test_catalog_passes_original_string_and_caches_without_writing_files(self):
        catalog = PromptCatalog()
        catalog.opener = Mock()
        original = '  synthetic\n' + 'x' * 7500 + '  '
        catalog.opener.open.return_value = Response(json.dumps([original]).encode())
        self.assertEqual(catalog.pick(current=lambda: True), original)
        self.assertEqual(catalog.pick(current=lambda: True), original)
        catalog.opener.open.assert_called_once()
        request = catalog.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://image.52131415.xyz/catalog.json')
        self.assertEqual(request.get_header('Referer'), 'https://image.52131415.xyz/')

    def test_invalid_catalog_never_exposes_response(self):
        for value in ([], {}, [''], [None], ['synthetic\0'], ['x' * 16001]):
            catalog = PromptCatalog()
            catalog.opener = Mock()
            catalog.opener.open.return_value = Response(json.dumps(value).encode())
            with self.assertRaisesRegex(GenerationError, '^抽卡目录格式不受支持$'):
                catalog.pick(current=lambda: True)

    def test_connection_change_during_fetch_does_not_publish_catalog(self):
        catalog = PromptCatalog()
        current = [True]

        def load():
            current[0] = False
            return ('synthetic',)

        with patch.object(catalog, '_load', side_effect=load):
            with self.assertRaises(GenerationCancelled):
                catalog.pick(current=lambda: current[0])
        self.assertEqual(catalog._entries, ())

    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        config = root / '.runtime/image_generation.json'
        config.parent.mkdir()
        config.write_text(json.dumps({'base_url': 'https://example.com', 'api_key': 'fixture',
                                      'model': 'fixture'}), encoding='utf-8')
        context = SimpleNamespace(project_root=root, members=SimpleNamespace(groups={'fixture@chatroom'}),
            plugin_is_active=lambda name: name == 'image_gacha',
            _service=SimpleNamespace(session_is_current=lambda session: session == 'fixture'),
            reply=Mock(), reply_image=Mock(return_value=True), logger=Mock())
        plugin = Plugin()
        plugin.on_load(context)
        self.addCleanup(plugin.on_unload, context)
        return plugin, context

    def test_command_generates_from_opaque_value_without_ordinary_generation_plugin(self):
        plugin, context = self.fixture()
        original = '  synthetic\n' + 'x' * 7500 + '  '
        plugin.catalog.pick = Mock(return_value=original)
        delivered = threading.Event()

        @contextmanager
        def generate(prompt, *, current, base_url):
            self.assertEqual(prompt, original)
            self.assertTrue(current())
            yield SimpleNamespace(path=Path(context.project_root) / 'synthetic.jpg', retain=Mock())

        def reply_image(*args, **kwargs):
            with kwargs['delivery_guard']():
                delivered.set()
            return True

        plugin.client.generate = generate
        context.reply_image.side_effect = reply_image
        for message in (Message('/抽卡extra', 'fixture'), Message('/抽卡 提示词', 'fixture'),
                        Message('/抽卡', 'fixture', is_self=True),
                        Message('/抽卡', 'fixture', session_id='old'),
                        Message('/抽卡', 'other@chatroom', is_group=True, session_id='fixture')):
            self.assertIsNone(plugin.on_message(message, context))
        self.assertTrue(plugin.on_message(Message('/抽卡', 'fixture', session_id='fixture'), context))
        self.assertTrue(delivered.wait(3))
        plugin.catalog.pick.assert_called_once()
        context.reply_image.assert_called_once()
        context.reply.assert_not_called()
        for call in context.logger.method_calls:
            self.assertNotIn(original, str(call))

    def test_catalog_failure_returns_generic_message_without_generation(self):
        plugin, context = self.fixture()
        plugin.catalog.pick = Mock(side_effect=GenerationError('synthetic-secret'))
        plugin.client.generate = Mock()
        plugin._generate(Message('/抽卡', 'fixture', session_id='fixture'), None, context)
        plugin.client.generate.assert_not_called()
        self.assertEqual(context.reply.call_args.args[1], '抽卡暂时失败，请稍后再试。')
        self.assertNotIn('synthetic-secret', str(context.logger.method_calls))
