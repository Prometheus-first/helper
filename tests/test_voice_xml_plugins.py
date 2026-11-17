"""语音 XML 投递给声明接收类型 34 的插件，监控只保存显示字段。"""
from dataclasses import replace
import unittest
import threading
from types import SimpleNamespace
from unittest.mock import Mock

from wxbot_framework.message_monitor import MessageBuffer
from wxbot_framework.runtime import BotApplication
from wxbot_framework.plugins import PluginManager
from wxbot_framework.sdk import Message


XML = '<msg><voicemsg aeskey="fixture-key" voiceurl="fixture-cdn-token" clientmsgid="fixture-client"/></msg>'
ROOM = '12345678@chatroom'


class VoiceXmlPluginTests(unittest.TestCase):
    def setUp(self):
        self.app = BotApplication.__new__(BotApplication)
        self.app.config = {'chat_allowlist': [ROOM]}
        self.app._with_group_role = lambda message: message
        self.buffer = MessageBuffer()
        self.app.message_observer = self.buffer.append
        self.app.plugins = Mock()
        self.message = Message.from_native(11046, {
            'wx_type': 34, 'chat_id': ROOM, 'room_wxid': ROOM, 'sender_id': 'wxid_fixture_sender',
            'native_message_id': '81', 'monitor_only': True, 'dispatch_to_plugins': True,
            'voice_status': 'ready', 'voice_xml': XML, 'raw_content': XML}, 'fixture-session')

    def test_ready_and_failed_audio_deliver_complete_xml_to_normal_plugins(self):
        for status in ('ready', 'failed', 'unavailable'):
            with self.subTest(status=status):
                self.app.plugins.reset_mock()
                message = replace(self.message, data=dict(self.message.data, voice_status=status))
                self.app._emit(message)
                if status == 'ready':
                    self.app.plugins.dispatch.assert_called_once_with(message)
                else:
                    self.app.plugins.dispatch.assert_any_call(message, required='voice.xml.receive')
                    self.assertTrue(all(call.kwargs for call in self.app.plugins.dispatch.call_args_list))
                self.assertEqual(message.data['raw_content'], XML)
                self.assertEqual(message.data['voice_xml'], XML)
                self.assertEqual(message.text, '')
        visible = repr(self.buffer.tail())
        self.assertNotIn('fixture-key', visible)
        self.assertNotIn('fixture-cdn-token', visible)

    def test_pending_self_and_already_delivered_events_only_reach_recall_plugins(self):
        cases = [replace(self.message, data=dict(self.message.data, voice_status='pending')),
                 replace(self.message, is_self=True),
                 replace(self.message, data=dict(self.message.data, bot_generated=True)),
                 replace(self.message, data=dict(self.message.data, dispatch_to_plugins=False))]
        for message in cases:
            with self.subTest(data=dict(message.data)):
                self.app.plugins.reset_mock()
                self.app._emit(message)
                calls = self.app.plugins.dispatch.call_args_list
                self.assertTrue(calls)
                self.assertTrue(all('only' in call.kwargs for call in calls))

    def test_metadata_only_delivery_requires_an_explicit_xml_subscription(self):
        message = replace(self.message, data=dict(self.message.data, voice_status='failed',
                          dispatch_to_plugins=False, voice_xml_only=True))
        self.app._emit(message)
        self.app.plugins.dispatch.assert_any_call(message, required='voice.xml.receive')
        self.assertTrue(all(call.kwargs for call in self.app.plugins.dispatch.call_args_list))

        manager = PluginManager.__new__(PluginManager)
        manager._lock = threading.RLock()
        legacy = SimpleNamespace(spec=SimpleNamespace(name='audio', api='v1',
            message_types={34}, requires={'voice.receive'}), enqueue=Mock())
        xml = SimpleNamespace(spec=SimpleNamespace(name='metadata', api='v1',
            message_types={34}, requires={'voice.xml.receive'}), enqueue=Mock())
        manager.records = {'audio': legacy, 'metadata': xml}
        manager.dispatch(message, required='voice.xml.receive')
        legacy.enqueue.assert_not_called()
        xml.enqueue.assert_called_once_with(message)

    def test_disallowed_chat_does_not_deliver_xml_to_any_plugin(self):
        self.app._emit(replace(self.message, chat_id='99999999@chatroom'))
        self.app.plugins.dispatch.assert_not_called()
