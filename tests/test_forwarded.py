"""合并转发：纯合成记录，不连接微信或下载网络资源。"""
import base64
import hashlib
import json
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock
from xml.sax.saxutils import escape

from wxbot_framework.forwarded import ForwardedError, parse_forwarded, MAX_FORWARD_XML_BYTES
from wxbot_framework.forwarded_receiver import ForwardedReceiver
from wxbot_framework.image_metadata import ImageStore
from wxbot_framework.message_monitor import MessageBuffer
from wxbot_framework.sdk import Message

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT, FRIEND, ROOM = 'wxid_forward_robot', 'wxid_forward_friend', '12345678@chatroom'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aWZkAAAAASUVORK5CYII=')


def text_item(text='普通文字', name='群友甲'):
    return ('<dataitem datatype="1"><sourcename>'+escape(name)+'</sourcename>'
            '<sourcetime>2026-01-02 10:38</sourcetime><datadesc>'+escape(text)+'</datadesc>'
            '<dataitemsource><hashusername>opaque-not-a-wxid</hashusername></dataitemsource></dataitem>')


def image_item(*, key=True, digest=None):
    return ('<dataitem datatype="2"><sourcename>群友乙</sourcename><sourcetime>2026-01-02 10:39</sourcetime>'
            '<datafmt>jpg</datafmt><datasize>'+str(len(PNG))+'</datasize><fullmd5>'+
            (digest or hashlib.md5(PNG).hexdigest())+'</fullmd5><cdndataurl>fixture-image-reference</cdndataurl>'
            '<cdndatakey>'+('a'*32 if key else '')+'</cdndatakey></dataitem>')


def forward_xml(items=None):
    if items is None:
        items = [text_item(), image_item()]
    inner = '<recordinfo><datalist count="'+str(len(items))+'">'+''.join(items)+'</datalist></recordinfo>'
    return '<msg><appmsg><type>19</type><title>合成转发记录</title><recorditem>'+escape(inner)+'</recorditem></appmsg></msg>'


def event(xml=None):
    return {'wx_type':49, 'msg':xml or forward_xml(), 'raw_content':xml or forward_xml(),
            'chat_id':ROOM, 'room_wxid':ROOM, 'sender_id':FRIEND, 'sequence':42,
            'native_receiver':ACCOUNT, 'is_self':False}


