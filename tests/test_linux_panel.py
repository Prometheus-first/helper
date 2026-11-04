"""Linux 网页管理台回归：仅使用合成账号和假后端，不启动真实微信。"""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from wxbot_framework.linux_panel_backend import LinuxPanelBackend, PANEL_CAPABILITIES
from wxbot_framework.linux_panel import LinuxPanelController
from wxbot_framework.message_monitor import MessageBuffer
from wxbot_framework.panel_common import PanelError
from wxbot_framework.panel_http import PanelServer
from wxbot_framework.sdk import Message

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
        self.assertFalse(recall.data['dispatch_to_recall_plugins'])
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

    def chats(self):
        return [{'chat_id':'wxid_friend_fixture', 'is_group':False, 'name':''}]

    def contacts(self):
        return {'account': self.account, 'session_id': self.session_id, 'source': 'wechat_database',
                'complete': False, 'scanned_at': '2026-01-01T00:00:00Z',
                'contacts': [{'wxid':'wxid_friend_fixture', 'nickname':'合成昵称', 'remark':'合成备注',
                              'alias':'', 'tags':['合成标签'], 'tag_ids':['7'], 'tags_status':'ready'}],
                'tags': [{'id':'7', 'name':'合成标签'}]}

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
        self.assertFalse(rows['anti_recall']['supported'])
        with self.assertRaises(PanelError):
            self.controller.plugin_action({'name':'anti_recall','action':'enable'})
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
        with self.assertRaises(PanelError):
            self.controller.send_text(dict(data, content='hello', at_list=['wxid_friend_fixture']))

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


if __name__ == '__main__':
    unittest.main()
