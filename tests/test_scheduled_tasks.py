"""定时任务的持久化、账号隔离和不补发行为。"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from wxbot_framework.panel_common import PanelError
from wxbot_framework.scheduled_tasks import ScheduledTasks, next_time
from wxbot_framework.scheduled_actions import BUILTIN, actions_from_manifest
from wxbot_framework.web_panel import atomic_json, read_json


class Store:
    account = 'wxid_test'
    def load(self): return {'wechat_account': self.account}


class Controller:
    def __init__(self, path):
        self.workspace, self.store, self.sent = Path(path), Store(), []
        self.connected = True
    def send_connection(self):
        if not self.connected: raise PanelError('未连接', 409)
        return {'session_id': 'session'}
    def send_text(self, data):
        self.sent.append(data)
        return {'status': 'sent'}


class ScheduledTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.controller = Controller(self.temp.name)
        self.scheduler = ScheduledTasks(self.controller, atomic_json, read_json)
        self.addCleanup(self.scheduler.close)
        self.start_patch = patch.object(self.scheduler, 'start')
        self.start_patch.start()
        self.addCleanup(self.start_patch.stop)

    def create(self, **values):
        data = dict(name='提醒', chat_id='filehelper', content='你好', kind='interval', interval_seconds=300)
        data.update(values)
        with patch('wxbot_framework.scheduled_tasks.time.time', return_value=1000):
            return self.scheduler.save(data)['task']

    def test_interval_persist_and_send_once(self):
        task = self.create()
        self.assertEqual(task['next_run'], 1300)
        self.scheduler.tick(1299)
        self.scheduler.tick(1300)
        self.scheduler.tick(1300)
        self.assertEqual(len(self.controller.sent), 1)
        self.assertEqual(self.controller.sent[0]['content'], '你好')
        row = self.scheduler.list()['tasks'][0]
        self.assertEqual((row['last_status'], row['next_run']), ('已发送', 1600))
        self.assertEqual(read_json(self.scheduler.path)['tasks'][0]['next_run'], 1600)

    def test_late_or_disconnected_does_not_catch_up(self):
        self.create()
        self.scheduler.tick(1400)
        self.assertEqual(self.scheduler.list()['tasks'][0]['last_status'], '已跳过（错过执行时间）')
        self.controller.connected = False
        self.scheduler.tick(1700)
        self.assertIn('未发送', self.scheduler.list()['tasks'][0]['last_status'])
        self.controller.connected = True
        self.scheduler.tick(1701)
        self.assertEqual(self.controller.sent, [])

    def test_account_isolation_disable_and_delete(self):
        task = self.create()
        self.controller.store.account = 'wxid_other'
        self.scheduler.tick(1300)
        self.assertEqual(self.controller.sent, [])
        with self.assertRaises(PanelError): self.scheduler.delete({'id': task['id']})
        self.controller.store.account = task['account']
        self.scheduler.save({**task, 'enabled': False})
        self.scheduler.tick(5000)
        self.assertEqual(self.controller.sent, [])
        self.scheduler.delete({'id': task['id']})
        self.assertEqual(self.scheduler.list()['tasks'], [])

    def test_daily_time_and_validation(self):
        at = datetime(2026, 10, 30, 8, 30).timestamp()
        self.assertEqual(datetime.fromtimestamp(next_time({'kind':'daily', 'daily_time':'09:00'}, at)).hour, 9)
        self.assertEqual(datetime.fromtimestamp(next_time({'kind':'daily', 'daily_time':'08:30'}, at)).day, 31)
        with self.assertRaises(PanelError): self.create(interval_seconds=1)
        with self.assertRaises(PanelError): self.create(kind='daily', daily_time='25:00')
        with self.assertRaises(PanelError): self.create(chat_id='bad target')

    def test_stale_claim_never_sends(self):
        task = self.create()
        task['claim'] = 'stale'
        self.assertEqual(self.scheduler._deliver(task), '已取消')
        self.assertFalse(self.controller.sent)

    def test_restart_skips_missed_task_even_if_recent(self):
        self.create()
        scheduler = ScheduledTasks(self.controller, atomic_json, read_json)
        with patch('wxbot_framework.scheduled_tasks.time.time', return_value=1301), patch.object(scheduler, '_loop'):
            scheduler.start()
            scheduler.thread.join()
        row = scheduler.list()['tasks'][0]
        self.assertEqual(row['last_status'], '已跳过（停机期间错过）')
        self.assertEqual(row['next_run'], 1601)
        scheduler.close()

    def test_plugin_actions_validate_and_dispatch_without_breaking_old_tasks(self):
        action = actions_from_manifest('example', {'scheduled_actions':[{'id':'work', 'label':'做事',
            'method':'run_task', 'fields':[{'name':'count', 'label':'次数', 'type':'integer', 'required':True}]}]})[0]
        self.controller.scheduled_action_types = lambda: {'actions':[BUILTIN, action]}
        calls = []
        self.controller.execute_scheduled_plugin = lambda aid, params, session, account, valid: calls.append((aid, params, session, account, valid()))
        with self.assertRaises(PanelError): self.create(action_id=action['id'], params={'count':'2'})
        task = self.create(action_id=action['id'], params={'count':2})
        self.scheduler.tick(1300)
        self.assertEqual(calls, [(action['id'], {'count':2}, 'session', 'wxid_test', True)])
        self.assertEqual(self.scheduler.list()['tasks'][0]['last_status'], '执行完成')
        self.controller.scheduled_action_types = lambda: {'actions':[BUILTIN]}
        self.scheduler.toggle({'id':task['id'], 'enabled':False})
        self.assertFalse(self.scheduler.list()['tasks'][0]['enabled'])

    def test_running_task_never_overlaps_itself(self):
        task = self.create()
        self.scheduler.inflight.add(task['id'])
        self.scheduler.tick(1300)
        self.assertFalse(self.controller.sent)
        self.assertEqual(self.scheduler.list()['tasks'][0]['next_run'], 1600)

    def test_restart_preserves_uncertain_inflight_result(self):
        self.create()
        rows = self.scheduler.list()['tasks']
        rows[0].update(claim='unfinished', last_time=1300, last_status='执行中', next_run=1600)
        self.scheduler._write(rows)
        scheduler = ScheduledTasks(self.controller, atomic_json, read_json)
        with patch('wxbot_framework.scheduled_tasks.time.time', return_value=1700), patch.object(scheduler, '_loop'):
            scheduler.start()
            scheduler.thread.join()
        row = scheduler.list()['tasks'][0]
        self.assertIn('待确认', row['last_status'])
        self.assertEqual(row['last_time'], 1300)
        self.assertEqual(row['next_run'], 2000)
        scheduler.close()


if __name__ == '__main__': unittest.main()
