"""新版只开放已完成验证的桥接能力。"""
import unittest
from types import SimpleNamespace
from wxbot_framework.backend import NativeTextBackend, TEXT_42012_CAPABILITIES

class Capability42012Tests(unittest.TestCase):
    def test_unverified_features_are_not_advertised(self):
        backend = object.__new__(NativeTextBackend)
        backend.raw = SimpleNamespace(wechat_version='4.2.0.12')
        self.assertEqual(backend.capabilities, TEXT_42012_CAPABILITIES)
        for feature in ('text.send', 'text.receive', 'image.send', 'image.receive',
                        'file.send', 'file.receive', 'voice.send', 'voice.receive',
                        'message.recall', 'group.members.read', 'group.text.mention', 'group.events',
                        'friend.request.receive', 'friend.request.accept', 'contact.read'):
            self.assertIn(feature, backend.capabilities)
        for feature in ('emoticon.send',):
            self.assertNotIn(feature, backend.capabilities)
