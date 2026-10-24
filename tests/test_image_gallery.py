"""图库请求边界、凭据隔离、连接取消和临时文件生命周期；不连接外网或微信。"""
from contextlib import contextmanager, nullcontext
from io import BytesIO
import json
import logging
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

from PIL import Image, PngImagePlugin

from plugins.image_gallery import Plugin
from plugins.image_gallery.source import GalleryCancelled, GalleryClient, GalleryError, ImageLease, MAX_JSON_BYTES
from wxbot_framework.sdk import Message


def png():
    data=BytesIO();metadata=PngImagePlugin.PngInfo();metadata.add_text('private','test-metadata')
    Image.new('RGB',(24,32),'pink').save(data,'PNG',pnginfo=metadata)
    return data.getvalue()


class Response(BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers={'Content-Length':str(len(body)),**(headers or {})}


def payload(value):
    return Response(json.dumps(value).encode('utf-8'))


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory=Path(self.tmp.name)/'gallery'

    def client(self, options=None, responses=()):
        client=GalleryClient(self.directory,options or {})
        client.opener=Mock()
        client.opener.open.side_effect=list(responses)
        return client

    def test_builtin_download_validates_and_strips_metadata_then_cleans_file(self):
        client=self.client(responses=[payload({'code':200,'data':'https://images.xxapi.cn/picture.png'}),Response(png())])
        with client.fetch() as image:
            path=image.path
            self.assertTrue(path.is_file())
            with Image.open(path) as decoded:
                self.assertEqual(decoded.format,'JPEG')
                self.assertEqual(decoded.size,(24,32))
                self.assertNotIn('private',decoded.info)
            self.assertNotIn(b'test-metadata',path.read_bytes())
        self.assertFalse(path.exists())
        self.assertEqual(list(self.directory.iterdir()),[])

    def test_second_provider_uses_its_envelope_and_nested_url(self):
        client=self.client({'source':'wzapi'},[payload({'status':'success','data':{'wallpaper':'https://p1.toutiaoimg.com/picture'}}),Response(png())])
        with client.fetch() as image:self.assertTrue(image.path.is_file())
        self.assertIn('format=json',client.opener.open.call_args_list[0].args[0].full_url)

    def test_custom_json_array_path_and_api_header_are_not_sent_to_cdn(self):
        client=self.client({'source':'custom','api_url':'https://api.example.com/random','json_path':'items.0.url',
                            'image_hosts':['cdn.example.com'],'headers':{'Authorization':'Bearer TEST_SECRET'}},
                           [payload({'items':[{'url':'https://cdn.example.com/img'}]}),Response(png())])
        with client.fetch():pass
        calls=client.opener.open.call_args_list
        self.assertEqual(calls[0].args[0].get_header('Authorization'),'Bearer TEST_SECRET')
        self.assertIsNone(calls[1].args[0].get_header('Authorization'))
        self.assertEqual(calls[0].args[0].full_url,'https://api.example.com/random')

    def test_direct_image_endpoint_receives_configured_api_header(self):
        client=self.client({'source':'custom','api_url':'https://api.example.com/random','response_type':'image',
                            'headers':{'Authorization':'Bearer TEST_SECRET'}},[Response(png())])
        with client.fetch():pass
        self.assertEqual(client.opener.open.call_args.args[0].get_header('Authorization'),'Bearer TEST_SECRET')
        self.assertEqual(client.opener.open.call_count,1)

    def test_cross_origin_redirect_drops_custom_headers(self):
        redirect=urllib.error.HTTPError('https://api.example.com/random',302,'redirect',{'Location':'https://cdn.example.com/json'},None)
        client=self.client({'source':'custom','api_url':'https://api.example.com/random','image_hosts':['cdn.example.com'],
                            'headers':{'Authorization':'Bearer TEST_SECRET'}},
                           [redirect,payload({'data':'https://cdn.example.com/img'}),Response(png())])
        with client.fetch():pass
        calls=client.opener.open.call_args_list
        self.assertEqual(calls[0].args[0].get_header('Authorization'),'Bearer TEST_SECRET')
        self.assertTrue(all(call.args[0].get_header('Authorization') is None for call in calls[1:]))

    def test_same_origin_redirect_keeps_api_authentication(self):
        redirect=urllib.error.HTTPError('https://api.example.com/random',302,'redirect',{'Location':'/new'},None)
        client=self.client({'source':'custom','api_url':'https://api.example.com/random','response_type':'image',
                            'headers':{'Authorization':'Bearer TEST_SECRET'}},[redirect,Response(png())])
        with client.fetch():pass
        self.assertEqual(client.opener.open.call_args_list[1].args[0].get_header('Authorization'),'Bearer TEST_SECRET')

    def test_unlisted_or_non_https_url_is_rejected_before_download(self):
        for url in ('https://not-allowed.example/img','https://images.xxapi.cn.evil.example/img',
                    'file:///etc/passwd','http://images.xxapi.cn/img','https://127.0.0.1/img',
                    'https://user:pass@images.xxapi.cn/img'):
            with self.subTest(url=url):
                client=self.client(responses=[payload({'code':200,'data':url})])
                with self.assertRaises(GalleryError):
                    with client.fetch():pass
                self.assertEqual(client.opener.open.call_count,1)

    def test_disallowed_redirect_and_excessive_chain_are_bounded(self):
        error=lambda location:urllib.error.HTTPError('https://v2.xxapi.cn/api/meinvpic',302,'redirect',{'Location':location},None)
        client=self.client(responses=[error('https://localhost/private')])
        with self.assertRaises(GalleryError):
            with client.fetch():pass
        self.assertEqual(client.opener.open.call_count,1)
        client=self.client(responses=[error('/again') for _ in range(4)])
        with self.assertRaisesRegex(GalleryError,'重定向'):
            with client.fetch():pass
        self.assertEqual(client.opener.open.call_count,4)

    def test_provider_error_or_missing_json_field_never_downloads(self):
        for value in ({'code':500,'data':'https://images.xxapi.cn/img'}, {'code':True,'data':'https://images.xxapi.cn/img'},
                      {'code':200,'data':None},{'code':200}):
            with self.subTest(value=value):
                client=self.client(responses=[payload(value)])
                with self.assertRaises(GalleryError):
                    with client.fetch():pass
                self.assertEqual(client.opener.open.call_count,1)

    def test_size_limit_and_truncated_response_are_rejected(self):
        responses=[Response(b'x'*(MAX_JSON_BYTES+1)),Response(b'{}',{'Content-Length':'50'})]
        for response in responses:
            client=self.client(responses=[response])
            with self.assertRaises(GalleryError):
                with client.fetch():pass
        client=self.client({'max_image_bytes':1024},[payload({'code':200,'data':'https://images.xxapi.cn/img'}),Response(b'x'*1025)])
        with self.assertRaises(GalleryError):
            with client.fetch():pass
        self.assertFalse(self.directory.exists())

    def test_invalid_image_or_excessive_pixels_is_not_saved(self):
        for raw in (b'<html>not an image</html>',png()[:25]):
            client=self.client(responses=[payload({'code':200,'data':'https://images.xxapi.cn/img'}),Response(raw)])
            with self.assertRaises(GalleryError):
                with client.fetch():pass
        client=self.client(responses=[payload({'code':200,'data':'https://images.xxapi.cn/img'}),Response(png())])
        with patch('plugins.image_gallery.source.MAX_PIXELS',1),self.assertRaises(GalleryError):
            with client.fetch():pass
        self.assertFalse(self.directory.exists())

    def test_cancelled_request_does_no_network_io(self):
        client=self.client()
        with self.assertRaises(GalleryCancelled):
            with client.fetch(current=lambda:False):pass
        client.opener.open.assert_not_called()

    def test_http_error_does_not_expose_url_credentials_or_response(self):
        error=urllib.error.HTTPError('https://v2.xxapi.cn/api/meinvpic?apiKey=TEST_SECRET',401,'TEST_SECRET',{},BytesIO(b'TEST_SECRET'))
        client=self.client(responses=[error])
        with self.assertRaisesRegex(GalleryError,'HTTP 401') as caught:
            with client.fetch():pass
        self.assertNotIn('TEST_SECRET',str(caught.exception))
        self.assertNotIn('https:',str(caught.exception))

    def test_cleanup_on_consumer_error_and_retention_when_explicit(self):
        client=self.client(responses=[payload({'code':200,'data':'https://images.xxapi.cn/img'}),Response(png())])
        with self.assertRaisesRegex(RuntimeError,'abort'):
            with client.fetch() as image:
                path=image.path
                raise RuntimeError('abort')
        self.assertFalse(path.exists())
        client=self.client(responses=[payload({'code':200,'data':'https://images.xxapi.cn/img'}),Response(png())])
        with client.fetch() as image:
            path=image.path;image.retain()
        self.assertTrue(path.is_file())

    def test_bad_configuration_is_rejected(self):
        bad=[{'source':'unknown'},{'timeout_seconds':True},{'timeout_seconds':float('nan')},
             {'timeout_seconds':31},{'max_image_bytes':True},{'max_image_bytes':21*1024*1024},
             {'image_hosts':['localhost']},{'headers':{'Host':'other'}},
             {'headers':{'Authorization':'abc\r\nInjected: yes'}},
             {'source':'custom','api_url':'file:///tmp/image'},
             {'source':'custom','api_url':'https://api.example.com','response_type':'xml'}]
        for options in bad:
            with self.subTest(options=options),self.assertRaises((ValueError,GalleryError)):
                self.client(options)


class Context:
    def __init__(self):
        self.config={}
        self.members=SimpleNamespace(account='wxid_test_robot',groups={'123@chatroom','456@chatroom'})
        self.logger=logging.getLogger('GalleryPluginTest')
        self.active={'image_gallery','idiom_chain'}
        self.current=True
        self._service=SimpleNamespace(session_is_current=lambda session:self.current and session=='one')
        self.sent=[]
        self.image_result=True
        self.send_error=None

    def plugin_is_active(self, name):return name in self.active

    def reply_image(self, message, path, *, delivery_guard=None):
        with delivery_guard() if delivery_guard else nullcontext():
            self.assert_image(path)
            self.sent.append(('image',message.chat_id,Path(path)))
            if self.send_error:raise self.send_error
            return self.image_result

    @staticmethod
    def assert_image(path):
        assert Path(path).is_file()

    def reply(self, message, text, *, delivery_guard=None):
        with delivery_guard() if delivery_guard else nullcontext():
            self.sent.append(('text',message.chat_id,text))
        return True


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.directory=Path(self.tmp.name)
        self.ctx=Context();self.now=100.0;self.after_fetch=None;self.fetch_count=0
        timer=patch('plugins.image_gallery.time.monotonic',side_effect=lambda:self.now)
        timer.start();self.addCleanup(timer.stop)
        self.plugin=Plugin();self.plugin.on_load(self.ctx)
        self.plugin.client=Mock()
        self.plugin.client.fetch.side_effect=self.fetch

    @contextmanager
    def fetch(self, *, current):
        self.fetch_count+=1
        folder=self.directory/str(self.fetch_count);folder.mkdir()
        lease=ImageLease(folder/'image.jpg');lease.path.write_bytes(png())
        try:
            if self.after_fetch:self.after_fetch()
            yield lease
        finally:lease.close()

    def send(self, text='/来张图', **kwargs):
        values={'text':text,'chat_id':'123@chatroom','sender_id':'wxid_test_sender','is_group':True,
                'session_id':'one','received_at':self.now}
        values.update(kwargs)
        self.plugin.on_message(Message(**values),self.ctx)

    def test_command_sends_only_image_and_deletes_successful_cache(self):
        self.send()
        self.assertEqual([row[0] for row in self.ctx.sent],['image'])
        self.assertFalse(self.ctx.sent[0][2].exists())
        self.now+=11;self.send('/美女')
        self.assertEqual(self.fetch_count,2)

    def test_group_cooldown_uses_arrival_time_and_is_independent(self):
        self.send();self.now+=15
        self.send(received_at=100.1)
        self.assertEqual(self.fetch_count,1)
        self.send(chat_id='456@chatroom')
        self.assertEqual(self.fetch_count,2)
        self.send()
        self.assertEqual(self.fetch_count,3)

    def test_unsupported_chat_self_bot_mention_and_old_message_do_not_fetch(self):
        cases=[{'chat_id':'789@chatroom'},{'is_group':False},{'is_self':True},
               {'data':{'bot_generated':True}},{'data':{'is_at_self':True}},
               {'received_at':60},{'text':'/来张图 extra'}]
        for values in cases:self.send(**values)
        self.assertEqual(self.fetch_count,0)
        self.assertEqual(self.ctx.sent,[])

    def test_disconnect_or_disable_during_fetch_cancels_before_sending(self):
        for change in (lambda:setattr(self.ctx,'current',False),lambda:self.ctx.active.clear(),
                       lambda:self.ctx.members.groups.clear()):
            self.ctx.current=True;self.ctx.active={'image_gallery','idiom_chain'}
            self.ctx.members.groups={'123@chatroom'};self.now+=11
            self.after_fetch=change;self.send()
        self.assertEqual(self.ctx.sent,[])
        self.assertEqual(list(self.directory.iterdir()),[])

    def test_download_failure_has_one_short_reply(self):
        self.plugin.client.fetch.side_effect=GalleryError('图库连接失败或超时')
        self.send()
        self.assertEqual(self.ctx.sent,[('text','123@chatroom','取图失败，请稍后再试。')])

    def test_uncertain_send_keeps_file_without_retry(self):
        self.ctx.image_result=False;self.send()
        self.assertTrue(self.ctx.sent[0][2].is_file())
        self.assertEqual(self.fetch_count,1)
        self.assertEqual(len(self.ctx.sent),1)

    def test_timeout_keeps_file_and_does_not_send_another_image(self):
        self.ctx.send_error=TimeoutError('native timeout');self.send()
        self.assertTrue(self.ctx.sent[0][2].is_file())
        self.assertEqual(len(self.ctx.sent),1)

    def test_help_is_merged_or_shown_only_when_no_other_menu_plugin(self):
        self.send('/help');self.assertEqual(self.ctx.sent,[])
        self.ctx.active={'image_gallery'};self.send('/help')
        self.assertEqual(self.ctx.sent,[('text','123@chatroom','1. /来张图\n\n2. /help')])
        self.assertEqual(self.fetch_count,0)

    def test_unload_prevents_more_requests(self):
        self.plugin.on_unload(self.ctx);self.send()
        self.assertEqual(self.fetch_count,0)

    def test_invalid_commands_and_cooldown_do_not_load(self):
        for options in ({'commands':['invalid']},{'commands':['/two words']},{'commands':[]},
                        {'cooldown_seconds':-1},{'cooldown_seconds':True}):
            with self.subTest(options=options),self.assertRaises(ValueError):
                self.ctx.config=options;Plugin().on_load(self.ctx)


if __name__=='__main__':unittest.main()
