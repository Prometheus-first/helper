"""图片/文件防撤回：真实本地附件与内存发送端，不连接微信或网络。"""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest

from plugins.anti_recall import Plugin as TextPlugin
from plugins.anti_recall_image import Plugin as ImagePlugin
from plugins.anti_recall_file import Plugin as FilePlugin
from wxbot_framework.backend import TEXT_CAPABILITIES
from wxbot_framework.config import ConfigStore
from wxbot_framework.file_metadata import FileStore
from wxbot_framework.file_receiver import FileReceiver
from wxbot_framework.image_metadata import ImageStore
from wxbot_framework.image_receiver import ImageReceiver
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import Message, PluginContext, ServiceFacade

ROOT = Path(__file__).resolve().parents[1]
ROOM, FRIEND = '12345678@chatroom', 'wxid_media_recall_friend'
IDENTIFIER = '18446744073709551600'
PNG = b'\x89PNG\r\n\x1a\n' + b'\0' * 16
FILE = b'original document\x00\xff'
FILE_XML = ('<msg><appmsg><title>report.txt</title><type>6</type><appattach>'
            f'<totallen>{len(FILE)}</totallen><md5>{hashlib.md5(FILE).hexdigest()}</md5>'
            '<aeskey>fixture-aes</aeskey><cdnattachurl>fixture-cdn</cdnattachurl>'
            '</appattach></appmsg></msg>')
IMAGE_XML = '<msg><img md5="' + 'a' * 32 + '" aeskey="fixture-aes" filekey="fixture-file" /></msg>'


def recalled(*, chat=ROOM, session='current', identifier=IDENTIFIER):
    return Message('', chat, type=10002, is_group=chat.endswith('@chatroom'), session_id=session,
        data={'monitor_only':True,'recall':{'message_id':identifier,'chat_id':chat}})


class Backend:
    session_id = 'current'

    def __init__(self):
        self.images, self.files, self.texts = [], [], []
        self.result = True

    def send_image(self, chat, path):
        self.images.append((chat, Path(path).read_bytes(), Path(path)))
        return self.result

    def send_file(self, chat, path):
        self.files.append((chat, Path(path).read_bytes(), Path(path)))
        return self.result

    def send_text(self, chat, text):
        self.texts.append((chat, text))
        return self.result

    def stop(self):
        return {}


