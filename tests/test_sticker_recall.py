"""表情防撤回：合成 GIF、模拟 CDN 与内存后端；不访问微信或网络。"""
from io import BytesIO
from pathlib import Path
import hashlib
import threading
import time
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from plugins.anti_recall import Plugin
from plugins.anti_recall import stickers
from wxbot_framework.backend import TEXT_CAPABILITIES
from wxbot_framework.sdk import Message, PluginContext, ServiceFacade

ROOM, FRIEND = '12345678@chatroom', 'wxid_sticker_friend'


def gif(color='blue'):
    output=BytesIO();Image.new('RGB',(8,8),color).save(output,format='GIF');return output.getvalue()


GIF=gif()
MD5=hashlib.md5(GIF).hexdigest()


def xml(data=GIF, **attributes):
    values={'md5':hashlib.md5(data).hexdigest(),'len':str(len(data)),
            'cdnurl':'http://vweixinf.tc.qq.com/synthetic-sticker'}
    values.update(attributes)
    from xml.sax.saxutils import quoteattr
    return '<msg><emoji '+' '.join(k+'='+quoteattr(v) for k,v in values.items())+'/></msg>'


def original(identifier='11', *, session='current', chat=ROOM, content=None):
    return Message('',chat,FRIEND,type=47,is_group=chat.endswith('@chatroom'),session_id=session,
        data={'native_message_id':identifier,'monitor_only':True,'raw_content':content or xml()})


def recall(identifier='11', *, session='current', chat=ROOM):
    return Message('',chat,type=10002,is_group=chat.endswith('@chatroom'),session_id=session,
        data={'recall':{'message_id':identifier,'chat_id':chat},'monitor_only':True})


class MetadataTests(unittest.TestCase):
    def test_real_shaped_metadata_hides_urls_and_validates_original_hash(self):
        result=stickers.parse_sticker(xml(aeskey='synthetic-key',encrypturl='http://example.invalid/secret'))
        self.assertEqual((result.md5,result.length),(MD5,len(GIF)))
        self.assertNotIn('synthetic-sticker',repr(result))
        self.assertEqual(stickers.parse_sticker(xml(len='0')).length,None)

    def test_untrusted_urls_and_structures_are_rejected(self):
        urls=['file:///C:/private/test.gif','http://localhost/test','http://127.0.0.1/test',
              'http://vweixinf.tc.qq.com.evil.invalid/x','http://user:pass@vweixinf.tc.qq.com/x',
              'http://vweixinf.tc.qq.com:443/x','http://vweixinf.tc.qq.com/x#fragment',
              'https://vweixinf.tc.qq.com\\@127.0.0.1/x','http://vweixinf.tc.qq.com/x\nHeader:bad']
        for url in urls:
            with self.subTest(url=url),self.assertRaises(stickers.StickerError):
                stickers.parse_sticker(xml(cdnurl=url))
        for content in ('<!DOCTYPE msg [<!ENTITY x SYSTEM "file:///private">]><msg/>',
                        '<msg><emoji/><emoji/></msg>', '<msg><nested><emoji/></nested></msg>',
                        '<msg>'+'<x>'*17+'</x>'*17+'</msg>', xml(md5='bad'),
                        xml(len=str(stickers.MAX_SOURCE_BYTES+1)), 'x'*16385):
            with self.subTest(content=content[:20]),self.assertRaises(stickers.StickerError):
                stickers.parse_sticker(content)

    def response(self,data,header=None):
        response=BytesIO(data);response.headers={} if header is None else {'Content-Length':header}
        return response

    def test_download_checks_length_md5_and_session_and_disables_redirects(self):
        opener=Mock();opener.open.return_value=self.response(GIF,str(len(GIF)))
        with patch.object(stickers.urllib.request,'build_opener',return_value=opener) as build:
            self.assertEqual(stickers.download_sticker(stickers.parse_sticker(xml()),lambda:True),GIF)
            self.assertIsInstance(build.call_args.args[0],stickers._NoRedirect)
        for body in (GIF+b'extra',gif('red')):
            opener.open.return_value=self.response(body)
            with patch.object(stickers.urllib.request,'build_opener',return_value=opener),self.assertRaises(stickers.StickerError):
                stickers.download_sticker(stickers.parse_sticker(xml()),lambda:True)
        opener.open.reset_mock()
        with patch.object(stickers.urllib.request,'build_opener',return_value=opener),self.assertRaises(stickers.StickerError):
            stickers.download_sticker(stickers.parse_sticker(xml()),lambda:False)
        opener.open.assert_not_called()
        self.assertIsNone(stickers._NoRedirect().redirect_request(None,None,None,None,None,None))

    def test_download_does_not_echo_url_on_failure_or_oversized_response(self):
        opener=Mock();opener.open.side_effect=OSError('private credential URL')
        with patch.object(stickers.urllib.request,'build_opener',return_value=opener),self.assertRaises(stickers.StickerError) as error:
            stickers.download_sticker(stickers.parse_sticker(xml()),lambda:True)
        self.assertNotIn('private',str(error.exception))
        opener.open.side_effect=None
        opener.open.return_value=self.response(GIF,str(stickers.MAX_SOURCE_BYTES+1))
        with patch.object(stickers.urllib.request,'build_opener',return_value=opener),self.assertRaises(stickers.StickerError):
            stickers.download_sticker(stickers.parse_sticker(xml()),lambda:True)

    def test_original_gif_and_png_conversion_use_valid_native_format(self):
        self.assertEqual(stickers.gif_bytes(GIF,lambda:True),GIF)
        self.assertEqual(stickers.gif_bytes(GIF+b'wechat-trailing-marker;not-another-gif',lambda:True),GIF)
        stream=BytesIO();Image.new('RGB',(8,8),'red').save(stream,format='PNG')
        converted=stickers.gif_bytes(stream.getvalue(),lambda:True)
        with Image.open(BytesIO(converted)) as image:self.assertEqual(image.format,'GIF')
        with self.assertRaises(ValueError):stickers.gif_bytes(b'invalid',lambda:True)
        with self.assertRaises(ValueError):stickers.gif_bytes(GIF[:-5]+b';',lambda:True)


