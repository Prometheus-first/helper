"""Capabilities must reflect the connected WeChat version."""
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

NATIVE = Path(__file__).resolve().parents[1] / 'native'
if str(NATIVE) not in sys.path:
    sys.path.insert(0, str(NATIVE))
from text_service import NativeTextService


class MomentsCapabilitiesTests(unittest.TestCase):
    def test_42011_does_not_advertise_unverified_interaction(self):
        service = NativeTextService(pid=123)
        service._owns_bridge = True
        service._channel = object()
        service._check_account = Mock()
        with patch('moments_transport.moments_capabilities', return_value={'inspect_supported': True}), \
             patch('moments_interaction_transport.interaction_capability', return_value=True):
            service.wechat_version = '4.2.0.11'
            self.assertFalse(service.moments_capabilities()['interaction_supported'])
            service.wechat_version = '4.1.15.56'
            self.assertTrue(service.moments_capabilities()['interaction_supported'])