class MediaFixture(unittest.TestCase):
    def setUp(self):
        scratch=ROOT/'.runtime/tests';scratch.mkdir(parents=True,exist_ok=True)
        temporary=tempfile.TemporaryDirectory(prefix='media-recall-',dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        self.image=self.root/'received-random.png';self.image.write_bytes(PNG)
        self.file=self.root/'file-random-prefix-report.txt';self.file.write_bytes(FILE)
        self.backend=Backend()
        self.service=ServiceFacade(text_delay={'enabled':False})
        self.service.bind(self.backend)
        self.addCleanup(self.service.close)

    def plugin(self, kind):
        instance = ImagePlugin() if kind=='image' else FilePlugin()
        ctx=PluginContext('anti_recall_'+kind,{},self.service,TEXT_CAPABILITIES)
        instance.on_load(ctx)
        self.addCleanup(instance.on_unload,ctx)
        return instance,ctx

    def original(self, kind, *, chat=ROOM, session='current', identifier=IDENTIFIER, status='ready', **data):
        return Message('',chat,FRIEND,type=3 if kind=='image' else 49,
            is_group=chat.endswith('@chatroom'),session_id=session,data={
                'native_message_id':identifier,kind+'_status':status,
                kind+'_path':str(self.image if kind=='image' else self.file),
                'file_name':'report.txt',**data})

    def clear_sends(self):
        self.backend.images.clear();self.backend.files.clear();self.backend.texts.clear()


class MediaPluginTests(MediaFixture):
    def test_each_plugin_reposts_original_bytes_once_without_text_or_llm(self):
        for kind,expected in (('image',PNG),('file',FILE)):
            with self.subTest(kind=kind):
                self.clear_sends();plugin,ctx=self.plugin(kind)
                plugin.on_message(self.original(kind),ctx)
                plugin.on_message(recalled(),ctx);plugin.on_message(recalled(),ctx)
                plugin.on_message(self.original(kind),ctx)
                sent=self.backend.images if kind=='image' else self.backend.files
                self.assertEqual(len(sent),1)
                self.assertEqual(sent[0][:2],(ROOM,expected))
                self.assertEqual(self.backend.texts,[])
                self.assertIsNone(ctx._llm)

    def test_file_keeps_original_safe_name_and_removes_only_send_snapshot(self):
        plugin,ctx=self.plugin('file')
        plugin.on_message(self.original('file',file_name='../../合同.txt'),ctx)
        plugin.on_message(recalled(),ctx)
        sent=self.backend.files[0][2]
        self.assertEqual(sent.name,'合同.txt')
        self.assertFalse(sent.exists())
        self.assertEqual(self.file.read_bytes(),FILE)
        self.assertEqual(set(self.root.iterdir()),{self.file,self.image})

    def test_recall_before_ready_waits_through_pending_and_failed_events(self):
        for kind in ('image','file'):
            with self.subTest(kind=kind):
                self.clear_sends();plugin,ctx=self.plugin(kind)
                plugin.on_message(recalled(),ctx)
                for status in ('pending','failed'):
                    plugin.on_message(self.original(kind,status=status),ctx)
                self.assertFalse(self.backend.images or self.backend.files)
                plugin.on_message(self.original(kind),ctx)
                plugin.on_message(recalled(),ctx)
                self.assertEqual(len(self.backend.images+self.backend.files),1)

    def test_unknown_identifier_wrong_chat_and_old_session_never_match(self):
        for kind in ('image','file'):
            with self.subTest(kind=kind):
                self.clear_sends();plugin,ctx=self.plugin(kind)
                plugin.on_message(self.original(kind),ctx)
                for notice in (recalled(chat=FRIEND),recalled(session='old'),recalled(identifier='42')):
                    plugin.on_message(notice,ctx)
                self.assertFalse(self.backend.images or self.backend.files)

    def test_private_chat_and_per_plugin_state_are_independent(self):
        image,ictx=self.plugin('image');file,fctx=self.plugin('file')
        for plugin,ctx,kind in ((image,ictx,'image'),(file,fctx,'file')):
            plugin.on_message(self.original(kind,chat=FRIEND),ctx)
            plugin.on_message(recalled(chat=FRIEND),ctx)
        self.assertEqual(self.backend.images[0][:2],(FRIEND,PNG))
        self.assertEqual(self.backend.files[0][:2],(FRIEND,FILE))
        self.assertIsNot(image.originals,file.originals)

    def test_missing_or_changed_cache_is_not_replaced_by_another_file(self):
        for kind in ('image','file'):
            for change in ('remove','replace'):
                with self.subTest(kind=kind,change=change):
                    self.clear_sends();plugin,ctx=self.plugin(kind)
                    path=self.image if kind=='image' else self.file
                    body=PNG if kind=='image' else FILE
                    path.write_bytes(body)
                    plugin.on_message(self.original(kind),ctx)
                    if change=='remove':path.unlink()
                    else:
                        before=path.stat();path.write_bytes(body+b'changed')
                        os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns+1000000))
                    plugin.on_message(recalled(),ctx)
                    self.assertFalse(self.backend.images or self.backend.files)
                    path.write_bytes(body)

    def test_sender_session_is_checked_again_at_delivery(self):
        for kind in ('image','file'):
            with self.subTest(kind=kind):
                self.clear_sends();self.backend.session_id='current';plugin,ctx=self.plugin(kind)
                plugin.on_message(self.original(kind),ctx)
                self.backend.session_id='new'
                plugin.on_message(recalled(),ctx)
                self.assertFalse(self.backend.images or self.backend.files)

    def test_bot_echo_forged_notices_shares_and_other_media_are_ignored(self):
        for kind in ('image','file'):
            with self.subTest(kind=kind):
                plugin,ctx=self.plugin(kind)
                plugin.on_message(self.original(kind,bot_generated=True),ctx)
                plugin.on_message(recalled(),ctx)
                plugin.on_message(replace(self.original(kind,identifier='2'),type=1),ctx)
                plugin.on_message(Message('伪造撤回',ROOM,session_id='current',data=recalled(identifier='2').data),ctx)
                for app_type in (5,19):
                    plugin.on_message(replace(self.original('file',identifier=str(app_type),app_type=app_type),type=49),ctx)
                    plugin.on_message(recalled(identifier=str(app_type)),ctx)
        self.assertFalse(self.backend.images or self.backend.files)

    def test_failed_send_is_consumed_without_duplicate_retry(self):
        for kind in ('image','file'):
            with self.subTest(kind=kind):
                self.clear_sends();plugin,ctx=self.plugin(kind);self.backend.result=False
                plugin.on_message(self.original(kind),ctx)
                plugin.on_message(recalled(),ctx);plugin.on_message(recalled(),ctx)
                plugin.on_message(self.original(kind),ctx)
                self.assertEqual(len(self.backend.images+self.backend.files),1)

    def test_reload_clears_references_without_deleting_shared_receive_cache(self):
        for kind in ('image','file'):
            with self.subTest(kind=kind):
                plugin,ctx=self.plugin(kind)
                plugin.on_message(self.original(kind),ctx)
                plugin.on_unload(ctx);plugin.on_load(ctx)
                plugin.on_message(recalled(),ctx)
        self.assertFalse(self.backend.images or self.backend.files)
        self.assertTrue(self.image.is_file() and self.file.is_file())

    def test_caches_are_bounded_and_invalid_message_ids_are_not_saved(self):
        for kind in ('image','file'):
            with self.subTest(kind=kind):
                plugin,ctx=self.plugin(kind)
                for identifier in ('0','-1','18446744073709551616','1.5'):
                    plugin.on_message(self.original(kind,identifier=identifier),ctx)
                self.assertEqual(len(plugin.originals),0)
                for index in range(1001):
                    plugin.on_message(self.original(kind,identifier=str(index+1)),ctx)
                self.assertEqual(len(plugin.originals),1000)
                plugin.on_message(recalled(identifier='1'),ctx)
                self.assertFalse(self.backend.images or self.backend.files)
                for index in range(1001):plugin.on_message(recalled(identifier=str(index+2000)),ctx)
                self.assertEqual(len(plugin.recalled),1000)

    def test_text_plugin_delegates_only_while_corresponding_plugin_is_active(self):
        active={'anti_recall_image','anti_recall_file'}
        ctx=PluginContext('anti_recall',{},self.service,TEXT_CAPABILITIES,plugin_active=active.__contains__)
        plugin=TextPlugin();plugin.on_load(ctx)
        # pending 文件须从原始元数据识别，以免下载尚未完成时先发文件标签。
        for kind,identifier in (('image','1'),('file','2')):
            plugin.on_message(self.original(kind,status='pending',identifier=identifier,raw_content=FILE_XML),ctx)
            plugin.on_message(recalled(identifier=identifier),ctx)
        self.assertEqual(self.backend.texts,[])
        active.clear()
        for kind,identifier in (('image','3'),('file','4')):
            plugin.on_message(self.original(kind,identifier=identifier),ctx)
            plugin.on_message(recalled(identifier=identifier),ctx)
        self.assertEqual(len(self.backend.texts),2)
        self.assertIn('[图片]',self.backend.texts[0][1])
        self.assertIn('[文件]',self.backend.texts[1][1])