class ParserTests(unittest.TestCase):
    def test_text_images_original_author_and_time_without_exposing_credentials(self):
        parsed = parse_forwarded(forward_xml())
        public = parsed.public()
        self.assertEqual([x.kind for x in parsed.items], ['text','image'])
        self.assertEqual(public['forwarded_records'][0]['sender_name'], '群友甲')
        self.assertEqual(public['forwarded_records'][0]['source_time'], '2026-01-02 10:38')
        self.assertNotIn('sender_id', public['forwarded_records'][0])
        serialized = json.dumps(public, ensure_ascii=False) + repr(parsed)
        for secret in ('fixture-image-reference', 'a'*32, 'opaque-not-a-wxid'):
            self.assertNotIn(secret, serialized)

    def test_ordinary_appmsg_and_missing_embedded_record(self):
        self.assertIsNone(parse_forwarded('<msg><appmsg><type>6</type></appmsg></msg>'))
        with self.assertRaises(ForwardedError):
            parse_forwarded('<msg><appmsg><type>19</type></appmsg></msg>')

    def test_large_record_above_old_limit_and_malformed_limits(self):
        parsed = parse_forwarded(forward_xml([text_item('长文本'*6000)]))
        self.assertEqual(len(parsed.items[0].text), 18000)
        for content in ('', '\0', '<msg>', '<!DOCTYPE msg [<!ENTITY x "boom">]><msg/>',
                        'x'*(MAX_FORWARD_XML_BYTES+1)):
            with self.subTest(kind=content[:20]), self.assertRaises(ForwardedError):
                parse_forwarded(content)
        inner = '<!DOCTYPE recordinfo [<!ENTITY x "boom">]><recordinfo><datalist/></recordinfo>'
        with self.assertRaises(ForwardedError):
            parse_forwarded('<msg><appmsg><type>19</type><recorditem>'+escape(inner)+'</recorditem></appmsg></msg>')

    def test_limits_preserve_text_and_mark_unavailable_images(self):
        parsed = parse_forwarded(forward_xml([image_item()]*17 + [text_item()]))
        self.assertIsNone(parsed.items[16].image)
        self.assertEqual(parsed.items[-1].text, '普通文字')
        self.assertIsNone(parse_forwarded(forward_xml([image_item(key=False)])).items[0].image)
        many = parse_forwarded(forward_xml([text_item('x')]*205))
        self.assertEqual(len(many.items), 200)
        self.assertTrue(many.truncated)


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ImageStore(Path(self.temp.name).resolve(), ACCOUNT)
        self.events = queue.Queue()
        self.current = threading.Event()
        self.current.set()
        self.receivers = []

    def tearDown(self):
        for receiver in self.receivers:
            receiver.close(2)
        self.temp.cleanup()

    def receiver(self, download, deliver=None):
        receiver = ForwardedReceiver(self.store, download, deliver or self.events.put, self.current.is_set)
        self.receivers.append(receiver)
        return receiver

    def test_full_image_saved_by_signature_and_no_raw_credentials_delivered(self):
        def download(metadata, destination):
            self.assertEqual(metadata.cdnbigimgurl, 'fixture-image-reference')
            destination.write_bytes(PNG)
            return True
        receiver = self.receiver(download)
        raw = event()
        self.assertTrue(receiver.submit(raw, parse_forwarded(raw['msg'])))
        result = self.events.get(timeout=2)
        picture = result['forwarded_records'][1]
        self.assertEqual(picture['image_status'], 'ready')
        self.assertEqual(Path(picture['image_path']).suffix, '.png')
        self.assertEqual(Path(picture['image_path']).read_bytes(), PNG)
        for field in ('raw_content','msg','content'):
            self.assertNotIn(field, result)
        self.assertNotIn('fixture-image-reference', json.dumps(result))

    def test_one_failure_does_not_hide_text_or_next_image(self):
        def download(metadata, destination):
            destination.write_bytes(PNG)
            return True
        raw = event(forward_xml([image_item(digest='0'*32), text_item('保留正文'), image_item()]))
        receiver = self.receiver(download)
        receiver.submit(raw, parse_forwarded(raw['msg']))
        result = self.events.get(timeout=2)
        self.assertEqual(result['forwarded_status'], 'partial')
        self.assertEqual([x.get('image_status') for x in result['forwarded_records']], ['failed',None,'ready'])
        self.assertEqual(result['forwarded_records'][1]['text'], '保留正文')

    def test_cancel_during_download_removes_files_and_never_delivers_old_account(self):
        entered, release = threading.Event(), threading.Event()
        def download(metadata, destination):
            entered.set()
            release.wait(2)
            destination.write_bytes(PNG)
            return True
        receiver = self.receiver(download)
        receiver.submit(event(), parse_forwarded(forward_xml()))
        self.assertTrue(entered.wait(1))
        self.current.clear()
        receiver.close(0)
        self.assertTrue(receiver.is_alive)
        release.set()
        receiver.close(2)
        self.assertTrue(self.events.empty())
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_delivery_failure_removes_saved_images(self):
        called = threading.Event()
        def download(metadata, destination):
            destination.write_bytes(PNG)
            return True
        def deliver(data):
            called.set()
            raise RuntimeError('synthetic delivery failure')
        receiver = self.receiver(download, deliver)
        receiver.submit(event(), parse_forwarded(forward_xml()))
        self.assertTrue(called.wait(1))
        receiver.close(2)
        self.assertEqual(list(self.store.directory.iterdir()), [])


