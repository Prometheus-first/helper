"""业务函数视图与局部替换：不展示生命周期，不改写函数外代码。"""
import ast
from copy import deepcopy
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest

from test_plugin_editor import EditingApp
from test_web_panel import PanelFixture
from wxbot_framework.plugin_business import merge, view
from wxbot_framework.plugin_sources import PluginSources
from wxbot_framework.sdk import Message
from wxbot_framework.web_panel import PanelError, PanelServer

SOURCE='''import math
DEFAULT = "unchanged"

class Plugin:
    def on_load(self, ctx):
        self.prefix = "framework-state"

    @staticmethod
    def format_reply(text):
        """只处理回复文案。"""
        return text + "!"

    def on_message(self, message, ctx):
        ctx.reply(message, self.format_reply(message.text))

    def on_unload(self, ctx):
        self.prefix = None
'''


def snapshot():
    manifest={'business':[{'function':'Plugin.format_reply','label':'回复文案'},
                          {'function':'Plugin.on_message','label':'处理消息'}]}
    return {'name':'echo','revision':'version','options':{},'customized':False,
            'files':{'__init__.py':SOURCE,'plugin.json':json.dumps(manifest),'README.md':'说明'}}


class FunctionTests(unittest.TestCase):
    def test_view_contains_functions_only_no_module_class_imports_or_lifecycle(self):
        result=view(snapshot())
        self.assertNotIn('files',result)
        self.assertEqual(len(result['functions']),2)
        for code in result['functions'].values():
            self.assertNotIn('import math',code)
            self.assertNotIn('class Plugin',code)
            self.assertNotIn('on_load',code)
            self.assertNotIn('on_unload',code)
            self.assertEqual(len(ast.parse(code).body),1)

    def test_multiple_edits_preserve_imports_lifecycle_config_and_other_files(self):
        original=snapshot();functions=view(original)['functions']
        functions['__init__.py:Plugin.format_reply']=functions['__init__.py:Plugin.format_reply'].replace('text + "!"','text.upper()')
        functions['__init__.py:Plugin.on_message']+='    # 新增业务逻辑\n'
        result=merge(original['files'],functions)
        compile(result['__init__.py'],'plugin.py','exec')
        self.assertIn('import math\nDEFAULT = "unchanged"',result['__init__.py'])
        self.assertIn('    def on_load(self, ctx):\n        self.prefix = "framework-state"',result['__init__.py'])
        self.assertIn('    def on_unload(self, ctx):\n        self.prefix = None',result['__init__.py'])
        self.assertIn('return text.upper()',result['__init__.py'])
        self.assertIn('# 新增业务逻辑',result['__init__.py'])
        self.assertEqual(result['plugin.json'],original['files']['plugin.json'])
        self.assertEqual(result['README.md'],'说明')

    def test_signature_decorators_and_top_level_code_cannot_be_changed(self):
        data=snapshot();original=view(data)['functions']
        key='__init__.py:Plugin.format_reply';code=original[key]
        bad=[code.replace('format_reply','on_load'),code.replace('(text)','(text, extra)'),
             code.replace('@staticmethod','@property'),code+'\nimport os\n',
             code+'\ndef other(): pass\n',code.replace('def ','async def ',1)]
        for value in bad:
            with self.subTest(value=value):
                changed=dict(original);changed[key]=value
                with self.assertRaises(ValueError):merge(data['files'],changed)

    def test_syntax_error_points_to_displayed_business_function(self):
        data=snapshot();functions=view(data)['functions']
        functions['__init__.py:Plugin.on_message']='def on_message(self, message, ctx):\n    if (:\n        pass\n'
        with self.assertRaisesRegex(ValueError,'处理消息 第 2 行'):merge(data['files'],functions)

    def test_trailing_business_comments_survive_save_and_reopen(self):
        data=snapshot();functions=view(data)['functions']
        key='__init__.py:Plugin.format_reply'
        functions[key]+='    # 我的业务说明\n'
        updated=merge(data['files'],functions)
        data['files']=updated
        self.assertIn('# 我的业务说明',view(data)['functions'][key])
        updated_again=merge(data['files'],view(data)['functions'])
        self.assertEqual(updated_again['__init__.py'].count('# 我的业务说明'),1)

    def test_tab_indented_plugin_keeps_its_class_indentation(self):
        data=snapshot();data['files']['plugin.json']='{}'
        data['files']['__init__.py']='class Plugin:\n\tdef on_load(self,c): pass\n\tdef on_message(self,m,c):\n\t\tc.reply(m,"old")\n'
        functions=view(data)['functions'];key=next(iter(functions))
        functions[key]=functions[key].replace('"old"','"new"')
        result=merge(data['files'],functions)
        compile(result['__init__.py'],'plugin.py','exec')
        self.assertIn('\tdef on_load(self,c): pass',result['__init__.py'])

    def test_lifecycle_function_cannot_be_injected_into_save_payload(self):
        data=snapshot();functions=view(data)['functions']
        functions['__init__.py:Plugin.on_load']='def on_load(self, ctx): pass\n'
        with self.assertRaises(ValueError):merge(data['files'],functions)

    def test_lifecycle_cannot_be_declared_as_a_business_entry(self):
        data=snapshot();data['files']['plugin.json']=json.dumps({'business':[{'function':'Plugin.on_load'}]})
        with self.assertRaises(ValueError):view(data)

    def test_metadata_can_select_a_function_in_a_separate_business_module(self):
        data=snapshot();data['files']['business.py']='def reply(text):\n    return text\n'
        data['files']['plugin.json']=json.dumps({'business':[{'file':'business.py','function':'reply','label':'回复规则'}]})
        functions=view(data)['functions'];self.assertEqual(list(functions),['business.py:reply'])
        functions['business.py:reply']=functions['business.py:reply'].replace('return text','return text.upper()')
        result=merge(data['files'],functions)
        self.assertEqual(result['__init__.py'],SOURCE)
        self.assertIn('return text.upper()',result['business.py'])

    def test_legacy_plugins_fall_back_to_message_function_without_showing_files(self):
        data=snapshot();data['files']['plugin.json']='{}'
        result=view(data)
        self.assertEqual(list(result['functions']),['__init__.py:Plugin.on_message'])
        self.assertNotIn('files',result)

    def test_every_bundled_plugin_has_a_business_entry_without_lifecycle_functions(self):
        root=Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            sources=PluginSources(root/'plugins',temporary)
            for directory in (root/'plugins').iterdir():
                if not directory.is_dir() or directory.name.startswith('_'):continue
                with self.subTest(plugin=directory.name):
                    result=view(sources.snapshot(directory.name,{}))
                    self.assertTrue(result['functions'])
                    for code in result['functions'].values():
                        self.assertNotIn('def on_load(',code)
                        self.assertNotIn('def on_unload(',code)


