"""本地管理台：进程所有权、配置保留、任务线程和 HTTP 隔离。"""
from concurrent.futures import Future
import http.client
import json
import logging
from pathlib import Path
import queue
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from wxbot_framework.config import ConfigStore
from wxbot_framework.runtime import BotApplication
from wxbot_framework.web_panel import LogBuffer, PanelController, PanelError, PanelServer, read_json

ROOT = Path(__file__).resolve().parents[1]


class FakeLock:
    held = set()

    def __init__(self, path):
        self.path, self.acquired = str(path), False

    def acquire(self):
        if self.path in self.held:
            raise RuntimeError('已经运行')
        self.held.add(self.path)
        self.acquired = True
        return self

    def close(self):
        if self.acquired:
            self.held.discard(self.path)
            self.acquired = False

    __enter__ = acquire

    def __exit__(self, *_):
        self.close()


class FakePlugins:
    def __init__(self, store):
        self.store = store
        self.active = False
        self.thread_ids = []

    def status(self):
        enabled = self.store.load()['plugins']['echo']['enabled']
        return [{'name':'echo', 'enabled':enabled, 'active':self.active, 'description':'测试回声', 'errors':0}]

    def enable(self, name):
        self.thread_ids.append(threading.get_ident())
        self.store.set_enabled(name, True)
        self.active = True

    def disable(self, name):
        self.thread_ids.append(threading.get_ident())
        self.store.set_enabled(name, False)
        self.active = False

    def reload(self, name):
        self.thread_ids.append(threading.get_ident())


class FakeApp:
    submit_control = BotApplication.submit_control
    _drain_controls = BotApplication._drain_controls

    def __init__(self, root, store):
        self.config = store.load()
        self.plugins = FakePlugins(store)
        self.stop_event, self.stopped_event = threading.Event(), threading.Event()
        self.controls = queue.Queue(maxsize=64)
        self._connection_ready = False
        self._last_connection_error = ''
        self.backend = SimpleNamespace(pid=1234, chats=lambda:[{'chat_id':'123@chatroom','is_group':True}])

    def run(self):
        self.thread_id = threading.get_ident()
        self._connection_ready = True
        try:
            while not self.stop_event.wait(.01):
                self._drain_controls()
        finally:
            self.stop()

    def stop(self):
        self.stop_event.set()
        self._drain_controls(stopping=True)
        self.stopped_event.set()


