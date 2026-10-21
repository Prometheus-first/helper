"""账号发现与批量启动：使用虚拟账号和 FakeApp，不访问或启动真实微信。"""
import http.client
import json
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_robot_hub import FakeWorker
from wxbot_framework.robot_hub import RobotHub
from wxbot_framework.web_panel import PanelError, PanelServer, atomic_json, read_json

ROOT = Path(__file__).resolve().parents[1]


class AccountSelectionTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.runtime/tests'
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for source in ROOT.glob('*.example.json'):
            shutil.copy2(source, self.root / source.name)
        shutil.copytree(ROOT / 'plugins/echo', self.root / 'plugins/echo', ignore=shutil.ignore_patterns('__pycache__'))
        self.accounts = [{'pid': 1234, 'wxid': 'wxid_test_robot', 'name': '测试账号甲'},
                         {'pid': 4321, 'wxid': 'wxid_other_robot', 'name': '测试账号乙'},
                         {'pid': 5678, 'wxid': 'wxid_unselected', 'name': '不启动的账号'}]
        self.hub = RobotHub(self.root, worker_factory=FakeWorker, scan=lambda root: self.accounts)
        self.addCleanup(self.hub.close)

    def test_discovery_has_no_binding_config_or_worker_side_effects(self):
        before = {path: path.read_bytes() for path in self.root.glob('*config.json')}
        registry = self.hub.registry.read_bytes()
        result = self.hub.logged_accounts()
        self.assertEqual(len(result['accounts']), 3)
        self.assertTrue(all(row['selectable'] and row['robot_id'] is None for row in result['accounts']))
        self.assertEqual(self.hub.workers, {})
        self.assertEqual(self.hub.registry.read_bytes(), registry)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_prepare_without_starting_and_repeat_reuses_same_profile(self):
        first = self.hub.prepare_account(self.accounts[0])
        second = self.hub.prepare_account(self.accounts[1])
        self.assertEqual(first['id'], 'default')
        self.assertNotEqual(second['id'], 'default')
        self.assertEqual(self.hub.workers, {})
        self.assertEqual(self.hub.prepare_account(self.accounts[1]), second)
        self.assertEqual(len(self.hub.entries()), 2)

    def test_only_selected_accounts_start_and_keep_independent_private_config(self):
        llm = read_json(self.root / 'llm_config.json')
        llm['api_key'] = 'private-default-example'
        atomic_json(self.root / 'llm_config.json', llm)
        history = self.root / '.runtime/history-marker'
        history.write_text('existing-history', encoding='utf-8')
        result = self.hub.start_selected({'accounts': self.accounts[:2]})
        self.assertEqual((result['started'], result['failed']), (2, 0))
        self.assertEqual(len(self.hub.entries()), 2)
        self.assertEqual(len(self.hub.workers), 2)
        second = self.hub.workspace(result['results'][1]['robot_id'])
        self.assertEqual(read_json(second / 'llm_config.json')['api_key'], '')
        self.assertEqual(read_json(second / 'members_config.json')['groups'], {})
        self.assertEqual(read_json(self.root / 'llm_config.json')['api_key'], 'private-default-example')
        self.assertEqual(history.read_text(encoding='utf-8'), 'existing-history')
        self.assertFalse(any(row.get('account') == 'wxid_unselected' for row in self.hub.robot_list()['robots']))
        self.assertTrue(all(worker.controller.snapshot()['state'] == 'running' for worker in self.hub.workers.values()))

    def test_known_account_keeps_config_across_hub_restart_and_new_pid(self):
        prepared = self.hub.prepare_account(self.accounts[0])
        config = read_json(self.root / 'members_config.json')
        config['groups'] = {'123@chatroom': {'summary': {'enabled': True}}}
        atomic_json(self.root / 'members_config.json', config)
        before = (self.root / 'members_config.json').read_bytes()
        self.hub.close()
        self.accounts[0]['pid'] = 6789
        self.hub = RobotHub(self.root, worker_factory=FakeWorker, scan=lambda root: self.accounts)
        self.addCleanup(self.hub.close)
        result = self.hub.start_selected({'accounts': [self.accounts[0]]})
        self.assertEqual(result['results'][0]['robot_id'], prepared['id'])
        self.assertEqual(len(self.hub.entries()), 1)
        self.assertEqual((self.root / 'members_config.json').read_bytes(), before)

    def test_stale_selection_fails_without_claiming_another_login(self):
        stale = {**self.accounts[0], 'pid': 9999}
        result = self.hub.start_selected({'accounts': [stale]})
        self.assertEqual(result['failed'], 1)
        self.assertIsNone(read_json(self.root / 'bot_config.json')['wechat_account'])
        self.assertEqual(self.hub.workers, {})
        self.assertEqual(len(self.hub.entries()), 1)

    def test_duplicate_login_is_unselectable_and_does_not_block_other_selected_account(self):
        self.accounts.append({**self.accounts[0], 'pid': 8765})
        listed = self.hub.logged_accounts()['accounts']
        self.assertFalse(listed[0]['selectable'])
        result = self.hub.start_selected({'accounts': self.accounts[:2]})
        self.assertEqual((result['failed'], result['started']), (1, 1))
        self.assertEqual(read_json(self.root / 'bot_config.json')['wechat_account'], self.accounts[1]['wxid'])

    def test_malformed_or_duplicate_requests_make_no_changes(self):
        original = (self.root / 'bot_config.json').read_bytes()
        for value in (None, [], [{}], [self.accounts[0], self.accounts[0]], [{'wxid': 'x', 'pid': True}]):
            with self.subTest(value=value), self.assertRaises(PanelError):
                self.hub.start_selected({'accounts': value})
        self.assertEqual((self.root / 'bot_config.json').read_bytes(), original)
        self.assertEqual(self.hub.workers, {})

    def test_concurrent_repeated_start_is_idempotent(self):
        barrier = threading.Barrier(2)
        results = []
        def start():
            barrier.wait()
            results.append(self.hub.start_selected({'accounts': [self.accounts[0]]}))
        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(sum(result['started'] for result in results), 1)
        self.assertEqual(sum(result['already_running'] for result in results), 1)
        self.assertEqual(len(self.hub.workers), 1)
        self.assertEqual(len(self.hub.entries()), 1)

    def test_worker_failure_is_reported_per_account_and_redacted(self):
        original = FakeWorker.request
        def request(worker, route, data=None):
            if route == 'start' and worker.controller.store.load()['wechat_account'] == self.accounts[0]['wxid']:
                raise PanelError('worker error sk-' + 'example_not_a_real_api_key_123456')
            return original(worker, route, data)
        with patch.object(FakeWorker, 'request', request):
            result = self.hub.start_selected({'accounts': self.accounts[:2]})
        self.assertEqual((result['failed'], result['started']), (1, 1))
        self.assertNotIn('sk-example', result['results'][0]['error'])
        retry = self.hub.start_selected({'accounts': [self.accounts[0]]})
        self.assertEqual(retry['started'], 1)
        self.assertEqual(len(self.hub.entries()), 2)

    def test_global_http_routes_keep_auth_and_do_not_require_profile_selection(self):
        server = PanelServer(self.hub, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def request(route, data=None, token=None, origin=None):
            connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=5)
            headers = {'X-Helper-Token': server.token if token is None else token, 'Content-Type': 'application/json'}
            if origin: headers['Origin'] = origin
            connection.request('GET' if data is None else 'POST', route,
                               body=None if data is None else json.dumps(data), headers=headers)
            response = connection.getresponse()
            result = response.status, json.loads(response.read())
            connection.close()
            return result
        try:
            self.assertEqual(len(request('/api/accounts?robot=unknown')[1]['accounts']), 3)
            self.assertEqual(request('/api/accounts/prepare', self.accounts[0])[0], 200)
            self.assertEqual(self.hub.workers, {})
            body = {'accounts': self.accounts[:2]}
            self.assertEqual(request('/api/accounts/start', body, token='invalid')[0], 403)
            self.assertEqual(request('/api/accounts/start', body, origin='https://example.invalid')[0], 403)
            status, result = request('/api/accounts/start?robot=unknown', body)
            self.assertEqual((status, result['started']), (200, 2))
        finally:
            server.shutdown();server.server_close();thread.join(2)


if __name__ == '__main__':
    unittest.main()
