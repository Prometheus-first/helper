"""本机防撤回开关的账号隔离、重连和重载回归。"""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock
from wxbot_framework.sdk import ServiceFacade, CapabilityError
from wxbot_framework.config import ConfigStore
from wxbot_framework.plugins import PluginManager


def backend(session='one', supported=True):
    return Mock(session_id=session, capabilities={'message.recall.local'} if supported else {'message.recall'})


class LocalRecallSwitchTests(unittest.TestCase):
    def setUp(self):
        self.service = ServiceFacade()
        self.backend = backend()
        self.service.bind(self.backend)

    def test_old_plugin_unload_cannot_disable_new_instance(self):
        old = self.service.enable_local_recall()
        current = self.service.enable_local_recall()
        self.backend.set_local_recall.reset_mock()
        self.service.disable_local_recall(old)
        self.backend.set_local_recall.assert_not_called()
        self.service.disable_local_recall(current)
        self.backend.set_local_recall.assert_called_once_with(False)

    def test_active_local_plugin_is_restored_after_reconnect(self):
        owner = self.service.enable_local_recall()
        self.service.close()
        current = backend('two')
        self.service.bind(current)
        self.service.restore_local_recall()
        current.set_local_recall.assert_called_once_with(True)
        self.service.disable_local_recall(owner)
        self.assertEqual(current.set_local_recall.call_args.args, (False,))

    def test_disabled_plugin_stays_off_after_reconnect(self):
        owner = self.service.enable_local_recall()
        self.service.close()
        self.service.disable_local_recall(owner)
        current = backend('two')
        self.service.bind(current)
        self.service.restore_local_recall()
        current.set_local_recall.assert_not_called()

    def test_unsupported_reconnect_can_release_previous_owner(self):
        owner = self.service.enable_local_recall()
        self.service.close()
        current = backend('two', supported=False)
        self.service.bind(current)
        self.service.disable_local_recall(owner)
        self.service.restore_local_recall()
        current.set_local_recall.assert_not_called()
        with self.assertRaises(CapabilityError):
            self.service.enable_local_recall()

    def test_failed_enable_preserves_the_previous_owner(self):
        owner = self.service.enable_local_recall()
        self.backend.set_local_recall.side_effect = RuntimeError('模拟设置失败')
        with self.assertRaises(RuntimeError):
            self.service.enable_local_recall()
        self.backend.set_local_recall.side_effect = None
        self.backend.set_local_recall.reset_mock()
        self.service.disable_local_recall(owner)
        self.backend.set_local_recall.assert_called_once_with(False)


    def test_plugin_manager_applies_web_enable_reload_and_disable(self):
        project = Path(__file__).resolve().parent.parent
        scratch = project / '.runtime/tests'
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='local-recall-', dir=scratch) as directory:
            root = Path(directory).resolve()
            self.assertTrue(root.is_relative_to(scratch.resolve()))
            plugin = root / 'plugins/anti_recall_local'
            plugin.mkdir(parents=True)
            for name in ('__init__.py', 'plugin.json'):
                shutil.copy2(project / 'plugins/anti_recall_local' / name, plugin / name)
            config = root / 'bot_config.json'
            config.write_text(json.dumps({'backend':'native_text', 'plugins':{}}), encoding='utf-8')
            manager = PluginManager(root / 'plugins', ConfigStore(config), self.service, self.backend.capabilities)
            try:
                manager.reload()
                self.assertFalse(manager.is_active('anti_recall_local'))
                self.backend.set_local_recall.assert_not_called()
                manager.enable('anti_recall_local')
                manager.reload('anti_recall_local')
                self.assertTrue(manager.is_active('anti_recall_local'))
                self.assertFalse(any(call.args == (False,) for call in self.backend.set_local_recall.call_args_list))
                manager.disable('anti_recall_local')
                self.assertFalse(manager.is_active('anti_recall_local'))
                self.assertEqual(self.backend.set_local_recall.call_args.args, (False,))
            finally:
                manager.shutdown()