class StickerRecallTests(unittest.TestCase):
    def setUp(self):
        self.backend=Mock(session_id='current');self.backend.send_text.return_value=True
        self.sent=[]
        def send(chat,path):
            p=Path(path);self.sent.append((chat,p.read_bytes(),p));return True
        self.backend.send_emoticon.side_effect=send
        self.service=ServiceFacade(text_delay={'enabled':False});self.service.bind(self.backend)
        self.addCleanup(self.service.close)
        self.ctx=PluginContext('anti_recall',{},self.service,TEXT_CAPABILITIES)
        self.fetch=Mock(return_value=GIF)
        self.patch=patch.object(stickers,'download_sticker',self.fetch);self.patch.start();self.addCleanup(self.patch.stop)
        self.plugin=Plugin();self.plugin.on_load(self.ctx)
        self.addCleanup(self.plugin.on_unload,self.ctx)

    def emit(self,message):self.plugin.on_message(message,self.ctx)

    def drain(self):
        deadline=time.monotonic()+3
        while self.plugin.stickers._queue.unfinished_tasks:
            if time.monotonic()>deadline:self.fail('表情缓存未完成')
            time.sleep(.005)

    def test_original_sticker_reposts_once_and_temporary_file_is_removed(self):
        self.emit(original());self.drain()
        self.assertFalse(self.sent);self.backend.send_text.assert_not_called()
        self.emit(recall());self.emit(recall());self.emit(original());self.drain()
        self.assertEqual([(chat,body) for chat,body,_ in self.sent],[(ROOM,GIF)])
        self.assertFalse(self.sent[0][2].exists())
        self.backend.send_text.assert_not_called();self.fetch.assert_called_once()
        self.assertEqual(dict(self.plugin.originals[('current',ROOM,'11')].message.data),{})

    def test_recall_before_download_completes_waits_and_sends_original(self):
        gate=threading.Event();self.addCleanup(gate.set)
        self.fetch.side_effect=lambda *args: gate.wait(2) and GIF
        self.emit(original());self.emit(recall());self.emit(recall())
        self.assertFalse(self.sent);gate.set();self.drain()
        self.assertEqual(len(self.sent),1);self.backend.send_text.assert_not_called()

    def test_recall_before_original_and_private_chat_are_supported(self):
        self.emit(recall(chat=FRIEND));self.emit(original(chat=FRIEND));self.drain()
        self.assertEqual(self.sent[0][:2],(FRIEND,GIF))

    def test_failed_download_and_evicted_cache_preserve_type_notice(self):
        self.fetch.side_effect=ValueError('private CDN credential')
        self.emit(original());self.emit(recall());self.drain()
        self.backend.send_text.assert_called_once_with(ROOM,'撤回的消息是：[表情]')
        self.assertFalse(self.sent)
        self.fetch.side_effect=None
        self.emit(original('12'));self.drain()
        with self.plugin.stickers._lock:self.plugin.stickers._items.clear()
        self.emit(recall('12'))
        self.assertEqual(self.backend.send_text.call_count,2)

    def test_plain_unicode_and_wechat_inline_faces_remain_verbatim(self):
        text='[微笑][捂脸]😂🐱'
        self.emit(Message(text,ROOM,FRIEND,is_group=True,session_id='current',data={'native_message_id':'22'}))
        self.emit(recall('22'))
        self.backend.send_text.assert_called_once_with(ROOM,'撤回的消息是：'+text)
        self.fetch.assert_not_called()

    def test_unknown_format_or_wrong_room_does_not_send_sticker(self):
        self.emit(original(content='<msg><emoji/></msg>'));self.emit(recall())
        self.backend.send_text.assert_called_once_with(ROOM,'撤回的消息是：[表情]')
        self.emit(original('12'));self.drain();self.emit(recall('12',chat='999@chatroom'))
        self.assertFalse(self.sent)

    def test_expired_connection_blocks_reposts(self):
        self.emit(original());self.drain();self.backend.session_id='new'
        self.emit(recall());self.assertFalse(self.sent)
        self.backend.send_text.assert_not_called()

    def test_unload_cancels_pending_download_and_clears_memory(self):
        gate=threading.Event();started=threading.Event();self.addCleanup(gate.set)
        def fetch(*args):started.set();gate.wait(2);return GIF
        self.fetch.side_effect=fetch
        self.emit(original());self.emit(recall());self.assertTrue(started.wait(1))
        self.plugin.on_unload(self.ctx);gate.set();self.drain()
        self.assertFalse(self.sent);self.backend.send_text.assert_not_called()
        self.assertFalse(self.plugin.originals);self.assertFalse(self.plugin.stickers._items)

    def test_content_cache_reuses_original_without_another_download_and_stays_bounded(self):
        self.emit(original());self.drain();self.emit(original('12'));self.drain()
        self.fetch.assert_called_once()
        with patch.object(stickers,'MAX_CACHE_ITEMS',1):
            self.fetch.return_value=gif('red');self.emit(original('13',content=xml(gif('red'))));self.drain()
        self.assertEqual(len(self.plugin.stickers._items),1)
        self.assertNotIn(MD5,self.plugin.stickers._items)


if __name__=='__main__':unittest.main()
