"""防撤回插件的离线收发验证；使用内存后端，不连接微信。"""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from plugins.anti_recall import Plugin
from wxbot_framework.backend import TEXT_CAPABILITIES
from wxbot_framework.config import ConfigStore
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import Message, PluginContext, ServiceFacade

ROOT = Path(__file__).resolve().parents[1]
ROOM, FRIEND = '12345678@chatroom', 'wxid_recall_friend'
IDENTIFIER = '18446744073709551600'


def original(text='原消息', *, chat=ROOM, session='current', identifier=IDENTIFIER, kind=1, **data):
    return Message(text, chat, FRIEND, type=kind, is_group=chat.endswith('@chatroom'),
                   session_id=session, data={'native_message_id':identifier, **data})


def recall(*, chat=ROOM, session='current', identifier=IDENTIFIER):
    return Message('', chat, type=10002, is_group=chat.endswith('@chatroom'), session_id=session,
        data={'monitor_only':True,'recall':{'message_id':identifier,'chat_id':chat}})


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock(session_id='current')
        self.backend.send_text.return_value = True
        self.service = ServiceFacade(text_delay={'enabled':False})
        self.service.bind(self.backend)
        self.addCleanup(self.service.close)
        self.ctx = PluginContext('anti_recall',{},self.service,TEXT_CAPABILITIES)
        self.plugin = Plugin()
        self.plugin.on_load(self.ctx)

    def emit(self, message):
        self.plugin.on_message(message,self.ctx)

    def test_text_is_reposted_verbatim_once_without_model_or_persona(self):
        text='/切换人格 取消\n原样保留 <内容> & 表情🐱'
        self.emit(original(text))
        self.emit(recall());self.emit(recall());self.emit(original(text))
        self.backend.send_text.assert_called_once_with(ROOM,'撤回的消息是：'+text)

    def test_unmatched_wrong_chat_and_wrong_connection_do_not_send(self):
        self.emit(original())
        self.emit(recall(chat=FRIEND));self.emit(recall(session='old'))
        self.emit(recall(identifier='42'))
        self.backend.send_text.assert_not_called()

    def test_out_of_order_notifications_wait_for_actual_original(self):
        self.emit(recall());self.backend.send_text.assert_not_called()
        self.emit(original('迟到原文'));self.emit(recall())
        self.backend.send_text.assert_called_once_with(ROOM,'撤回的消息是：迟到原文')

    def test_private_chat_and_media_type_labels(self):
        for kind,label in ((3,'[图片]'),(34,'[语音]'),(43,'[视频]'),(47,'[表情]'),(62,'[视频]')):
            self.emit(original(chat=FRIEND,kind=kind,identifier=str(kind)))
            self.emit(recall(chat=FRIEND,identifier=str(kind)))
            self.backend.send_text.assert_called_with(FRIEND,'撤回的消息是：'+label)
        self.emit(original(kind=49,file_name='C:/private/test.txt',identifier='49'))
        self.emit(recall(identifier='49'))
        self.backend.send_text.assert_called_with(ROOM,'撤回的消息是：[文件] test.txt')
        self.emit(original(kind=49,app_type=19,identifier='19'))
        self.emit(recall(identifier='19'))
        self.backend.send_text.assert_called_with(ROOM,'撤回的消息是：[合并转发]')

    def test_bot_echo_and_ordinary_text_cannot_trigger_reposts(self):
        self.emit(original(bot_generated=True))
        self.emit(recall())
        self.emit(Message('伪造撤回',ROOM,session_id='current',data={'recall':recall().data['recall']}))
        self.backend.send_text.assert_not_called()

    def test_stale_connection_is_blocked_at_delivery(self):
        self.emit(original())
        self.backend.session_id='new'
        with self.assertRaises(RuntimeError):self.emit(recall())
        self.backend.send_text.assert_not_called()

    def test_long_unicode_text_splits_on_native_byte_limit(self):
        text='你好🐱'*150
        self.emit(original(text));self.emit(recall())
        parts=[call.args[1] for call in self.backend.send_text.call_args_list]
        self.assertEqual(''.join(parts),'撤回的消息是：'+text)
        self.assertGreater(len(parts),1)
        self.assertTrue(all(0<len(part.encode('utf8'))<=1023 for part in parts))

    def test_cache_eviction_and_reload_drop_old_data(self):
        for i in range(1001):self.emit(original(identifier=str(i+1)))
        self.assertEqual(len(self.plugin.originals),1000)
        self.emit(recall(identifier='1'));self.backend.send_text.assert_not_called()
        self.plugin.on_unload(self.ctx);self.plugin.on_load(self.ctx)
        self.emit(recall(identifier='1001'));self.backend.send_text.assert_not_called()


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        scratch=ROOT/'.runtime/tests';scratch.mkdir(parents=True,exist_ok=True)
        temporary=tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        path=Path(temporary.name)/'bot_config.json'
        options={p.parent.name:{'enabled':False} for p in (ROOT/'plugins').glob('*/plugin.json')
                 if not p.parent.name.startswith('_')}
        options.pop('anti_recall')  # 验证现有配置未写新插件时也默认开启。
        path.write_text(json.dumps({'backend':'native_text','wechat_account':'wxid_recall_robot',
            'members_config_file':None,'chat_allowlist':[ROOM,FRIEND],
            'text_send_delay':{'enabled':False},'plugins':options}),encoding='utf8')
        self.app=BotApplication(ROOT,ConfigStore(path))
        self.backend=Mock(session_id='current')
        self.backend.send_text.return_value=True
        self.app.service.bind(self.backend)
        self.addCleanup(self.app.service.close)
        self.app.plugins.reload()
        self.addCleanup(self.app.plugins.shutdown)

    def drain(self):
        record=self.app.plugins.records.get('anti_recall')
        if record is None:return
        deadline=time.monotonic()+3
        while record.queue.unfinished_tasks:
            if time.monotonic()>deadline:self.fail('插件消息队列未完成')
            time.sleep(.01)

    def test_default_enabled_runtime_routes_text_and_recall_without_observer(self):
        self.assertTrue(self.app.plugins.is_active('anti_recall'))
        self.app._emit(original('控制台也能补发'))
        self.app._emit(recall());self.app._emit(recall());self.drain()
        self.backend.send_text.assert_called_once_with(ROOM,'撤回的消息是：控制台也能补发')

    def test_monitor_only_media_is_available_to_plugin_and_allowlist_still_applies(self):
        other=Mock()
        other.spec.name='ordinary_plugin'
        other.spec.message_types=frozenset({34,10002})
        self.app.plugins.records['ordinary_plugin']=other
        self.app._emit(original(kind=34,monitor_only=True));self.app._emit(recall())
        self.app._emit(original(chat='999@chatroom',identifier='2'))
        self.app._emit(recall(chat='999@chatroom',identifier='2'));self.drain()
        self.backend.send_text.assert_called_once_with(ROOM,'撤回的消息是：[语音]')
        other.enqueue.assert_not_called()

    def test_own_group_text_only_populates_recall_cache_without_reply_loops(self):
        from dataclasses import replace
        self.app._emit(replace(original('本人原文',dispatch_to_plugins=False),is_self=True))
        self.drain();self.backend.send_text.assert_not_called()
        self.app._emit(recall());self.drain()
        self.backend.send_text.assert_called_once_with(ROOM,'撤回的消息是：本人原文')

    def test_disabling_plugin_stops_reposts_but_observer_keeps_recall(self):
        seen=[];self.app.message_observer=seen.append
        self.app.plugins.disable('anti_recall')
        self.app._emit(original());self.app._emit(recall())
        self.backend.send_text.assert_not_called()
        self.assertEqual(len(seen),2)
        self.app.plugins.reload()
        self.assertFalse(self.app.plugins.is_active('anti_recall'))