class MonitorTests(unittest.TestCase):
    def test_expanded_records_keep_forwarder_and_do_not_forge_author_wxid(self):
        data = dict(event(), **parse_forwarded(forward_xml()).public())
        buffer = MessageBuffer(clock=lambda:10)
        buffer.append(Message.from_native(11046,data,'session'),account=ACCOUNT,sender_name='转发者')
        rows = buffer.tail()['rows']
        self.assertEqual(len(rows),3)
        self.assertEqual(rows[1]['sender_name'],'群友甲')
        self.assertEqual(rows[1]['sender_id'],'')
        self.assertEqual(rows[1]['forwarded_by_id'],FRIEND)
        self.assertEqual(rows[1]['chat_id'],ROOM)
        serialized = json.dumps(rows)
        for secret in ('fixture-image-reference','a'*32,'raw_content','image_path'):
            self.assertNotIn(secret,serialized)

    def test_image_status_updates_use_same_id_and_advance_cursor(self):
        data = dict(event(), **parse_forwarded(forward_xml()).public())
        buffer = MessageBuffer(clock=lambda:10)
        message = Message.from_native(11046,data,'session')
        buffer.append(message, account=ACCOUNT)
        initial = buffer.tail()
        data['forwarded_records'][1].update(image_status='ready',image_path='C:/private/image.png')
        buffer.clock = lambda:20
        buffer.append(Message.from_native(11046,data,'session'),account=ACCOUNT)
        changed = buffer.tail(initial['cursor'])
        self.assertEqual(len(changed['rows']),1)
        self.assertEqual(changed['rows'][0]['id'],initial['rows'][2]['id'])
        self.assertEqual(changed['rows'][0]['content'],'[图片] 已保存')
        self.assertEqual(changed['rows'][0]['timestamp'],10)
        self.assertGreater(changed['cursor'],initial['cursor'])
        self.assertEqual(len(buffer.tail()['rows']),3)
        buffer.append(Message.from_native(11046,data,'session'),account=ACCOUNT)
        self.assertEqual(buffer.tail(changed['cursor'])['rows'],[])


@unittest.skipUnless(sys.platform=='win32','Windows 原生映射，模拟接收')
class NativeMappingTests(unittest.TestCase):
    def test_forwarded_reaches_monitor_but_not_ordinary_file_receiver(self):
        sys.path.insert(0,str(ROOT/'native'))
        from filehelper_service import ReceivedMediaMessageV4
        from text_service import NativeTextService
        service=NativeTextService(pid=123,account=ACCOUNT)
        service.router.set_account(ACCOUNT)
        service._sender_wechat_name=Mock(return_value='转发者')
        service._forwarded_receiver,service._file_receiver=Mock(),Mock()
        service.monitor_handler=Mock()
        raw=ReceivedMediaMessageV4(type=49,sequence=42,sender=FRIEND.encode(),receiver=ACCOUNT.encode(),image_xml=forward_xml().encode())
        self.assertIsNone(service._map_message(raw))
        data=service.monitor_handler.call_args.args[0]
        self.assertTrue(data['monitor_only'])
        self.assertEqual(len(data['forwarded_records']),2)
        self.assertEqual(Message.from_native(11046,data,'test').text,'')
        service._forwarded_receiver.submit.assert_called_once()
        service._file_receiver.submit.assert_not_called()
        service.image_allowlist=frozenset(('filehelper',))
        service._forwarded_receiver.reset_mock()
        service._map_message(raw)
        service._forwarded_receiver.submit.assert_not_called()
        self.assertEqual(service.monitor_handler.call_args.args[0]['forwarded_records'][1]['image_status'],'skipped')

    def test_missing_record_and_overlong_payload_are_visible_without_file_download(self):
        sys.path.insert(0,str(ROOT/'native'))
        from filehelper_service import ReceivedMediaMessageV4
        from text_service import NativeTextService
        service=NativeTextService(pid=123,account=ACCOUNT)
        service.router.set_account(ACCOUNT)
        service._sender_wechat_name=Mock(return_value='转发者')
        service.monitor_handler=Mock()
        missing='<msg><appmsg><type>19</type></appmsg></msg>'
        raw=ReceivedMediaMessageV4(type=49,sequence=1,sender=FRIEND.encode(),receiver=ACCOUNT.encode(),image_xml=missing.encode())
        service._map_message(raw)
        data=service.monitor_handler.call_args.args[0]
        self.assertEqual(data['forwarded_status'],'failed')
        buffer=MessageBuffer()
        buffer.append(Message.from_native(11046,data,'missing'),account=ACCOUNT)
        self.assertIn('无法解析',buffer.tail()['rows'][0]['content'])
        raw=ReceivedMediaMessageV4(type=49,sequence=2,reserved=1,sender=FRIEND.encode(),receiver=ACCOUNT.encode())
        service._map_message(raw)
        data=service.monitor_handler.call_args.args[0]
        buffer.append(Message.from_native(11046,data,'missing'),account=ACCOUNT)
        self.assertIn('内容过长或不可读',buffer.tail()['rows'][-1]['content'])


if __name__=='__main__':
    unittest.main()