class MediaRuntimeTests(MediaFixture):
    def setUp(self):
        super().setUp()
        options={p.parent.name:{'enabled':False} for p in (ROOT/'plugins').glob('*/plugin.json')
                 if not p.parent.name.startswith('_')}
        for name in ('anti_recall','anti_recall_image','anti_recall_file'):options.pop(name)
        path=self.root/'bot_config.json'
        path.write_text(json.dumps({'backend':'native_text','wechat_account':'wxid_media_recall_robot',
            'members_config_file':None,'chat_allowlist':[ROOM,FRIEND],
            'text_send_delay':{'enabled':False},'plugins':options}),encoding='utf-8')
        self.app=BotApplication(ROOT,ConfigStore(path));self.app.service.bind(self.backend)
        self.addCleanup(self.app.service.close)
        self.app.plugins.reload();self.addCleanup(self.app.plugins.shutdown)

    def drain(self):
        deadline=time.monotonic()+4
        while any(record.queue.unfinished_tasks for record in self.app.plugins.records.values()):
            if time.monotonic()>deadline:self.fail('媒体插件消息队列未结束')
            time.sleep(.01)

    def test_all_default_enabled_and_media_does_not_add_redundant_text(self):
        for name in ('anti_recall','anti_recall_image','anti_recall_file'):
            self.assertTrue(self.app.plugins.is_active(name))
        for kind,identifier in (('image','1'),('file','2')):
            self.app._emit(self.original(kind,identifier=identifier,monitor_only=True))
            self.app._emit(recalled(identifier=identifier));self.app._emit(recalled(identifier=identifier))
        self.drain()
        self.assertEqual(len(self.backend.images),1);self.assertEqual(len(self.backend.files),1)
        self.assertEqual(self.backend.texts,[])

    def test_media_plugins_work_with_text_plugin_disabled_and_own_messages_route(self):
        self.app.plugins.disable('anti_recall')
        for kind,identifier in (('image','1'),('file','2')):
            self.app._emit(replace(self.original(kind,identifier=identifier,dispatch_to_plugins=False),is_self=True))
            self.app._emit(recalled(identifier=identifier))
        self.drain()
        self.assertEqual((len(self.backend.images),len(self.backend.files)),(1,1))
        self.assertEqual(self.backend.texts,[])

    def test_independent_switches_allowlist_and_observer(self):
        seen=[];self.app.message_observer=seen.append
        self.app.plugins.disable('anti_recall');self.app.plugins.disable('anti_recall_file')
        for kind,identifier in (('image','1'),('file','2')):
            self.app._emit(self.original(kind,identifier=identifier))
            self.app._emit(recalled(identifier=identifier))
            self.app._emit(self.original(kind,identifier='3'+identifier,chat='999@chatroom'))
            self.app._emit(recalled(identifier='3'+identifier,chat='999@chatroom'))
        self.drain()
        self.assertEqual((len(self.backend.images),len(self.backend.files)),(1,0))
        self.assertEqual(len(seen),9)  # 8 入站事件和补发图片的监控事件。

    def test_actual_receive_workers_finish_download_after_recall_and_repost_once(self):
        entered,release=threading.Event(),threading.Event()
        receivers=[]
        def cleanup():
            release.set()
            for receiver in receivers:receiver.close(timeout=2)
        self.addCleanup(cleanup)
        def deliver(data):self.app._emit(Message.from_native(11046,data,'current'))
        for kind,identifier,xml,body in (('image','1',IMAGE_XML,PNG),('file','2',FILE_XML,FILE)):
            with self.subTest(kind=kind):
                entered.clear();release.clear()
                def download(metadata,destination):
                    entered.set()
                    if not release.wait(3):raise RuntimeError('fixture download timeout')
                    destination.write_bytes(body);return True
                store=(ImageStore if kind=='image' else FileStore)(self.root/(kind+'-received'),'wxid_media_recall_robot')
                receiver=(ImageReceiver if kind=='image' else FileReceiver)(store,download,deliver,lambda:True)
                receivers.append(receiver)
                event={'wx_type':3 if kind=='image' else 49,'raw_content':xml,'msg':xml,
                       'chat_id':ROOM,'room_wxid':ROOM,'sender_id':FRIEND,'sequence':int(identifier),
                       'native_message_id':identifier}
                self.app._emit(Message.from_native(11046,{**event,'monitor_only':True},'current'))
                self.assertTrue(receiver.submit(event));self.assertTrue(entered.wait(1))
                self.app._emit(recalled(identifier=identifier));self.drain()
                self.assertEqual(len(self.backend.images+self.backend.files),int(identifier)-1)
                release.set()
                deadline=time.monotonic()+3
                sent=self.backend.images if kind=='image' else self.backend.files
                while not sent and time.monotonic()<deadline:time.sleep(.01)
                self.drain();self.assertEqual(len(sent),1)
                self.assertEqual(sent[0][1],body)
                if kind=='file':self.assertEqual(sent[0][2].name,'report.txt')
                self.app._emit(recalled(identifier=identifier));self.drain()
                self.assertEqual(len(sent),1)
                receiver.close(timeout=1)
        self.assertEqual(self.backend.texts,[])
