import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'native'))

from filehelper_service import ReceivedMediaMessage
from text_service import NativeTextService
from wxbot_framework.backend import TEXT_42011_CAPABILITIES, TEXT_42012_CAPABILITIES
from wxbot_framework.sdk import Message
from wxbot_framework.video_metadata import parse_video_metadata

ACCOUNT = 'wxid_video_account'
SENDER = 'wxid_video_sender'
ROOM = '123456789@chatroom'
XML = ('<msg><videomsg length="123" md5="' + 'a'*32 + '" cdnvideourl="standard-ref" '
       'aeskey="' + 'b'*32 + '" rawlength="456" rawmd5="' + 'c'*32 + '" '
       'cdnrawvideourl="original-ref" cdnrawvideoaeskey="' + 'd'*32 + '"/></msg>')


@unittest.skipUnless(sys.platform == 'win32', '仅针对 Windows 原生消息映射')
class NativeVideoReceiveTests(unittest.TestCase):
    def service(self, allowed=()):
        service = NativeTextService(pid=123, account=ACCOUNT, image_allowlist=allowed)
        service.router.set_account(ACCOUNT)
        service._sender_wechat_name = Mock(return_value='')
        service._video_receiver = Mock()
        service.monitor_handler = Mock()
        return service

    def test_group_video_is_queued_once_and_monitor_contains_no_credentials(self):
        service = self.service((ROOM,))
        message = ReceivedMediaMessage(type=43, sequence=1, sender=ROOM.encode(), receiver=ACCOUNT.encode(),
                                       image_xml=(SENDER + ':\n' + XML).encode())
        self.assertIsNone(service._map_message(message))
        service._video_receiver.submit.assert_called_once()
        event = service._video_receiver.submit.call_args.args[0]
        self.assertEqual((event['chat_id'], event['sender_id'], event['raw_content']), (ROOM, SENDER, XML))
        public = service.monitor_handler.call_args.args[0]
        self.assertEqual(public['video_status'], 'pending')
        self.assertTrue(public['monitor_only'])
        self.assertNotIn('original-ref', str(public))
        self.assertNotIn('d' * 32, str(public))

    def test_disallowed_video_is_not_downloaded(self):
        service = self.service(('other@chatroom',))
        message = ReceivedMediaMessage(type=43, sequence=1, sender=ROOM.encode(), receiver=ACCOUNT.encode(),
                                       image_xml=(SENDER + ':\n' + XML).encode())
        event = service._map_message(message)
        self.assertEqual(event['video_status'], 'skipped')
        self.assertNotIn('original-ref', str(event))
        service._video_receiver.submit.assert_not_called()

    def test_download_uses_video_type_and_rechecks_account(self):
        metadata = parse_video_metadata(XML)
        fake = SimpleNamespace(_calls=threading.RLock(), _owns_bridge=True, _stop=threading.Event(),
            wechat_version='4.2.0.12', pid=123, preserve_reports=False,
            _check_account=Mock(), _run=Mock(return_value={'success':True}))
        self.assertTrue(NativeTextService._download_video(fake, metadata, Path('video.mp4')))
        args = fake._run.call_args.kwargs['download_image']
        self.assertEqual((args['image_type'], args['filekey']), (4, 'original-ref'))
        self.assertEqual(fake._check_account.call_count, 2)
        fake._run.reset_mock()
        fake.wechat_version = '4.2.0.11'
        self.assertFalse(NativeTextService._download_video(fake, metadata, Path('video.mp4')))
        fake._run.assert_not_called()

    def test_only_verified_version_advertises_video_receive(self):
        self.assertIn('video.receive', TEXT_42012_CAPABILITIES)
        self.assertNotIn('video.receive', TEXT_42011_CAPABILITIES)
        self.assertEqual(Message('', 'filehelper', data={'video_path':'video.mp4'}).video_path, 'video.mp4')


if __name__ == '__main__':
    unittest.main()
