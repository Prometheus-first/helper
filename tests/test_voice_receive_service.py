"""语音接收 ABI 与路由；只使用模拟结构，不连接微信或发送音频。"""
import ctypes as c
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'native'))
from filehelper_service import ReceivedMediaMessageV5, ReceivedMediaMessageV6
from text_service import NativeTextService

ACCOUNT, FRIEND, ROOM = 'wxid_voice_receive_self', 'wxid_voice_receive_friend', '123456789@chatroom'
XML = b'<msg><voicemsg voicelength="20" length="15" endflag="1"/></msg>'
SILK = b'\x02#!SILK_V3\x03\0a\0b'  # 包结构测试，不是可播放录音。


class VoiceReceiveServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = NativeTextService(pid=123, account=ACCOUNT, preserve_reports=False)
        self.service.router.set_account(ACCOUNT)
        self.service._voice_receiver = Mock()
        self.service._sender_wechat_name = Mock(return_value=None)
        self.delivered = []
        self.service.msg_handlers.append(lambda pid, protocol, data, service: self.delivered.append(data))

    def incoming(self, *, group=False):
        raw = ReceivedMediaMessageV6(type=34, sequence=1, native_message_id=123456,
                                    sender=(ROOM if group else FRIEND).encode(), receiver=ACCOUNT.encode())
        raw.size = c.sizeof(raw)
        body = FRIEND.encode() + b':\n' + XML if group else XML
        offset = c.sizeof(ReceivedMediaMessageV6) - 8  # explicitly tested append-only ABI
        self.assertEqual(offset, ReceivedMediaMessageV6.voice_offset.offset)
        raw.voice_offset, raw.voice_size = len(body) + 1, len(SILK)
        c.memmove(c.addressof(raw) + ReceivedMediaMessageV6.image_xml.offset,
                  body + b'\0' + SILK, len(body) + 1 + len(SILK))
        return raw

    def test_append_only_v6_preserves_v5_fields(self):
        self.assertEqual(c.sizeof(ReceivedMediaMessageV5), 267560)
        self.assertEqual(c.sizeof(ReceivedMediaMessageV6), 267568)
        self.assertEqual(ReceivedMediaMessageV6.voice_offset.offset, 267560)
        self.assertEqual(ReceivedMediaMessageV6.native_message_id.offset, 267552)
        self.assertEqual(NativeTextService._poll_export, b'WxPollMediaMessageV7')

    def test_binary_nuls_are_preserved_and_xml_is_not_exposed(self):
        raw = self.incoming()
        self.assertIsNone(self.service._map_message(raw))
        result = self.delivered[0]
        data, payload = self.service._voice_receiver.submit.call_args.args
        self.assertEqual(payload, SILK)
        self.assertIn('voicemsg', data['raw_content'])
        self.assertEqual(data['native_message_id'], '123456')
        self.assertEqual(result['voice_status'], 'pending')
        self.assertTrue(result['monitor_only'])
        for field in ('raw_content', 'msg', 'content'):
            self.assertEqual(result[field], '')

    def test_group_voice_keeps_conversation_and_real_sender(self):
        self.assertIsNone(self.service._map_message(self.incoming(group=True)))
        result = self.delivered[0]
        data, payload = self.service._voice_receiver.submit.call_args.args
        self.assertEqual((data['chat_id'], data['sender_id']), (ROOM, FRIEND))
        self.assertEqual(result['room_wxid'], ROOM)
        self.assertEqual(payload, SILK)

    def test_outside_media_allowlist_does_not_cache(self):
        self.service.image_allowlist = frozenset({ROOM})
        result = self.service._map_message(self.incoming())
        self.assertEqual(result['voice_status'], 'skipped')
        self.service._voice_receiver.submit.assert_not_called()

    def test_invalid_binary_ranges_return_no_payload(self):
        for offset, size in ((0, 10), (1, 10), (262143, 2), (100, 262144), (100, 0)):
            with self.subTest(offset=offset, size=size):
                raw = self.incoming()
                raw.voice_offset, raw.voice_size = offset, size
                self.assertEqual(self.service._voice_payload(raw), b'')
        self.assertEqual(self.service._voice_payload(ReceivedMediaMessageV5()), b'')

    def test_pending_is_delivered_before_synchronous_input_failure(self):
        self.service._voice_receiver.submit.side_effect = lambda data, payload: self.service._deliver_voice(
            dict(data, voice_status='failed', voice_path=''))
        self.assertIsNone(self.service._map_message(self.incoming()))
        self.assertEqual([row['voice_status'] for row in self.delivered], ['pending', 'failed'])

    def test_receiver_delivery_stops_with_the_service(self):
        callback = Mock()
        self.service.msg_handlers.append(callback)
        data = {'voice_status': 'ready', 'voice_path': 'offline.silk'}
        self.service._deliver_voice(data)
        callback.assert_called_once_with(123, 11046, data, self.service)
        self.service._stop.set()
        self.service._deliver_voice(data)
        callback.assert_called_once()

    def test_stop_preserves_bridge_until_voice_worker_has_finished(self):
        self.service._voice_receiver.is_alive = True
        with self.assertRaisesRegex(RuntimeError, '语音接收缓存尚未结束'):
            self.service.stop()
        self.service._voice_receiver.close.assert_called_once_with(timeout=30)
        self.assertIsNotNone(self.service._voice_receiver)


if __name__ == '__main__':
    unittest.main()
