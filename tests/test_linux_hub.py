"""Linux account discovery excludes historical sessions while retaining their data."""
from pathlib import Path
import tempfile
import unittest

from wxbot_framework.linux_hub import LinuxRobotHub
from wxbot_framework.web_panel import atomic_json


class Worker:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = []

    def request(self, route, body=None):
        self.calls.append((route, body))
        if route == 'state':
            return self.snapshot.copy()
        if route == 'linux-session/open':
            self.snapshot.update(login_state='starting', login_pending=True)
            return {'ok': True}
        raise AssertionError(route)

    def close(self):
        pass


class LinuxHubAccountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.hub = LinuxRobotHub(self.root, wechat_path='/tmp/wechat', worker_factory=lambda *_: Worker({}))
        self.addCleanup(self.hub.close)

    def test_refresh_excludes_saved_offline_profiles_but_keeps_pending_and_logged_in(self):
        inactive = self.hub.workspace('default') / 'identity.json'
        atomic_json(inactive, {'wxid': 'wxid_old_fixture', 'name': 'old'})
        pending = self.hub.create_robot({'name': 'pending'})['id']
        logged = self.hub.create_robot({'name': 'logged'})['id']
        self.hub.workers[pending] = Worker({'account':'', 'state':'stopped', 'connected':False,
            'logged_in':False, 'login_pending':True, 'login_state':'waiting_login', 'pid':101, 'error':''})
        self.hub.workers[logged] = Worker({'account':'wxid_active_fixture', 'state':'running', 'connected':True,
            'logged_in':True, 'login_pending':False, 'login_state':'logged_in', 'pid':102, 'error':''})
        rows = self.hub.logged_accounts()['accounts']
        self.assertEqual({row['robot_id'] for row in rows}, {pending, logged})
        self.assertEqual(len(self.hub.robot_list()['robots']), 3)
        self.assertTrue(inactive.exists())
        self.hub.workers[logged].snapshot.update(logged_in=False, connected=False, pid=None, login_state='closed')
        self.assertEqual([row['robot_id'] for row in self.hub.logged_accounts()['accounts']], [pending])

    def test_new_login_reuses_closed_profile_without_deleting_configuration(self):
        metadata = self.hub.workspace('default') / 'identity.json'
        atomic_json(metadata, {'wxid': 'wxid_old_fixture', 'name': 'old'})
        worker = Worker({'account':'wxid_old_fixture', 'state':'stopped', 'connected':False,
            'logged_in':False, 'login_pending':False, 'login_state':'closed', 'pid':None, 'error':''})
        self.hub.workers['default'] = worker
        result = self.hub.open_session({'request_id':'fixture-login-0000001'})
        self.assertEqual(result['id'], 'default')
        self.assertEqual(len(self.hub.entries()), 1)
        self.assertTrue(metadata.exists())
        self.assertEqual(worker.calls[-1][0], 'linux-session/open')

    def test_starting_session_remains_visible_before_pid_is_ready(self):
        self.hub.workers['default'] = Worker({'account':'', 'state':'stopped', 'connected':False,
            'logged_in':False, 'login_pending':False, 'login_state':'starting', 'pid':None, 'error':''})
        self.assertEqual(len(self.hub.logged_accounts()['accounts']), 1)


if __name__ == '__main__':
    unittest.main()
