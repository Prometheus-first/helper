import hashlib
from pathlib import Path
import queue
import struct
import tempfile
import threading
import unittest

from wxbot_framework.file_metadata import FileStore
from wxbot_framework.video_receiver import VideoReceiver
from wxbot_framework.video_metadata import select_video_download


def box(kind, data):
    return struct.pack('>I4s', len(data) + 8, kind) + data


def video():
    mvhd = b'\0' * 12 + struct.pack('>II', 1000, 2100)
    hdlr = b'\0' * 8 + b'vide'
    return box(b'ftyp', b'isom\0\0\0\0') + box(b'moov', box(b'mvhd', mvhd) + box(b'trak', box(b'mdia', box(b'hdlr', hdlr))))


class VideoReceiverTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parent.parent / '.runtime' / 'tests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.store = FileStore(Path(self.temp.name) / 'videos', 'wxid_receiver_test')
        self.events = queue.Queue()
        self.current = threading.Event()
        self.current.set()
        self.receivers = []
        self.addCleanup(self.close_receivers)

    def close_receivers(self):
        for receiver in self.receivers:
            receiver.close(timeout=2)
            self.assertFalse(receiver.is_alive)

    def receiver(self, download):
        receiver = VideoReceiver(self.store, download, self.events.put, self.current.is_set)
        self.receivers.append(receiver)
        return receiver

    def event(self, body, *, raw=True, checksum=None):
        digest = checksum or hashlib.md5(body).hexdigest()
        raw_fields = (' rawlength="%d" rawmd5="%s" cdnrawvideourl="private-original" '
                      'cdnrawvideoaeskey="%s"' % (len(body), digest, 'a' * 32)) if raw else ''
        xml = ('<msg><videomsg length="%d" md5="%s" cdnvideourl="private-standard" '
               'aeskey="%s" playlength="3"%s/></msg>' % (len(body), digest, 'b' * 32, raw_fields))
        return {'wx_type':43, 'chat_id':'wxid_peer', 'sender_id':'wxid_peer', 'sequence':1,
                'msg':xml, 'content':xml, 'raw_content':xml}

    def test_original_download_is_verified_and_credentials_are_removed(self):
        body = video()
        calls = []
        def download(metadata, destination):
            calls.append(select_video_download(metadata)[4])
            destination.write_bytes(body)
            return True
        receiver = self.receiver(download)
        self.assertTrue(receiver.submit(self.event(body)))
        result = self.events.get(timeout=3)
        self.assertEqual((result['video_status'], result['video_quality'], result['video_size']),
                         ('ready', 'original', len(body)))
        self.assertEqual(Path(result['video_path']).read_bytes(), body)
        self.assertEqual(calls, ['original'])
        self.assertNotIn('raw_content', result)
        self.assertNotIn('private-original', str(result))
        self.assertNotIn('a' * 32, str(result))

    def test_missing_original_selects_standard_and_hash_mismatch_fails(self):
        body = video()
        receiver = self.receiver(lambda metadata, destination: destination.write_bytes(body) > 0)
        self.assertTrue(receiver.submit(self.event(body, raw=False, checksum='f' * 32)))
        result = self.events.get(timeout=3)
        self.assertEqual((result['video_status'], result['video_quality'], result['video_path']),
                         ('failed', 'standard', ''))
        self.assertFalse(list(self.store.directory.glob('*.mp4')))

    def test_standard_video_succeeds_when_original_is_absent(self):
        body = video()
        receiver = self.receiver(lambda metadata, destination: destination.write_bytes(body) > 0)
        self.assertTrue(receiver.submit(self.event(body, raw=False)))
        result = self.events.get(timeout=3)
        self.assertEqual((result['video_status'], result['video_quality']), ('ready', 'standard'))
        self.assertEqual(Path(result['video_path']).read_bytes(), body)

    def test_failed_original_is_not_silently_replaced(self):
        calls = []
        def download(metadata, destination):
            calls.append(select_video_download(metadata)[4])
            return False
        receiver = self.receiver(download)
        self.assertTrue(receiver.submit(self.event(video())))
        result = self.events.get(timeout=3)
        self.assertEqual((result['video_status'], result['video_quality']), ('failed', 'original'))
        self.assertEqual(calls, ['original'])

    def test_invalid_mp4_and_missing_credentials_fail_closed(self):
        body = b'not a valid video'
        receiver = self.receiver(lambda metadata, destination: destination.write_bytes(body) > 0)
        receiver.submit(self.event(body))
        self.assertEqual(self.events.get(timeout=3)['video_status'], 'failed')
        self.assertFalse(receiver.submit({'content':'<msg><videomsg/></msg>'}))
        self.assertEqual(self.events.get(timeout=3)['video_status'], 'failed')


if __name__ == '__main__':
    unittest.main()
