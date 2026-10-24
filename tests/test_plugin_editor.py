"""插件源码编辑、在线重载、失败回滚、重置及账号隔离；不连接微信。"""
from copy import deepcopy
import http.client
import json
from pathlib import Path
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_web_panel import FakeApp, FakeLock, PanelFixture
from wxbot_framework.backend import CAPABILITIES
from wxbot_framework.plugins import PluginManager
from wxbot_framework.plugin_sources import PluginSources
from wxbot_framework.sdk import Message, ServiceFacade
from wxbot_framework.web_panel import PanelController, PanelError, PanelServer, read_json


class EditingApp(FakeApp):
    def __init__(self, root, store):
        super().__init__(root, store)
        self.sent, self.sent_event = [], threading.Event()
        def send(chat, text):
            self.sent.append((chat, text)); self.sent_event.set(); return True
        self.backend = SimpleNamespace(pid=1234, session_id='editing-session', send_text=send)
        self.service = ServiceFacade(text_delay={'enabled':False})
        self.service.bind(self.backend)
        self.plugins = PluginManager(root/'plugins', store, self.service, CAPABILITIES)
        self.plugins.load_one('echo')

    def stop(self):
        try:
            super().stop()
        finally:
            self.plugins.shutdown()
            self.service.close()


class EditorTests(PanelFixture):
    def setUp(self):
        super().setUp()
        self.folder = self.root/'plugins/echo'
        (self.folder/'__init__.py').write_text(
            'from .helper import reply\nclass Plugin:\n def on_message(self,m,c): c.reply(m,reply())\n', encoding='utf-8')
        (self.folder/'helper.py').write_text('def reply(): return "默认回复"\n', encoding='utf-8')
        self.controller.factory = EditingApp

    def editor(self):
        return self.controller.plugin_editor('echo')

    def change(self, text='修改后的回复'):
        data = deepcopy(self.editor())
        data['files']['helper.py'] = f'def reply(): return {text!r}\n'
        return data

    def reply(self):
        app = self.controller.app
        app.sent_event.clear()
        app.plugins.dispatch(Message(text='test', chat_id='filehelper', session_id='editing-session'))
        self.assertTrue(app.sent_event.wait(2))
        return app.sent[-1][1]

    def test_edit_while_stopped_loads_on_next_start_and_survives_new_controller(self):
        before = (self.folder/'helper.py').read_bytes()
        result = self.controller.save_plugin_source(self.change())
        self.assertTrue(result['customized']); self.assertFalse(result['applied'])
        self.assertEqual((self.folder/'helper.py').read_bytes(), before)
        reopened = PanelController(self.root,app_factory=EditingApp,scan=lambda root:self.accounts,lock_factory=FakeLock)
        self.addCleanup(reopened.close)
        self.assertEqual(reopened.plugin_editor('echo')['revision'], result['revision'])
        self.controller.start()
        self.assertEqual(self.reply(), '修改后的回复')
        self.wait_stopped()

    def test_running_save_reloads_helpers_and_options_without_restarting_bot(self):
        self.controller.start(); app = self.controller.app
        self.assertEqual(self.reply(), '默认回复')
        data = self.change(); data['options']['prefix'] = 'ping '
        result = self.controller.save_plugin_source(data)
        self.assertTrue(result['applied'])
        self.assertIs(self.controller.app, app)
        self.assertEqual(self.reply(), '修改后的回复')
        self.assertEqual(app.plugins.records['echo'].context.config['prefix'], 'ping ')
        data = self.change('第二次修改')
        self.controller.save_plugin_source(data)
        self.assertEqual(self.reply(), '第二次修改')
        self.wait_stopped()

    def test_syntax_and_json_errors_leave_live_code_and_config_unchanged(self):
        self.controller.start(); before = self.editor()
        for file, invalid in (('helper.py', 'def broken(:'), ('plugin.json', '{broken'),
                              ('plugin.json', '{"message_types":[true]}')):
            data = self.change(); data['files'][file] = invalid
            with self.subTest(file=file), self.assertRaises(ValueError):
                self.controller.save_plugin_source(data)
            self.assertEqual(self.editor()['revision'], before['revision'])
            self.assertEqual(self.reply(), '默认回复')
        self.wait_stopped()

    def test_on_load_failure_rolls_back_files_options_and_running_instance(self):
        self.controller.start(); before = self.editor(); record = self.controller.app.plugins.records['echo']
        data = self.change(); data['options']['prefix']='changed'
        data['files']['__init__.py'] = 'class Plugin:\n def on_load(self,c): raise RuntimeError("故意失败")\n def on_message(self,m,c): pass\n'
        with self.assertRaisesRegex(PanelError, '已恢复原版本'):
            self.controller.save_plugin_source(data)
        self.assertEqual(self.editor()['revision'], before['revision'])
        self.assertIs(self.controller.app.plugins.records['echo'], record)
        self.assertEqual(self.reply(), '默认回复')
        self.wait_stopped()

    def test_plugin_system_exit_does_not_stop_robot_or_replace_original(self):
        self.controller.start(); data = self.change()
        data['files']['__init__.py'] = 'raise SystemExit(3)\n'
        with self.assertRaisesRegex(PanelError, '已恢复原版本'):
            self.controller.save_plugin_source(data)
        self.assertTrue(self.controller.worker.is_alive())
        self.assertEqual(self.reply(), '默认回复')
        self.wait_stopped()

    def test_config_write_failure_restores_source_pointer_and_original_plugin(self):
        self.controller.start(); before = self.editor()
        with patch.object(self.controller, '_save_many', side_effect=OSError('模拟写入失败')):
            with self.assertRaisesRegex(PanelError, '已恢复原版本'):
                self.controller.save_plugin_source(self.change())
        self.assertEqual(self.editor()['revision'], before['revision'])
        self.assertEqual(self.reply(), '默认回复')
        self.wait_stopped()

    def test_busy_callback_rejects_save_and_keeps_its_source_and_config(self):
        self.controller.start(); before=self.editor()
        entered,release=threading.Event(),threading.Event()
        record=self.controller.app.plugins.records['echo']
        def busy(message, context):entered.set();release.wait(5)
        record.instance.on_message=busy
        self.controller.app.plugins.dispatch(Message(text='hold',chat_id='filehelper',session_id='editing-session'))
        self.assertTrue(entered.wait(2))
        try:
            with self.assertRaisesRegex(PanelError, '已恢复原版本'):
                self.controller.save_plugin_source(self.change())
            self.assertEqual(self.editor()['revision'],before['revision'])
            self.assertIs(self.controller.app.plugins.records['echo'],record)
            self.assertTrue(record.active)
        finally:
            release.set();self.wait_stopped()

    def test_stale_edit_or_reset_cannot_overwrite_newer_changes(self):
        old = self.change(); self.controller.save_plugin_source(old)
        for reset in (False, True):
            with self.subTest(reset=reset), self.assertRaises(PanelError) as caught:
                self.controller.save_plugin_source(old, reset=reset)
            self.assertEqual(caught.exception.status, 409)

    def test_reset_restores_default_code_and_only_this_plugins_options(self):
        self.controller.start(); data = self.change()
        data['options'] = {'enabled':True,'prefix':'custom','extra':'remove me'}
        self.controller.save_plugin_source(data)
        before = self.controller.store.load()
        llm = (self.root/'llm_config.json').read_bytes()
        expected = read_json(self.root/'bot_config.example.json')['plugins']['echo']
        result = self.controller.save_plugin_source(self.editor(), reset=True)
        self.assertFalse(result['customized']); self.assertTrue(result['applied'])
        self.assertEqual(self.reply(), '默认回复')
        config = self.controller.store.load()
        self.assertEqual(config['plugins']['echo'], expected)
        config['plugins']['echo'] = before['plugins']['echo']
        self.assertEqual(config, before)
        self.assertEqual((self.root/'llm_config.json').read_bytes(), llm)
        self.wait_stopped()

    def test_disabled_plugin_stays_disabled_when_saving_source(self):
        self.controller.start(); data = self.change(); data['options']['enabled']=False
        result = self.controller.save_plugin_source(data)
        self.assertFalse(result['applied'])
        self.assertNotIn('echo', self.controller.app.plugins.records)
        self.wait_stopped()

    def test_workspace_overrides_never_change_other_robot(self):
        other = PanelController(self.root,workspace=self.root/'other-bot',app_factory=EditingApp,
                                scan=lambda root:self.accounts,lock_factory=FakeLock)
        self.addCleanup(other.close)
        self.controller.save_plugin_source(self.change())
        self.assertFalse(other.plugin_editor('echo')['customized'])
        self.assertIn('默认回复', other.plugin_editor('echo')['files']['helper.py'])
        sources = PluginSources(self.root/'plugins', other.workspace)
        self.assertEqual(sources.path('echo'), self.folder)

    def test_arbitrary_files_paths_and_oversized_source_are_rejected(self):
        for name in ('../echo', '_template', 'not_found'):
            with self.subTest(name=name), self.assertRaises(ValueError):self.controller.plugin_editor(name)
        for relative in ('../bot_config.json', '/outside.py', 'sub\\outside.py'):
            data=self.change();data['files'][relative]='pass'
            with self.subTest(path=relative), self.assertRaises(ValueError):self.controller.save_plugin_source(data)
        data=self.change();data['files']['helper.py']='#'*(256*1024+1)
        with self.assertRaises(ValueError):self.controller.save_plugin_source(data)

    def test_editor_reads_source_as_text_without_executing_it(self):
        (self.folder/'helper.py').write_text('raise RuntimeError("must not execute")\n', encoding='utf-8')
        self.assertIn('must not execute', self.editor()['files']['helper.py'])

    def test_windows_newlines_are_normalized_for_textarea_without_rewriting_defaults(self):
        original=b'def reply(): return "default"\r\n'
        (self.folder/'helper.py').write_bytes(original)
        self.assertEqual(self.editor()['files']['helper.py'],'def reply(): return "default"\n')
        self.assertEqual((self.folder/'helper.py').read_bytes(),original)

    def test_stopped_plugin_list_keeps_name_description_and_options_after_edit(self):
        data=self.change();data['options']['enabled']=False
        manifest=json.loads(data['files']['plugin.json']);manifest['description']='自定义描述'
        data['files']['plugin.json']=json.dumps(manifest,ensure_ascii=False)
        self.controller.save_plugin_source(data)
        row=next(row for row in self.controller.snapshot()['plugins'] if row['name']=='echo')
        self.assertEqual(row['description'],'自定义描述')
        self.assertFalse(row['enabled'])

    def test_http_routes_require_token_and_keep_generic_body_limit(self):
        server=PanelServer(self.controller,0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path, data=None, token=True):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=5)
            headers={'Content-Type':'application/json'}
            if token:headers['X-Helper-Token']=server.token
            connection.request('GET' if data is None else 'POST',path,
                               body=None if data is None else json.dumps(data).encode('utf-8'),headers=headers)
            response=connection.getresponse();result=response.status,json.loads(response.read());connection.close();return result
        try:
            path='/api/plugin-source?name=echo'
            self.assertEqual(request(path,token=False)[0],403)
            status,data=request(path);self.assertEqual(status,200)
            data['files']['helper.py']='#'*(70*1024)+'\ndef reply(): return "large source"\n'
            self.assertEqual(request('/api/plugin-source',data)[0],200)
            self.assertEqual(request('/api/plugin-reset',self.editor())[0],200)
            self.assertEqual(request('/api/config',{'padding':'x'*70000})[0],413)
        finally:
            server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