class PanelFixture(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.runtime/tests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for path in ROOT.glob('*.example.json'):
            shutil.copy2(path, self.root/path.name)
        (self.root/'plugins/echo').mkdir(parents=True)
        (self.root/'plugins/echo/__init__.py').write_text('',encoding='utf-8')
        (self.root/'plugins/echo/plugin.json').write_text(json.dumps({'description':'回声','default_enabled':True}),encoding='utf-8')
        self.accounts = [{'pid':1234,'wxid':'wxid_test_robot','name':'测试账号'}]
        self.controller = PanelController(self.root, app_factory=FakeApp, scan=lambda root:self.accounts, lock_factory=FakeLock)
        self.addCleanup(self.controller.close)

    def wait_stopped(self):
        self.controller.stop()
        self.controller.worker.join(2)
        self.assertFalse(self.controller.worker.is_alive())


class ControllerTests(PanelFixture):
    def test_real_plugin_descriptions_survive_start_reload_disable_and_stop(self):
        from wxbot_framework.backend import CAPABILITIES
        from wxbot_framework.plugins import PluginManager
        from wxbot_framework.sdk import ServiceFacade

        (self.root/'plugins/echo/__init__.py').write_text(
            'class Plugin:\n def on_message(self, message, context): pass\n', encoding='utf-8')

        class PluginApp(FakeApp):
            def __init__(self, root, store):
                super().__init__(root, store)
                self.service = ServiceFacade(text_delay={'enabled':False})
                self.plugins = PluginManager(root/'plugins', store, self.service, CAPABILITIES)
                self.plugins.load_one('echo')

            def stop(self):
                try:
                    super().stop()
                finally:
                    self.plugins.shutdown()
                    self.service.close()

        self.controller.factory = PluginApp

        def assert_description(active):
            row = next(row for row in self.controller.snapshot()['plugins'] if row['name']=='echo')
            self.assertEqual(row.get('description'), '回声')
            self.assertEqual(row['active'], active)

        assert_description(False)
        self.controller.start()
        assert_description(True)
        self.controller.plugin_action({'name':'echo','action':'reload'})
        assert_description(True)
        self.controller.plugin_action({'name':'echo','action':'disable'})
        assert_description(False)
        self.wait_stopped()
        assert_description(False)

    def test_start_plugin_operation_uses_runtime_thread_then_stop_releases_lock(self):
        self.controller.start()
        with self.assertRaises(PanelError):
            self.controller.start()
        app = self.controller.app
        self.controller.plugin_action({'name':'echo','action':'enable'})
        self.assertEqual(app.plugins.thread_ids, [app.thread_id])
        self.assertNotEqual(app.thread_id, threading.get_ident())
        self.assertEqual(self.controller.chats()[0]['chat_id'], '123@chatroom')
        with self.assertRaises(PanelError):
            self.controller.save_configuration({'groups':[]})
        self.wait_stopped()
        self.assertEqual(self.controller.snapshot()['state'], 'stopped')
        self.controller.start()
        self.wait_stopped()

    def test_multi_account_requires_explicit_binding_and_stale_selection_rejected(self):
        self.accounts.append({'pid':4321,'wxid':'wxid_other_robot','name':'另一个账号'})
        with self.assertRaises(PanelError):
            self.controller.start()
        self.assertIsNone(self.controller.store.load()['wechat_account'])
        with self.assertRaises(PanelError):
            self.controller.bind_account({'wxid':'wxid_test_robot','pid':5678})
        self.controller.bind_account({'wxid':'wxid_other_robot','pid':4321})
        self.assertEqual(self.controller.store.load()['wechat_account'], 'wxid_other_robot')

    def test_other_console_instance_blocks_config_and_plugin_edits(self):
        with FakeLock(self.root/'.runtime/bot.lock'):
            for operation in (lambda:self.controller.start(),
                lambda:self.controller.save_configuration({'groups':[]}),
                lambda:self.controller.bind_account({'wxid':'wxid_test_robot','pid':1234}),
                lambda:self.controller.plugin_action({'name':'echo','action':'disable'})):
                with self.subTest(operation=operation), self.assertRaises(PanelError):
                    operation()

    def test_key_is_not_returned_blank_retains_and_explicit_clear_works(self):
        path = self.root/'llm_config.json'
        data = read_json(path)
        data.update(api_key='private-example-credential', extra_setting='keep me')
        path.write_text(json.dumps(data),encoding='utf-8')
        response = self.controller.configuration()
        self.assertNotIn('private-example-credential', json.dumps(response))
        self.assertTrue(response['llm']['api_key_configured'])
        self.controller.save_configuration({'llm':{'api_key':'','model':'new-model'}})
        self.assertEqual(read_json(path)['api_key'], 'private-example-credential')
        self.assertEqual(read_json(path)['extra_setting'], 'keep me')
        self.controller.save_configuration({'llm':{'clear_api_key':True}})
        self.assertEqual(read_json(path)['api_key'], '')
        self.assertGreater(len(list((self.root/'.runtime/web/backups').glob('*'))),0)

    def test_group_edit_keeps_profiles_and_adds_nonempty_allowlist(self):
        path = self.root/'members_config.json'
        data = read_json(path)
        data['groups'] = {'123@chatroom':[{'key':'test_person','name':'测试玩家'}]}
        path.write_text(json.dumps(data),encoding='utf-8')
        self.controller.save_configuration({'groups':['123@chatroom','456@chatroom'], 'chat_allowlist':['filehelper']})
        self.assertEqual(read_json(path)['groups']['123@chatroom'], data['groups']['123@chatroom'])
        self.assertEqual(self.controller.store.load()['chat_allowlist'], ['filehelper','123@chatroom','456@chatroom'])

    def test_group_names_round_trip_without_losing_other_settings(self):
        path = self.root/'members_config.json'
        data = read_json(path)
        data['groups'] = {'123@chatroom': [{'key':'test_person','name':'测试玩家'}], '456@chatroom': []}
        data['group_names'] = {'123@chatroom': '旧名称', '456@chatroom': '另一个群'}
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        self.controller.save_configuration({'groups':['123@chatroom'], 'group_names':{'123@chatroom':'  测试群  '}})
        saved = read_json(path)
        self.assertEqual(saved['groups']['123@chatroom'], data['groups']['123@chatroom'])
        self.assertEqual(saved['group_names'], {'123@chatroom':'测试群', '456@chatroom':'另一个群'})
        self.assertEqual(self.controller.configuration()['group_names'], saved['group_names'])
        self.controller.save_configuration({'group_names':{'123@chatroom':''}})
        self.assertNotIn('123@chatroom', read_json(path)['group_names'])

    def test_invalid_group_names_do_not_modify_configuration(self):
        path = self.root/'members_config.json'
        original = path.read_bytes()
        for names in ([], {'not-in-config@chatroom':'备注'}, {'123@chatroom':'x'*65}, {'123@chatroom':'含\n换行'}):
            with self.subTest(names=names), self.assertRaises(PanelError):
                self.controller.save_configuration({'groups':['123@chatroom'], 'group_names':names})
            self.assertEqual(path.read_bytes(), original)

    def test_compatible_llm_options_round_trip_without_changing_saved_key(self):
        path = self.root/'llm_config.json'
        original = read_json(path)
        original['api_key'] = 'private-model-key'
        path.write_text(json.dumps(original), encoding='utf-8')
        settings = {'base_url':'https://gateway.example.test/v1', 'model':'custom-model',
                    'json_mode':False, 'token_limit_parameter':'max_completion_tokens',
                    'extra_body':{'reasoning_effort':'low'}}
        self.controller.save_configuration({'llm':settings})
        saved = read_json(path)
        for key, value in settings.items():
            self.assertEqual(saved[key], value)
            self.assertEqual(self.controller.configuration()['llm'][key], value)
        self.assertEqual(saved['api_key'], 'private-model-key')
        self.assertNotIn('private-model-key', json.dumps(self.controller.configuration()))
        self.controller.save_configuration({'llm':{'extra_body':{}}})
        self.assertEqual(read_json(path)['extra_body'], {})

    def test_legacy_llm_config_has_compatible_defaults_and_invalid_edits_are_atomic(self):
        path = self.root/'llm_config.json'
        value = read_json(path)
        for key in ('json_mode', 'token_limit_parameter', 'extra_body'):
            value.pop(key, None)
        path.write_text(json.dumps(value), encoding='utf-8')
        settings = self.controller.configuration()['llm']
        self.assertTrue(settings['json_mode'])
        self.assertEqual(settings['token_limit_parameter'], 'max_tokens')
        self.assertEqual(settings['extra_body'], {})
        original = path.read_bytes()
        for edit in ({'json_mode':'false'}, {'token_limit_parameter':'invalid'}, {'extra_body':[]},
                     {'extra_body':{'stream':True}}):
            with self.subTest(edit=edit), self.assertRaises(ValueError):
                self.controller.save_configuration({'llm':edit})
            self.assertEqual(path.read_bytes(), original)

    def test_invalid_model_or_group_does_not_partially_save(self):
        original = {p:p.read_bytes() for p in self.root.glob('*_config.json')}
        for data in ({'llm':{'model':'changed'},'groups':['not-a-room']},
                     {'tts':{'max_chars_per_clip':500}}, {'llm':{'timeout_seconds':0}},
                     {'groups':'123@chatroom'}, {'daily_clear':1}):
            with self.subTest(data=data), self.assertRaises((PanelError, ValueError, RuntimeError)):
                self.controller.save_configuration(data)
            for path, raw in original.items():
                self.assertEqual(path.read_bytes(), raw)

    def test_write_failure_rolls_back_already_written_configs(self):
        from wxbot_framework.web_panel import atomic_json
        old = (self.root/'llm_config.json').read_bytes()
        def fail_on_main(path, data):
            if path.name == 'bot_config.json':
                raise OSError('模拟磁盘写入失败')
            atomic_json(path, data)
        with patch('wxbot_framework.web_panel.atomic_json',side_effect=fail_on_main):
            with self.assertRaises(OSError):
                self.controller.save_configuration({'llm':{'model':'new-model'}})
        self.assertEqual((self.root/'llm_config.json').read_bytes(), old)

    def test_cancelled_task_does_not_execute_and_shutdown_finishes_pending_tasks(self):
        app = FakeApp(self.root,self.controller.store)
        calls = []
        cancelled=app.submit_control(lambda:calls.append('wrong'))
        cancelled.cancel()
        app._drain_controls()
        self.assertEqual(calls,[])
        waiting=app.submit_control(lambda:None)
        app.stop()
        with self.assertRaises(RuntimeError):
            waiting.result()

    def test_offline_plugin_settings_persist_without_changing_other_plugins(self):
        before = self.controller.store.load()['plugins']['picture_idiom']
        self.controller.plugin_action({'name':'echo','action':'configure','options':{'enabled':False,'prefix':'ping '}})
        config = self.controller.store.load()
        self.assertEqual(config['plugins']['echo']['prefix'],'ping ')
        self.assertEqual(config['plugins']['picture_idiom'],before)
        with self.assertRaises(PanelError):
            self.controller.plugin_action({'name':'echo','action':'configure','options':{'enabled':'yes'}})


class HTTPTests(PanelFixture):
    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT/'webui', self.root/'webui')
        self.server = PanelServer(self.controller, 0)
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self,path='/',data=None,*,token=True,origin=None,host=None,raw=None,content_type='application/json'):
        conn=http.client.HTTPConnection('127.0.0.1',self.port,timeout=3)
        headers={}
        if token:headers['X-Helper-Token']=self.server.token
        if origin:headers['Origin']=origin
        if host:headers['Host']=host
        if data is not None or raw is not None:headers['Content-Type']=content_type
        body=json.dumps(data).encode() if data is not None else raw
        conn.request('POST' if body is not None else 'GET',path,body=body,headers=headers)
        response=conn.getresponse()
        result=response.status,dict(response.getheaders()),response.read()
        conn.close()
        return result

    def test_page_and_assets_are_local_and_token_is_not_in_url(self):
        status,headers,body=self.request(token=False)
        self.assertEqual(status,200)
        self.assertIn(self.server.token.encode(),body)
        self.assertEqual(headers['X-Frame-Options'],'DENY')
        self.assertEqual(headers['Cache-Control'],'no-store')
        for path in ('/app.js','/style.css'):
            self.assertEqual(self.request(path,token=False)[0],200)

    def test_scheduled_tasks_http_create_list_and_delete(self):
        self.controller.store.bind_account(self.accounts[0]['wxid'])
        payload = {'name':'测试任务', 'chat_id':'filehelper', 'content':'测试',
                   'kind':'interval', 'interval_seconds':300}
        self.assertEqual(self.request('/api/scheduled-tasks', payload, token=False)[0], 403)
        status, _, body = self.request('/api/scheduled-tasks', payload)
        self.assertEqual(status, 200)
        task = json.loads(body)['task']
        self.assertEqual(json.loads(self.request('/api/scheduled-tasks')[2])['tasks'][0]['id'], task['id'])
        self.assertEqual(self.request('/api/scheduled-tasks/delete', {'id':task['id']})[0], 200)
        self.assertEqual(json.loads(self.request('/api/scheduled-tasks')[2])['tasks'], [])

    def test_http_discovers_and_persists_declared_plugin_action(self):
        self.controller.store.bind_account(self.accounts[0]['wxid'])
        manifest = {'scheduled_actions':[{'id':'demo', 'label':'执行示例', 'method':'do_demo',
                    'fields':[{'name':'count', 'label':'次数', 'type':'integer', 'required':True}]}]}
        (self.root/'plugins/echo/plugin.json').write_text(json.dumps(manifest), encoding='utf-8')
        actions = json.loads(self.request('/api/scheduled-actions')[2])['actions']
        self.assertIn('plugin.echo.demo', [item['id'] for item in actions])
        payload = {'name':'测试', 'kind':'daily', 'daily_time':'10:30', 'action_id':'plugin.echo.demo',
                   'params':{'count':2}}
        status, _, body = self.request('/api/scheduled-tasks', payload)
        self.assertEqual(status, 200)
        task = json.loads(body)['task']
        self.assertEqual(task['params'], {'count':2})
        (self.root/'plugins/echo/plugin.json').write_text('{}', encoding='utf-8')
        self.assertEqual(self.request('/api/scheduled-tasks/toggle', {'id':task['id'], 'enabled':False})[0], 200)
        self.assertFalse(json.loads(self.request('/api/scheduled-tasks')[2])['tasks'][0]['enabled'])

    def test_api_rejects_missing_token_cross_origin_and_rebound_host(self):
        self.assertEqual(self.request('/api/config',token=False)[0],403)
        self.assertEqual(self.request('/api/config',origin='https://attacker.invalid')[0],403)
        self.assertEqual(self.request('/',host=f'attacker.invalid:{self.port}')[0],403)
        self.assertEqual(self.request('/api/start',{},origin='null')[0],403)

    def test_http_start_stop_and_secret_free_config(self):
        self.assertEqual(self.request('/api/config')[0],200)
        self.assertNotIn(b'"api_key":',self.request('/api/config')[2])
        self.assertEqual(self.request('/api/start',{})[0],200)
        self.assertEqual(self.request('/api/config',{'groups':[]})[0],409)
        self.assertEqual(self.request('/api/stop',{})[0],200)

    def test_member_and_send_routes_validate_token_and_preserve_connection(self):
        from wxbot_framework.sdk import ServiceFacade
        self.assertEqual(self.request('/api/send-connection')[0],409)
        self.controller.start()
        calls=[]
        backend=SimpleNamespace(session_id='http-test-session',pid=1234,stop=lambda: {},
            group_members=lambda room: ({'wxid':'wxid_test_member','nickname':'测试群友','role':'admin'},),
            send_at_text=lambda *args: calls.append(args) or True)
        service=ServiceFacade(text_delay={'enabled':False});service.bind(backend)
        self.addCleanup(service.close)
        self.controller.app.backend=backend;self.controller.app.service=service
        data=json.loads(self.request('/api/group-members?room=123%40chatroom')[2])
        self.assertEqual(data['members'][0]['role'],'admin')
        self.assertEqual(data['session_id'],'http-test-session')
        body={'request_id':'http-synthetic-request-001','session_id':data['session_id'],
              'chat_id':'123@chatroom','content':'你好','at_list':['wxid_test_member']}
        self.assertEqual(self.request('/api/send-text',body,token=False)[0],403)
        self.assertFalse(calls)
        self.assertEqual(self.request('/api/send-text',body)[0],200)
        for _ in range(100):
            result=json.loads(self.request('/api/send-status?id='+body['request_id'])[2])
            if result['status']!='sending':break
            time.sleep(.005)
        self.assertEqual(result['status'],'sent')
        self.assertEqual(calls,[('123@chatroom','@测试群友\u2005\n你好',('wxid_test_member',))])

    def test_invalid_json_oversized_body_and_unknown_routes(self):
        self.assertEqual(self.request('/api/config',raw=b'{broken')[0],400)
        self.assertEqual(self.request('/api/config',raw=b'x'*65537)[0],413)
        self.assertEqual(self.request('/api/config',raw=b'{}',content_type='text/plain')[0],400)
        self.assertEqual(self.request('/../bot_config.json')[0],404)
        self.assertEqual(self.request('/api/unknown',{})[0],404)

    def test_long_text_request_is_split_and_sent_in_order(self):
        from wxbot_framework.sdk import ServiceFacade
        self.controller.start()
        calls = []
        backend = SimpleNamespace(session_id='long-http-session', pid=1234, stop=lambda: {},
                                  send_text=lambda target, text: calls.append(text) or True)
        service = ServiceFacade(text_delay={'enabled':False})
        service.bind(backend)
        self.addCleanup(service.close)
        self.controller.app.backend = backend
        self.controller.app.service = service
        text = '长消息🌸' * 6000
        body = {'request_id':'long-http-request-0001', 'session_id':backend.session_id,
                'chat_id':'filehelper', 'content':text, 'at_list':[]}
        self.assertGreater(len(json.dumps(body).encode()), 65536)
        self.assertEqual(self.request('/api/send-text', body)[0], 200)
        for _ in range(200):
            result=json.loads(self.request('/api/send-status?id='+body['request_id'])[2])
            if result['status']!='sending': break
            time.sleep(.005)
        self.assertEqual(result['status'], 'sent')
        self.assertEqual(''.join(calls), text)
        self.assertTrue(all(len(part.encode('utf-8')) <= 1023 for part in calls))


class LogTests(unittest.TestCase):
    def test_log_ring_buffer_hides_common_keys_and_obeys_cursor(self):
        buffer=LogBuffer()
        buffer.handle(logging.LogRecord('Test',logging.INFO,'',0,'hello',(),None))
        buffer.handle(logging.LogRecord('Test',logging.WARNING,'',0,'key sk-'+'a'*24,(),None))
        rows=buffer.tail(after=1)
        self.assertEqual(len(rows['rows']),1)
        self.assertNotIn('a'*24,rows['rows'][0]['message'])
        self.assertEqual(rows['cursor'],2)


if __name__ == '__main__':
    unittest.main()
