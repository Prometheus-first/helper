"""多账号配置、并行生命周期、HTTP 分流和真实工作进程退出；不注入微信。"""
import json
import http.client
from pathlib import Path
import shutil
import tempfile
import threading
import unittest

from test_web_panel import FakeApp, FakeLock
from wxbot_framework.robot_hub import RobotHub, RobotWorker
from wxbot_framework.web_panel import PanelController, PanelError, PanelServer, atomic_json, read_json

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTS = [{'pid':1234,'wxid':'wxid_test_robot','name':'测试账号甲'},
            {'pid':4321,'wxid':'wxid_other_robot','name':'测试账号乙'}]


class FakeWorker:
    def __init__(self, root, workspace):
        self.controller = PanelController(root,workspace=workspace,app_factory=FakeApp,
                                         scan=lambda root:ACCOUNTS,lock_factory=FakeLock)

    def request(self, route, data=None):
        controller=self.controller
        if route.startswith('plugin-business?'):
            from urllib.parse import parse_qs
            return controller.plugin_business(parse_qs(route.split('?',1)[1])['name'][0])
        if route=='plugin-business':return controller.save_plugin_business(data)
        if route.startswith('plugin-source?'):
            from urllib.parse import parse_qs
            return controller.plugin_editor(parse_qs(route.split('?',1)[1])['name'][0])
        if route in ('plugin-source','plugin-reset'):
            return controller.save_plugin_source(data,reset=route=='plugin-reset')
        if route=='state':return controller.snapshot()
        if route=='config' and data is None:return controller.configuration()
        if route=='accounts':return {'accounts':controller.accounts()}
        if route.startswith('logs?after='):return controller.logs.tail(int(route.split('=',1)[1]))
        if route.startswith('messages?after='):return controller.message_tail(int(route.split('=',1)[1]))
        actions={'start':controller.start,'stop':controller.stop,
                 'restart':controller.restart,
                 'account':lambda:controller.bind_account(data),
                 'config':lambda:controller.save_configuration(data),
                 'plugin':lambda:controller.plugin_action(data)}
        actions[route]()
        return {'ok':True}

    def close(self):self.controller.close()