class ControllerBusinessTests(PanelFixture):
    def setUp(self):
        super().setUp()
        self.source=self.root/'plugins/echo/__init__.py'
        self.source.write_text('class Plugin:\n def on_load(self,c): self.state="keep"\n'
                               ' def on_message(self,m,c): c.reply(m,"默认回复")\n',encoding='utf-8')
        self.controller.factory=EditingApp

    def business(self):return self.controller.plugin_business('echo')

    def changed(self):
        data=deepcopy(self.business());key=next(iter(data['functions']))
        data['functions'][key]=data['functions'][key].replace('默认回复','业务修改')
        return data

    def test_running_business_edit_is_applied_and_keeps_framework_code(self):
        before=self.source.read_text('utf-8');self.controller.start()
        result=self.controller.save_plugin_business(self.changed())
        self.assertTrue(result['applied']);self.assertNotIn('files',result)
        app=self.controller.app;app.sent_event.clear()
        app.plugins.dispatch(Message(text='test',chat_id='filehelper',session_id='editing-session'))
        self.assertTrue(app.sent_event.wait(2));self.assertEqual(app.sent[-1][1],'业务修改')
        self.assertEqual(app.plugins.records['echo'].instance.state,'keep')
        self.assertEqual(self.source.read_text('utf-8'),before)
        self.wait_stopped()

    def test_reset_restores_default_business_and_options(self):
        changed=self.changed();changed['options']['prefix']='custom '
        saved=self.controller.save_plugin_business(changed)
        saved['action']='reset'
        result=self.controller.save_plugin_business(saved)
        self.assertFalse(result['customized']);self.assertNotIn('files',result)
        self.assertIn('默认回复',next(iter(result['functions'].values())))
        self.assertEqual(result['options']['prefix'],'echo ')

    def test_invalid_function_keeps_original_revision(self):
        before=self.business();data=self.changed()
        key=next(iter(data['functions']));data['functions'][key]='raise RuntimeError("file-level")'
        with self.assertRaises(ValueError):self.controller.save_plugin_business(data)
        self.assertEqual(self.business()['revision'],before['revision'])

    def test_stale_business_draft_cannot_overwrite_latest_saved_function(self):
        data=self.changed();self.controller.save_plugin_business(data)
        with self.assertRaises(PanelError) as caught:self.controller.save_plugin_business(data)
        self.assertEqual(caught.exception.status,409)

    def test_http_business_read_save_reset_never_returns_whole_source(self):
        server=PanelServer(self.controller,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        def request(path,data=None,token=True):
            connection=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=5)
            headers={'Content-Type':'application/json'}
            if token:headers['X-Helper-Token']=server.token
            connection.request('POST' if data is not None else 'GET',path,body=None if data is None else json.dumps(data),headers=headers)
            response=connection.getresponse();result=response.status,json.loads(response.read());connection.close();return result
        try:
            self.assertEqual(request('/api/plugin-business?name=echo',token=False)[0],403)
            status,data=request('/api/plugin-business?name=echo');self.assertEqual(status,200);self.assertNotIn('files',data)
            key=next(iter(data['functions']));data['functions'][key]=data['functions'][key].replace('默认回复','网页业务')
            status,saved=request('/api/plugin-business',data);self.assertEqual(status,200);self.assertNotIn('files',saved)
            saved['action']='reset';status,result=request('/api/plugin-business',saved)
            self.assertEqual(status,200);self.assertNotIn('files',result)
        finally:server.shutdown();server.server_close();thread.join(2)


if __name__=='__main__':unittest.main()
