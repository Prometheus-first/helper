"""Linux 网页管理台回归：仅使用合成账号和假后端，不启动真实微信。"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from wxbot_framework.linux_panel_backend import LinuxPanelBackend, PANEL_CAPABILITIES
from wxbot_framework.linux_panel import LinuxBotApplication, LinuxPanelController
from wxbot_framework.message_monitor import MessageBuffer
from wxbot_framework.panel_common import PanelError
from wxbot_framework.panel_http import PanelServer
from wxbot_framework.sdk import Message
from wxbot_framework.emoticons import ExportedEmoticon

ROOT = Path(__file__).resolve().parents[1]


def wait_for(check, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(.02)
    raise AssertionError('fixture timeout')


class AdapterTests(unittest.TestCase):
    def setUp(self):
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / 'wechat'
            executable.write_text('fixture')
            self.backend = LinuxPanelBackend(executable, executable, executable)

    def event(self, text='hello', kind=1, receiver='wxid_self_fixture', sender='wxid_friend_fixture'):
        return dict(sender=sender, receiver=receiver, text=text, type=kind, sequence=1)

    def test_image_scope_follows_empty_and_explicit_allowlists(self):
        from unittest.mock import Mock
        sender = Mock(features={'image_scope_groups'})
        sender.bind_all_image_scopes.return_value = 5
        sender.bind_image_scopes.return_value = 6
        self.backend._sender = sender
        self.backend.account = 'wxid_self_fixture'
        self.assertEqual(self.backend.configure_image_downloads([]), 5)
        sender.bind_all_image_scopes.assert_called_once_with('wxid_self_fixture')
        self.assertEqual(self.backend.configure_image_downloads(['123@chatroom']), 6)
        sender.bind_image_scopes.assert_called_once_with(['123@chatroom'], 'wxid_self_fixture')
        self.assertIsNone(self.backend.configure_image_downloads(['filehelper']))
        sender.bind_all_image_scopes.assert_called_once()

    def test_identity_is_confirmed_from_incoming_receiver(self):
        self.assertFalse(self.backend.account)
        message = self.backend.convert_event(self.event())
        self.assertEqual(self.backend.account, 'wxid_self_fixture')
        self.assertEqual(message.sender_id, 'wxid_friend_fixture')
        self.assertEqual(message.text, 'hello')
        self.assertIn(message.chat_id, {r['chat_id'] for r in self.backend.chats()})

    def test_group_member_and_long_text_are_preserved(self):
        body = 'x' * 12000 + '_END'
        message = self.backend.convert_event(self.event('wxid_friend_fixture:\n' + body, sender='123@chatroom'))
        self.assertTrue(message.is_group)
        self.assertEqual(message.sender_id, 'wxid_friend_fixture')
        self.assertEqual(message.text, body)

    def test_own_outgoing_sync_is_not_mistaken_for_account_change(self):
        self.backend.convert_event(self.event())
        self.assertIsNone(self.backend.convert_event(self.event(sender='wxid_self_fixture', receiver='wxid_friend_fixture')))
        self.assertEqual(self.backend.account, 'wxid_self_fixture')

    def test_first_filehelper_sync_confirms_sender_as_account(self):
        message = self.backend.convert_event(self.event(sender='wxid_self_fixture', receiver='filehelper'))
        self.assertEqual(self.backend.account, 'wxid_self_fixture')
        self.assertEqual(message.chat_id, 'filehelper')

    def test_account_change_stops_processing(self):
        self.backend.convert_event(self.event())
        with self.assertRaises(RuntimeError):
            self.backend.convert_event(self.event(receiver='wxid_other_fixture'))

    def test_media_monitor_does_not_keep_xml_credentials(self):
        monitor = MessageBuffer()
        for kind in (3, 34, 47, 49):
            message = self.backend.convert_event(self.event('<msg aeskey="PRIVATE_FIXTURE_KEY"/>', kind))
            self.assertTrue(message.data['monitor_only'])
            self.assertEqual(message.text, '')
            monitor.append(message)
            self.assertNotIn('PRIVATE_FIXTURE', repr(message.data))
        content = json.dumps(monitor.tail(0))
        self.assertNotIn('aeskey', content)
        for text in ('[图片]', '[语音]', '[表情]', '[文件/分享]'):
            self.assertIn(text, json.dumps(monitor.tail(0), ensure_ascii=False))

    def test_recall_marks_original_and_does_not_expose_system_xml(self):
        monitor=MessageBuffer()
        source=self.event('wxid_friend_fixture:\n原消息', sender='123@chatroom')
        source['native_message_id']='18446744073709551614'
        original=self.backend.convert_event(source)
        monitor.append(original)
        xml=('<sysmsg type="revokemsg"><revokemsg><session>123@chatroom</session>'
             '<newmsgid>18446744073709551614</newmsgid><replacemsg>撤回了一条消息</replacemsg>'
             '</revokemsg></sysmsg>')
        event=self.event(xml,10002,sender='123@chatroom');event['sequence']=2
        recall=self.backend.convert_event(event)
        self.assertTrue(recall.data['monitor_only'])
        self.assertTrue(recall.data['dispatch_to_recall_plugins'])
        self.assertNotIn('sysmsg',repr(recall.data))
        monitor.append(recall)
        rows=monitor.tail(0)['rows']
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['content'],'原消息')
        self.assertTrue(rows[0]['recalled'])
        self.assertEqual(rows[0]['message_id'],'18446744073709551614')

    def test_recall_rejects_wrong_session_and_forged_xml(self):
        xml=('<sysmsg type="revokemsg"><revokemsg><session>999@chatroom</session>'
             '<newmsgid>1</newmsgid></revokemsg></sysmsg>')
        self.assertIsNone(self.backend.convert_event(self.event(xml,10002,sender='123@chatroom')))
        self.assertIsNone(self.backend.convert_event(self.event('<!DOCTYPE x><sysmsg/>',10002,sender='123@chatroom')))

    def test_monitor_only_recall_does_not_invoke_plugins(self):
        from types import SimpleNamespace
        from wxbot_framework.runtime import BotApplication
        observed=[];dispatched=[]
        app=SimpleNamespace(_with_group_role=lambda message: message,
                            _observe_message=observed.append,
                            _dispatch_recall_plugins=dispatched.append,
                            config={'chat_allowlist':[]})
        msg=Message(text='',chat_id='123@chatroom',type=10002,is_group=True,
                    data={'monitor_only':True,'dispatch_to_recall_plugins':False,
                          'recall':{'chat_id':'123@chatroom','message_id':'7'}})
        BotApplication._emit(app,msg)
        self.assertEqual(observed,[msg])
        self.assertEqual(dispatched,[])

    def test_capture_environment_does_not_inherit_diagnostic_filters(self):
        env = self.backend.launch_environment({'HELPER_LINUX_TEST_MEDIA':'1', 'HELPER_LINUX_TEST_MARKERS':'1'})
        self.assertEqual(env, {'HELPER_LINUX_PANEL_MEDIA':'1'})


class FakeBackend:
    capabilities = PANEL_CAPABILITIES
    session_id = 'linux-fixture-session'
    pid = 777777
    last_error = None
    scope = 'fixture'

    def __init__(self, *args, **kwargs):
        self.account = ''
        self.calls = []
        self.allowed = kwargs.get('allowed_targets')

    def start(self, emit):
        self.emit = emit
        return True

    def identify(self):
        self.account = 'wxid_self_fixture'
        self.emit(Message(text='fixture hello', chat_id='wxid_friend_fixture', sender_id='wxid_friend_fixture',
            receiver_id=self.account, session_id=self.session_id, sequence=1))

    def send_text(self, target, text):
        self.calls.append(('text', target, text))
        return True

    def send_file(self, target, path):
        self.calls.append(('file', target, path))
        return True

    def send_voice(self, target, path):
        self.calls.append(('voice', target, Path(path).read_bytes()))
        return True

    def send_at_text(self, target, text, members):
        self.calls.append(('mention', target, text, members))
        return True

    def chats(self):
        return [{'chat_id':'wxid_friend_fixture', 'is_group':False, 'name':''}]

    def contacts(self):
        return {'account': self.account, 'session_id': self.session_id, 'source': 'wechat_database',
                'complete': False, 'scanned_at': '2026-01-01T00:00:00Z',
                'contacts': [{'wxid':'wxid_friend_fixture', 'nickname':'合成昵称', 'remark':'合成备注',
                              'alias':'', 'tags':['合成标签'], 'tag_ids':['7'], 'tags_status':'ready'}],
                'tags': [{'id':'7', 'name':'合成标签'}]}

    def group_members(self, room):
        return getattr(self, 'member_rows', None)

    def stop(self):
        return {'capture_stop_requested':True, 'wechat_left_running':True}


class PanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='helper-linux-panel-fixture-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for path in ROOT.glob('*.example.json'):
            shutil.copy2(path, self.root / path.name)
        for name in ('echo', 'id_responder', 'anti_recall'):
            shutil.copytree(ROOT / 'plugins' / name, self.root / 'plugins' / name)
        shutil.copytree(ROOT / 'webui', self.root / 'webui')
        self.fake_exe = self.root / 'fake-wechat'
        self.fake_exe.write_text('fixture executable')
        self.instances = []
        def factory(*args, **kwargs):
            backend = FakeBackend(*args, **kwargs)
            self.instances.append(backend)
            return backend
        self.controller = LinuxPanelController(self.root, wechat_path=self.fake_exe,
            receive_bridge=self.fake_exe, send_bridge=self.fake_exe,
            backend_factory=factory, scan=lambda: [])
        self.addCleanup(self.controller.close)

    def start_identified(self):
        self.controller.start()
        wait_for(lambda: self.instances and hasattr(self.instances[0], 'emit'))
        self.instances[0].identify()
        wait_for(lambda: self.controller.snapshot()['connected'])

    def test_start_waits_for_identity_and_never_persists_binding(self):
        self.controller.start()
        wait_for(lambda: self.controller.snapshot()['login_pending'])
        self.assertFalse(self.controller.snapshot()['connected'])
        with self.assertRaises(PanelError):
            self.controller.send_connection()
        self.instances[0].identify()
        self.assertTrue(self.controller.snapshot()['connected'])
        self.assertIsNone(self.controller.store.load()['wechat_account'])
        self.assertFalse((self.root / 'bot_config.json').exists())
        self.assertEqual(self.controller.message_tail()['rows'][0]['content'], 'fixture hello')
        self.assertEqual(self.controller.app.members.account, 'wxid_self_fixture')

    def test_failed_launch_ends_in_error_without_relaunch_loop(self):
        def factory(*args, **kwargs):
            backend=FakeBackend()
            self.instances.append(backend)
            def fail(emit):
                raise RuntimeError('fixture launch failure')
            backend.start=fail
            return backend
        self.controller._backend_factory=factory
        self.controller.start()
        wait_for(lambda: self.controller.snapshot()['state']=='error')
        self.assertIn('fixture launch failure',self.controller.snapshot()['error'])
        self.assertEqual(len(self.instances),1)
        self.assertFalse(self.controller.snapshot()['connected'])

    def test_existing_process_prevents_second_launch(self):
        self.controller._scan_linux = lambda: [{'pid':123}]
        with self.assertRaisesRegex(PanelError, '退出已有'):
            self.controller.start()
        self.assertEqual(self.instances, [])

    def test_duplicate_start_and_stop_cleanup(self):
        self.start_identified()
        with self.assertRaises(PanelError):
            self.controller.start()
        self.assertEqual(len(self.instances), 1)
        self.controller.stop()
        wait_for(lambda: self.controller.snapshot()['state'] == 'stopped')
        self.assertFalse(self.controller.snapshot()['connected'])

    def test_supported_plugins_and_business_editor_are_reused(self):
        rows = {r['name']:r for r in self.controller.plugin_rows()}
        self.assertTrue(rows['echo']['supported'])
        self.assertTrue(rows['anti_recall']['supported'])
        with self.assertRaises(PanelError):
            self.controller.plugin_action({'name':'anti_recall_voice','action':'enable'})
        editor = self.controller.plugin_business('echo')
        self.assertIn('functions', editor)
        self.assertNotIn('files', editor)

    def test_linux_contacts_http_read_is_authenticated_and_session_scoped(self):
        self.start_identified()
        server = PanelServer(self.controller, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = 'http://127.0.0.1:' + str(server.server_address[1]) + '/api/contacts'
        with self.assertRaises(HTTPError) as error:
            urlopen(url)
        self.assertEqual(error.exception.code, 403)
        with urlopen(Request(url, headers={'X-Helper-Token': server.token})) as response:
            result = json.load(response)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(result['session_id'], 'linux-fixture-session')
        self.assertEqual(result['account'], 'wxid_self_fixture')
        self.assertEqual(result['contacts'][0]['remark'], '合成备注')
        self.assertEqual(result['contacts'][0]['tags'], ['合成标签'])
        self.assertNotIn('合成备注', json.dumps(self.controller.snapshot(), ensure_ascii=False))

    def test_configuration_masks_keys_and_saves_group_alias(self):
        self.controller.save_configuration({'groups':['123@chatroom'], 'group_names':{'123@chatroom':'测试备注'},
            'llm':{'api_key':'fixture-secret-value'}})
        config = self.controller.configuration()
        self.assertEqual(config['group_names'], {'123@chatroom':'测试备注'})
        self.assertTrue(config['llm']['api_key_configured'])
        self.assertNotIn('fixture-secret-value', json.dumps(config))

    def test_file_send_is_deduplicated_and_stale_session_rejected(self):
        self.start_identified()
        path = self.root / 'file.txt'
        path.write_text('fixture file')
        data = dict(kind='file', path=str(path), chat_id='wxid_friend_fixture',
                    session_id='linux-fixture-session', request_id='fixture-send-id-00001')
        self.controller.send_media(data)
        wait_for(lambda: self.controller.send_status(data['request_id'])['status'] != 'sending')
        self.controller.send_media(data)
        self.assertEqual(len(self.instances[0].calls), 1)
        with self.assertRaises(PanelError):
            self.controller.send_media(dict(data, session_id='stale-session', request_id='fixture-send-id-00002'))
        with self.assertRaisesRegex(ValueError, '目标必须是群'):
            self.controller.send_text(dict(data, content='hello', at_list=['wxid_friend_fixture']))

    def test_voice_send_requires_active_capability_and_reuses_send_job(self):
        self.start_identified()
        body = b'\x02#!SILK_V3' + b'\x03\0abc' * 50
        path = self.root / 'voice.silk'
        path.write_bytes(body)
        data = dict(kind='voice', path=str(path), chat_id='wxid_friend_fixture',
                    session_id='linux-fixture-session', request_id='fixture-voice-id-00001')
        with self.assertRaisesRegex(PanelError, '尚未开放语音发送'):
            self.controller.send_media(data)
        self.assertNotIn(data['request_id'], self.controller.send_jobs)
        self.assertEqual(self.instances[0].calls, [])
        self.instances[0].capabilities = PANEL_CAPABILITIES | {'voice.send'}
        self.controller.send_media(data)
        wait_for(lambda: self.controller.send_status(data['request_id'])['status'] != 'sending')
        self.assertEqual(self.controller.send_status(data['request_id'])['status'], 'sent')
        self.controller.send_media(data)
        self.assertEqual(self.instances[0].calls, [('voice', data['chat_id'], body)])
        with self.assertRaisesRegex(PanelError, '编号已使用'):
            self.controller.send_media(dict(data, kind='file'))
        with self.assertRaisesRegex(PanelError, '连接已变化'):
            self.controller.send_media(dict(data, session_id='old', request_id='fixture-voice-id-00002'))

    def test_voice_send_enforces_allowlist_path_and_failed_result_does_not_retry(self):
        self.controller.save_configuration({'chat_allowlist': ['123@chatroom']})
        self.start_identified()
        self.instances[0].capabilities = PANEL_CAPABILITIES | {'voice.send'}
        path = self.root / 'invalid.silk'
        path.write_bytes(b'not an audio recording')
        data = dict(kind='voice', path=str(path), chat_id='123@chatroom',
                    session_id='linux-fixture-session', request_id='fixture-voice-id-00003')
        with self.assertRaisesRegex(PanelError, '白名单'):
            self.controller.send_media(dict(data, chat_id='wxid_friend_fixture'))
        with self.assertRaisesRegex(PanelError, '绝对路径'):
            self.controller.send_media(dict(data, path='voice.wav'))
        self.controller.send_media(data)
        wait_for(lambda: self.controller.send_status(data['request_id'])['status'] != 'sending')
        status = self.controller.send_status(data['request_id'])
        self.assertEqual(status['status'], 'failed')
        self.assertNotIn(str(path), status['error'])
        self.controller.send_media(data)
        self.assertEqual(self.instances[0].calls, [])

    def test_linux_tts_settings_reuse_masked_configuration_and_keep_blank_key(self):
        self.controller.save_configuration({'tts': {'api_key': 'fixture-tts-secret',
            'default_voice': 'fixture-voice', 'max_chars_per_clip': 80}})
        self.controller.save_configuration({'tts': {'api_key': '', 'max_chars_per_clip': 100}})
        tts = self.controller.configuration()['tts']
        self.assertTrue(tts['api_key_configured'])
        self.assertEqual(tts['default_voice'], 'fixture-voice')
        self.assertEqual(tts['max_chars_per_clip'], 100)
        self.assertNotIn('fixture-tts-secret', json.dumps(tts))

    def test_negotiated_session_capabilities_enable_voice_plugin_before_bot_start(self):
        shutil.copytree(ROOT / 'plugins' / 'anti_recall_voice', self.root / 'plugins' / 'anti_recall_voice')
        row = lambda: next(item for item in self.controller.plugin_rows() if item['name'] == 'anti_recall_voice')
        self.assertFalse(row()['supported'])
        self.assertNotIn('voice.send', self.controller.snapshot()['capabilities'])
        with self.assertRaisesRegex(PanelError, 'voice.send'):
            self.controller.plugin_action({'name': 'anti_recall_voice', 'action': 'enable'})
        backend = FakeBackend()
        backend.capabilities = PANEL_CAPABILITIES | {'voice.send', 'emoticon.send'}
        self.controller.session = SimpleNamespace(backend=backend,
            profile={'wxid': 'wxid_self_fixture', 'logged_in': True}, wechat_running=lambda: True)
        self.assertTrue(row()['supported'])
        self.assertIn('voice.send', self.controller.snapshot()['capabilities'])
        self.assertIn('emoticon.send', self.controller.snapshot()['capabilities'])
        self.controller.plugin_action({'name': 'anti_recall_voice', 'action': 'enable'})
        self.assertTrue(self.controller.store.load()['plugins']['anti_recall_voice']['enabled'])
        self.controller.session.backend = None
        self.assertFalse(row()['supported'])
        self.assertNotIn('voice.send', self.controller.snapshot()['capabilities'])

    def test_running_backend_capabilities_override_different_session_backend(self):
        shutil.copytree(ROOT / 'plugins' / 'anti_recall_voice', self.root / 'plugins' / 'anti_recall_voice')
        self.controller.plugin_action({'name': 'anti_recall_voice', 'action': 'configure', 'options': {'enabled': False}})
        self.start_identified()
        negotiated = FakeBackend()
        negotiated.capabilities = PANEL_CAPABILITIES | {'voice.send'}
        self.controller.session = SimpleNamespace(backend=negotiated,
            profile={'wxid': 'wxid_self_fixture', 'logged_in': True}, wechat_running=lambda: True)
        self.assertNotIn('voice.send', self.controller.snapshot()['capabilities'])
        with self.assertRaisesRegex(PanelError, 'voice.send'):
            self.controller.plugin_action({'name': 'anti_recall_voice', 'action': 'enable'})

    def test_application_initial_plugins_use_negotiated_capabilities_and_enable_while_running(self):
        shutil.copytree(ROOT / 'plugins' / 'anti_recall_voice', self.root / 'plugins' / 'anti_recall_voice')
        self.controller.plugin_action({'name': 'anti_recall_voice', 'action': 'configure', 'options': {'enabled': False}})
        backend = FakeBackend()
        backend.capabilities = PANEL_CAPABILITIES | {'voice.send'}
        app = LinuxBotApplication(self.root, self.controller.store, backend)
        self.addCleanup(app.service.close)
        self.addCleanup(app.plugins.shutdown)
        self.assertEqual(app.plugins.capabilities, backend.capabilities)
        factory = self.controller._backend_factory
        def negotiated(*args, **kwargs):
            result = factory(*args, **kwargs)
            result.capabilities = PANEL_CAPABILITIES | {'voice.send'}
            return result
        self.controller._backend_factory = negotiated
        self.start_identified()
        self.assertIn('voice.send', self.controller.snapshot()['capabilities'])
        self.controller.plugin_action({'name': 'anti_recall_voice', 'action': 'enable'})
        self.assertTrue(self.controller.app.plugins.is_active('anti_recall_voice'))

    def test_group_mentions_keep_ids_and_deduplicate_without_member_directory(self):
        self.controller.save_configuration({'text_send_delay':{'enabled':False}})
        self.start_identified()
        data = dict(chat_id='123@chatroom', content='你好', at_list=['wxid_friend_fixture'],
                    session_id='linux-fixture-session', request_id='fixture-mention-00001')
        self.controller.send_text(data)
        wait_for(lambda: self.controller.send_status(data['request_id'])['status'] != 'sending')
        self.assertEqual(self.controller.send_status(data['request_id'])['status'], 'sent')
        self.controller.send_text(data)
        self.assertEqual(self.instances[0].calls,
            [('mention', '123@chatroom', '@wxid_friend_fixture\u2005\n你好', ('wxid_friend_fixture',))])
        with self.assertRaises(PanelError):
            self.controller.send_text(dict(data, session_id='stale', request_id='fixture-mention-00002'))

    def test_mention_and_group_event_plugins_are_supported(self):
        names = ('mention_chat', 'idiom_chain', 'picture_idiom', 'group_notice')
        for name in names:
            shutil.copytree(ROOT / 'plugins' / name, self.root / 'plugins' / name)
        rows = {row['name']:row for row in self.controller.plugin_rows()}
        for name in names:
            self.assertTrue(rows[name]['supported'], name)
        self.assertIn('group.members.read', PANEL_CAPABILITIES)
        self.assertIn('group.events', PANEL_CAPABILITIES)

    def test_linux_group_members_route_and_mention_display_names(self):
        self.controller.save_configuration({'text_send_delay':{'enabled':False}})
        self.start_identified()
        self.controller.guard_route('/api/group-members',False)
        self.instances[0].member_rows = [dict(wxid='wxid_friend_fixture',nickname='群内名字')]
        result = self.controller.group_members('123@chatroom')
        self.assertEqual(result['session_id'],'linux-fixture-session')
        self.assertEqual(result['members'][0]['nickname'],'群内名字')
        data = dict(chat_id='123@chatroom',content='hello',at_list=['wxid_friend_fixture'],
            session_id='linux-fixture-session',request_id='fixture-member-names-0001')
        self.controller.send_text(data)
        wait_for(lambda:self.controller.send_status(data['request_id'])['status']!='sending')
        self.assertEqual(self.instances[0].calls[0][2],'@群内名字\u2005\nhello')
        data.update(at_list=['wxid_absent'],request_id='fixture-member-names-0002')
        self.controller.send_text(data)
        wait_for(lambda:self.controller.send_status(data['request_id'])['status']!='sending')
        self.assertEqual(len(self.instances[0].calls),1)
        self.assertEqual(self.controller.send_status(data['request_id'])['status'],'failed')

    @unittest.skipUnless(shutil.which('gcc'), 'requires GCC and native Linux fixture')
    def test_panel_runtime_receives_native_events_and_plugin_replies_via_native_scheduler(self):
        obj, executable, receive, send = [self.root / name for name in ('sdk.o','wechat-fixture','receive.so','send.so')]
        subprocess.run(['gcc','-O2','-Dmain=send_fixture_main','-c',
            str(ROOT / 'tests/fixtures/linux_send_fixture.c'),'-o',str(obj)],check=True)
        subprocess.run(['gcc','-O2','-rdynamic','-pthread','-DHELPER_FIXTURE_ROUNDTRIP',
            str(ROOT / 'tests/fixtures/linux_bridge_cli_fixture.c'),str(obj),'-o',str(executable)],check=True)
        for source, flag, target in (('text_bridge.c','-DHELPER_FIXTURE_TEST',receive),
                                      ('send_bridge.c','-DSDK_FIXTURE',send)):
            subprocess.run(['gcc','-O2','-fPIC','-shared','-pthread',flag,
                str(ROOT / 'native/linux' / source),'-ldl','-o',str(target)],check=True)
        self.controller.wechat_path=executable
        self.controller.receive_bridge,self.controller.send_bridge=receive,send
        digest=hashlib.sha256(executable.read_bytes()).hexdigest()
        self.controller._backend_factory=lambda *a,**k: LinuxPanelBackend(*a,**k,_expected_executable_sha256=digest)
        self.controller.save_configuration({'text_send_delay':{'enabled':False}})
        config=self.controller.store.load()
        config['plugins']['echo']={'enabled':True,'prefix':'WX_LINUX_BRIDGE_07_'}
        self.controller.store._save(config)
        self.controller.start()
        wait_for(lambda: any(row['direction']=='outgoing' and row['content']=='SEND'
            for row in self.controller.message_tail()['rows']), timeout=6)
        wait_for(lambda: any(row['is_group'] for row in self.controller.message_tail()['rows']))
        rows=self.controller.message_tail()['rows']
        self.assertTrue(any(row['is_group'] and row['sender_id']=='wxid_friend_fixture' for row in rows))
        self.assertTrue(any(row['content']=='WX_LINUX_BRIDGE_07_SEND' for row in rows))
        self.assertIsNone(self.controller.store.load()['wechat_account'])
        backend=self.controller.app.backend
        self.controller.stop()
        wait_for(lambda: self.controller.snapshot()['state']=='stopped')
        if backend.process is not None:
            backend.process.wait(timeout=3)

    def test_http_uses_same_token_origin_guards_and_explicit_linux_capabilities(self):
        server = PanelServer(self.controller, 0)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        base = 'http://127.0.0.1:' + str(server.server_address[1])
        page = urlopen(base).read().decode()
        self.assertIn('name="helper-platform" content="linux"', page)
        def get(route, token=True):
            return urlopen(Request(base + route, headers={'X-Helper-Token':server.token} if token else {}))
        with self.assertRaises(HTTPError) as error:
            get('/api/state', False)
        self.assertEqual(error.exception.code, 403)
        with self.assertRaises(HTTPError) as error:
            get('/api/contacts')
        self.assertEqual(error.exception.code, 409)
        self.assertEqual(json.load(get('/api/state'))['platform'], 'linux')
        self.assertEqual(len(json.load(get('/api/robots'))['robots']), 1)
        request = Request(base + '/api/start', data=b'{}', method='POST', headers={
            'X-Helper-Token':server.token,'Content-Type':'application/json','Origin':'https://untrusted.invalid'})
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(self.instances, [])

    def prepare_emoticon_export(self, method=None):
        self.start_identified()
        backend = self.instances[0]
        backend.capabilities = frozenset(backend.capabilities) | {'emoticon.extract'}
        calls = []
        def export(*, convert=True):
            calls.append(convert)
            if method is not None:
                return method()
            path = self.root / 'exports' / 'account-fixture' / 'item' / 'original.gif'
            return (ExportedEmoticon(identifier='a' * 32, original_path=path, gif_path=path,
                    format='GIF', sendable=True),
                    ExportedEmoticon(identifier='b' * 32, error='original:decode_failed'))
        backend.export_emoticons = export
        data = dict(request_id='fixture-emoticon-export-01', session_id=backend.session_id, convert=False)
        return backend, calls, data

    def test_emoticon_export_is_bounded_background_job_and_deduplicated(self):
        _, calls, data = self.prepare_emoticon_export()
        self.controller.export_emoticons(data)
        wait_for(lambda: self.controller.emoticon_export_status(data['request_id'])['status'] == 'done')
        result = self.controller.export_emoticons(data)['result']
        self.assertEqual(calls, [False])
        self.assertEqual((result['count'], result['sendable_count']), (2, 1))
        self.assertEqual(result['output_directories'], [str(self.root / 'exports' / 'account-fixture')])
        self.assertEqual(result['items'][1]['error'], 'original:decode_failed')
        self.assertNotIn('identifier', json.dumps(result))
        with self.assertRaises(PanelError):
            self.controller.export_emoticons(dict(data, convert=True))
        self.assertEqual(self.instances[0].calls, [])

    def test_emoticon_export_rejects_missing_capability_session_and_bad_arguments(self):
        backend, calls, data = self.prepare_emoticon_export()
        for change in (dict(convert=1), dict(request_id='bad'), dict(session_id='old-session')):
            with self.assertRaises(PanelError):
                self.controller.export_emoticons(dict(data, **change))
        backend.capabilities = PANEL_CAPABILITIES - {'emoticon.extract'}
        with self.assertRaises(PanelError):
            self.controller.export_emoticons(data)
        self.assertEqual(calls, [])

    def test_emoticon_export_busy_and_stop_hide_late_result(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def slow():
            started.set()
            release.wait(3)
            return ()
        _, _, data = self.prepare_emoticon_export(slow)
        self.controller.export_emoticons(data)
        self.assertTrue(started.wait(1))
        with self.assertRaisesRegex(PanelError, '上一项'):
            self.controller.export_emoticons(dict(data, request_id='fixture-emoticon-export-02'))
        self.controller.stop()
        wait_for(lambda: self.controller.snapshot()['state'] == 'stopped')
        release.set()
        result = self.controller.emoticon_export_status(data['request_id'])
        self.assertEqual(result['status'], 'cancelled')
        self.assertNotIn('result', result)

    def test_emoticon_export_never_returns_exception_secrets(self):
        def failure():
            raise RuntimeError('private-key-fixture should never be returned')
        _, _, data = self.prepare_emoticon_export(failure)
        self.controller.export_emoticons(data)
        wait_for(lambda: self.controller.emoticon_export_status(data['request_id'])['status'] == 'failed')
        result = self.controller.emoticon_export_status(data['request_id'])
        self.assertNotIn('private-key', json.dumps(result))
        self.assertIsNone(result['result'])

    def test_emoticon_export_http_uses_token_and_no_store(self):
        _, calls, data = self.prepare_emoticon_export()
        server = PanelServer(self.controller, 0)
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        base = 'http://127.0.0.1:' + str(server.server_address[1])
        headers = {'X-Helper-Token': server.token, 'Content-Type': 'application/json'}
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(base + '/api/emoticons-export', data=json.dumps(data).encode(),
                            headers={'Content-Type': 'application/json'}))
        self.assertEqual(error.exception.code, 403)
        error.exception.close()
        with urlopen(Request(base + '/api/emoticons-export', data=json.dumps(data).encode(), headers=headers)) as response:
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        wait_for(lambda: self.controller.emoticon_export_status(data['request_id'])['status'] == 'done')
        with urlopen(Request(base + '/api/emoticons-export-status?id=' + data['request_id'], headers=headers)) as response:
            self.assertEqual(json.load(response)['status'], 'done')
        self.assertEqual(calls, [False])

    def test_emoticon_export_hub_proxy_forwards_to_worker_http(self):
        from wxbot_framework.robot_hub import RobotProxy
        _, calls, data = self.prepare_emoticon_export()
        worker = PanelServer(self.controller, 0)
        self.addCleanup(worker.server_close)
        threading.Thread(target=worker.serve_forever, daemon=True).start()
        self.addCleanup(worker.shutdown)
        worker_url = 'http://127.0.0.1:' + str(worker.server_address[1])
        forwarded = []
        class Hub:
            root = ROOT
            platform = 'linux'
            def robot(hub, identifier):
                if identifier != 'export-fixture':
                    raise PanelError('unknown fixture robot', 404)
                return RobotProxy(hub, identifier)
            def request(hub, identifier, route, body=None):
                forwarded.append((identifier, route))
                request = Request(worker_url + '/api/' + route,
                    data=None if body is None else json.dumps(body).encode(),
                    headers={'X-Helper-Token': worker.token, 'Content-Type': 'application/json'})
                with urlopen(request) as response:
                    return json.load(response)
        hub = Hub()
        server = PanelServer(hub, 0)
        self.addCleanup(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        base = 'http://127.0.0.1:' + str(server.server_address[1])
        headers = {'X-Helper-Token': server.token, 'Content-Type': 'application/json'}
        with urlopen(Request(base + '/api/emoticons-export?robot=export-fixture',
                data=json.dumps(data).encode(), headers=headers)) as response:
            self.assertIn(json.load(response)['status'], ('exporting', 'done'))
        wait_for(lambda: self.controller.emoticon_export_status(data['request_id'])['status'] == 'done')
        with urlopen(Request(base + '/api/emoticons-export-status?robot=export-fixture&id=' + data['request_id'],
                headers=headers)) as response:
            self.assertEqual(json.load(response)['result']['count'], 2)
        self.assertEqual(calls, [False])
        self.assertEqual(forwarded, [('export-fixture', 'emoticons-export'),
            ('export-fixture', 'emoticons-export-status?id=' + data['request_id'])])

    def test_emoticon_proxy_status_urlencodes_identifiers(self):
        from wxbot_framework.robot_hub import RobotProxy
        from unittest.mock import Mock
        hub = Mock()
        RobotProxy(hub, 'fixture-robot').emoticon_export_status('one & two?')
        hub.request.assert_called_once_with('fixture-robot',
            'emoticons-export-status?id=one+%26+two%3F', None)


if __name__ == '__main__':
    unittest.main()