class HubTests(unittest.TestCase):
    def setUp(self):
        scratch=ROOT/'.runtime/tests'
        scratch.mkdir(parents=True,exist_ok=True)
        temporary=tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name)
        for path in ROOT.glob('*.example.json'):shutil.copy2(path,self.root/path.name)
        shutil.copytree(ROOT/'plugins',self.root/'plugins',ignore=shutil.ignore_patterns('__pycache__'))
        self.hub=RobotHub(self.root,worker_factory=FakeWorker)
        self.addCleanup(self.hub.close)

    def test_new_profile_uses_empty_templates_and_keeps_default_unchanged(self):
        path=self.root/'llm_config.json'
        config=read_json(path);config['api_key']='private-default-example'
        atomic_json(path,config)
        original=path.read_bytes()
        entry=self.hub.create_robot({'name':'第二个机器人'})
        workspace=self.hub.workspace(entry['id'])
        self.assertNotEqual(workspace,self.root)
        self.assertEqual(read_json(workspace/'llm_config.json')['api_key'],'')
        self.assertEqual(read_json(workspace/'members_config.json')['groups'],{})
        self.assertIsNone(read_json(workspace/'bot_config.json')['wechat_account'])
        self.assertEqual(path.read_bytes(),original)
        recreated=RobotHub(self.root,worker_factory=FakeWorker)
        self.addCleanup(recreated.close)
        self.assertEqual(recreated.entries()[1],entry)

    def test_source_editor_proxy_is_scoped_to_selected_robot(self):
        entry=self.hub.create_robot({'name':'源码测试机器人'})
        a,b=self.hub.robot('default'),self.hub.robot(entry['id'])
        data=a.plugin_editor('echo')
        data['files']['__init__.py']+='\n# 当前账号的修改\n'
        saved=a.save_plugin_source(data)
        self.assertTrue(saved['customized'])
        self.assertFalse(b.plugin_editor('echo')['customized'])
        self.assertNotIn('# 当前账号的修改',b.plugin_editor('echo')['files']['__init__.py'])
        reset=a.save_plugin_source(saved,reset=True)
        self.assertFalse(reset['customized'])

    def test_business_editor_proxy_keeps_accounts_independent(self):
        entry=self.hub.create_robot({'name':'业务函数测试账号'})
        a,b=self.hub.robot('default'),self.hub.robot(entry['id'])
        data=a.plugin_business('echo');self.assertNotIn('files',data)
        key=next(iter(data['functions']));data['functions'][key]+='\n    # 本账号的业务逻辑\n'
        result=a.save_plugin_business(data);self.assertTrue(result['customized'])
        self.assertFalse(b.plugin_business('echo')['customized'])

    def test_two_bots_run_independently_with_distinct_configs(self):
        entry=self.hub.create_robot({'name':'第二个机器人'})
        a,b=self.hub.robot('default'),self.hub.robot(entry['id'])
        a.bind_account({'wxid':ACCOUNTS[0]['wxid'],'pid':1234})
        b.bind_account({'wxid':ACCOUNTS[1]['wxid'],'pid':4321})
        a.save_configuration({'groups':['123@chatroom']})
        b.save_configuration({'groups':['456@chatroom']})
        a.start();b.start()
        self.assertEqual(a.snapshot()['state'],'running')
        self.assertEqual(b.snapshot()['state'],'running')
        a.plugin_action({'name':'echo','action':'disable'})
        self.assertFalse(a.configuration()['plugins']['echo']['enabled'])
        self.assertTrue(b.configuration()['plugins']['echo']['enabled'])
        self.assertEqual(a.configuration()['groups'],['123@chatroom'])
        self.assertEqual(b.configuration()['groups'],['456@chatroom'])
        a.stop()
        self.hub.workers['default'].controller.worker.join(2)
        self.assertEqual(a.snapshot()['state'],'stopped')
        self.assertEqual(b.snapshot()['state'],'running')
        b.restart()
        self.assertEqual(b.snapshot()['state'],'running')

    def test_duplicate_account_and_unbound_start_are_rejected(self):
        entry=self.hub.create_robot({'name':'第二个机器人'})
        a,b=self.hub.robot('default'),self.hub.robot(entry['id'])
        with self.assertRaises(PanelError):b.start()
        a.bind_account({'wxid':ACCOUNTS[0]['wxid'],'pid':1234})
        with self.assertRaises(PanelError):b.bind_account({'wxid':ACCOUNTS[0]['wxid'],'pid':1234})
        self.assertIsNone(read_json(self.hub.workspace(entry['id'])/'bot_config.json')['wechat_account'])

    def test_concurrent_binding_can_only_claim_account_once(self):
        entry=self.hub.create_robot({'name':'第二个机器人'})
        barrier=threading.Barrier(2)
        results=[]
        def bind(identifier):
            barrier.wait()
            try:
                self.hub.robot(identifier).bind_account({'wxid':ACCOUNTS[0]['wxid'],'pid':1234})
                results.append(True)
            except PanelError:results.append(False)
        threads=[threading.Thread(target=bind,args=(identifier,)) for identifier in ('default',entry['id'])]
        for thread in threads:thread.start()
        for thread in threads:thread.join(3)
        self.assertEqual(sorted(results),[False,True])

    def test_invalid_name_unknown_profile_and_corrupt_registry_are_rejected(self):
        for name in ('', 'a'*33, 'bad\nname', None):
            with self.subTest(name=name),self.assertRaises(PanelError):self.hub.create_robot({'name':name})
        with self.assertRaises(PanelError):self.hub.robot('../bot_config.json')
        atomic_json(self.hub.registry,{'robots':[{'id':'../../outside','name':'bad'}]})
        with self.assertRaises(PanelError):self.hub.entries()

    def test_http_routes_profile_changes_only_to_the_selected_bot(self):
        server=PanelServer(self.hub,0)
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        def request(route,data=None):
            conn=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=5)
            conn.request('GET' if data is None else 'POST',route,
                body=None if data is None else json.dumps(data),
                headers={'X-Helper-Token':server.token,'Content-Type':'application/json'})
            response=conn.getresponse()
            status,body=response.status,json.loads(response.read())
            conn.close()
            return status,body
        try:
            status,entry=request('/api/robots',{'name':'第二个机器人'})
            self.assertEqual(status,200)
            identifier=entry['id']
            self.assertEqual(len(request('/api/robots')[1]['robots']),2)
            self.assertEqual(request('/api/config?robot='+identifier,{'groups':['456@chatroom']})[0],200)
            self.assertEqual(request('/api/config?robot=default')[1]['groups'],[])
            self.assertEqual(request('/api/config?robot='+identifier)[1]['groups'],['456@chatroom'])
            self.assertEqual(request('/api/state?robot=unknown')[0],404)
            self.assertEqual(request('/api/start?robot='+identifier,{})[0],409)
            self.assertEqual(request('/api/logs?after=0&robot='+identifier)[0],200)
        finally:
            server.shutdown();server.server_close();thread.join(2)


class WorkerProcessTests(unittest.TestCase):
    def test_real_worker_processes_have_separate_configs_and_exit_on_parent_pipe_close(self):
        scratch=ROOT/'.runtime/robots'
        scratch.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch,prefix='worker-test-') as temporary:
            a_root,b_root=Path(temporary)/'a',Path(temporary)/'b'
            for workspace in (a_root,b_root):
                controller=PanelController(ROOT,workspace=workspace)
                controller.close()
            a,b=RobotWorker(ROOT,a_root),RobotWorker(ROOT,b_root)
            try:
                self.assertEqual(a.request('state')['state'],'stopped')
                self.assertEqual(b.request('state')['state'],'stopped')
                self.assertNotEqual(a.process.pid,b.process.pid)
                self.assertNotEqual(a.endpoint,b.endpoint)
                a.request('config',{'llm':{'model':'worker-model-a'}})
                self.assertEqual(a.request('config')['llm']['model'],'worker-model-a')
                self.assertNotEqual(b.request('config')['llm']['model'],'worker-model-a')
                a.close()
                self.assertIsNotNone(a.process.poll())
                self.assertEqual(b.request('state')['state'],'stopped')
            finally:
                a.close();b.close()
            self.assertIsNotNone(b.process.poll())


if __name__=='__main__':unittest.main()
