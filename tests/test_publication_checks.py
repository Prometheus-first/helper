"""Publication boundaries using generated synthetic data only."""
import json
from pathlib import Path
import tempfile
import unittest

from tools.check_publication import ALLOWED_DLLS, inspect


class PublicationChecksTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='helper-publication-synthetic-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in ('bot_config','members_config','llm_config','tts_config'):
            (self.root / (name + '.example.json')).write_text('{}', encoding='utf-8')

    def test_only_exact_distributed_bridge_paths_are_allowed(self):
        for name in ALLOWED_DLLS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'synthetic bridge bytes')
        self.assertTrue(inspect(self.root)['passed'])
        unknown = self.root / 'native/build_article/another_bridge.dll'
        unknown.write_bytes(b'synthetic unexpected bridge')
        self.assertIn({'file':'native/build_article/another_bridge.dll','kind':'unexpected_binary'}, inspect(self.root)['findings'])

    def test_linux_build_outputs_and_core_dumps_are_not_release_files(self):
        for name in ('native/linux/stale.so', 'core', 'core.1234', 'wechat.core'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'synthetic diagnostic artifact')
        findings = inspect(self.root)['findings']
        for name in ('native/linux/stale.so', 'core', 'core.1234', 'wechat.core'):
            self.assertIn({'file': name, 'kind': 'unnecessary_or_private_artifact'}, findings)

    def test_known_private_markers_in_native_utf16_are_reported_without_values(self):
        marker = 'synthetic-sensitive-owner'
        path = self.root / 'native/build_article/article_bridge.dll'
        path.parent.mkdir(parents=True)
        path.write_bytes(marker.encode('utf-16le'))
        result = inspect(self.root, (marker,))
        self.assertIn({'file':'native/build_article/article_bridge.dll','kind':'known_private_marker'}, result['findings'])
        self.assertNotIn(marker, json.dumps(result))

    def test_configuration_bindings_and_credentials_are_rejected(self):
        (self.root / 'bot_config.json').write_text(json.dumps({'wechat_account':'wxid_synthetic_owner'}), encoding='utf-8')
        (self.root / 'llm_config.json').write_text(json.dumps({'api_key':'synthetic-secret'}), encoding='utf-8')
        kinds = {item['kind'] for item in inspect(self.root)['findings']}
        self.assertIn('nonempty_account_or_chat_binding', kinds)
        self.assertIn('nonempty_api_key', kinds)

    def test_logs_and_local_media_are_excluded_from_a_release(self):
        path = self.root / '.runtime/articles/synthetic.jpg'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'synthetic image placeholder')
        kinds = {item['kind'] for item in inspect(self.root)['findings']}
        self.assertIn('runtime_or_legacy_directory', kinds)
        self.assertIn('unnecessary_or_private_artifact', kinds)


if __name__ == '__main__':
    unittest.main()
