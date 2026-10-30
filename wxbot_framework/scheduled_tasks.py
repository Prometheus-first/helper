"""按机器人工作区保存并执行定时文本任务。"""
from copy import deepcopy
from datetime import datetime, timedelta
import logging
import re
import secrets
import threading
import time

from .config import account_is_unbound
from .panel_common import PanelError
from .scheduled_actions import BUILTIN, validate_params


def next_time(task, now):
    if task['kind'] == 'interval':
        return now + task['interval_seconds']
    hour, minute = map(int, task['daily_time'].split(':'))
    current = datetime.fromtimestamp(now)
    candidate = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate.timestamp() <= now:
        candidate += timedelta(days=1)
    return candidate.timestamp()


class ScheduledTasks:
    def __init__(self, controller, write_json, read_json):
        self.controller = controller
        self.path = controller.workspace / '.runtime' / 'scheduled_tasks.json'
        self.write_json, self.read_json = write_json, read_json
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.slots = threading.BoundedSemaphore(8)
        self.inflight = set()

    def _read(self):
        if not self.path.exists():
            return []
        rows = self.read_json(self.path).get('tasks')
        if not isinstance(rows, list):
            raise PanelError('定时任务数据损坏，请检查本地文件', 500)
        return rows

    def _write(self, rows):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.write_json(self.path, {'tasks': rows})

    def start(self):
        if self.thread is not None or self.stop_event.is_set():
            return
        with self.lock:
            if self.thread is None and not self.stop_event.is_set():
                rows = self._read()
                now = time.time()
                changed = False
                for row in rows:
                    interrupted = bool(row.pop('claim', None))
                    if interrupted:
                        row['last_status'] = '结果待确认，请核对微信'
                        changed = True
                    if row.get('enabled') and row.get('next_run', 0) <= now:
                        row['next_run'] = next_time(row, now)
                        if not interrupted:
                            row.update(last_time=now, last_status='已跳过（停机期间错过）')
                        changed = True
                if changed:
                    self._write(rows)
                self.thread = threading.Thread(target=self._loop, name='ScheduledTasks', daemon=True)
                self.thread.start()

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(3)

    def list(self):
        with self.lock:
            return {'tasks': deepcopy(self._read())}

    def save(self, data):
        name = data.get('name')
        if not isinstance(name, str) or not name.strip() or len(name) > 100 or '\n' in name:
            raise PanelError('请输入 1–100 字的任务名称')
        action_id = data.get('action_id', 'builtin.send_text')
        available = self.controller.scheduled_action_types() if hasattr(self.controller, 'scheduled_action_types') else {'actions': [BUILTIN]}
        action = next((item for item in available['actions'] if item['id'] == action_id), None)
        if action is None:
            raise PanelError('定时动作不存在，请刷新任务类型')
        params = data.get('params')
        if params is None and action_id == 'builtin.send_text':
            params = {'chat_id': data.get('chat_id'), 'content': data.get('content')}
        try:
            params = validate_params(action, params)
        except (ValueError, UnicodeEncodeError) as error:
            raise PanelError(str(error)) from None
        kind = data.get('kind')
        interval = data.get('interval_seconds')
        daily = data.get('daily_time')
        if kind == 'interval':
            if type(interval) is not int or not 60 <= interval <= 31536000:
                raise PanelError('间隔应为 1 分钟至 365 天')
            daily = ''
        elif kind == 'daily':
            if not isinstance(daily, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', daily):
                raise PanelError('请选择每天的执行时间')
            interval = 0
        else:
            raise PanelError('请选择执行方式')
        account = self.controller.store.load().get('wechat_account')
        if account_is_unbound(account):
            raise PanelError('请先选择已登录的微信账号', 409)
        enabled = data.get('enabled', True)
        if type(enabled) is not bool:
            raise PanelError('任务状态无效')
        with self.lock:
            rows = self._read()
            identifier = data.get('id')
            existing = next ( (row for row in rows if row['id'] == identifier), None) if identifier else None
            if identifier and (existing is None or existing['account'] != account):
                raise PanelError('任务不存在或不属于当前账号', 404)
            if not existing and len(rows) >= 200:
                raise PanelError('最多创建 200 个定时任务')
            task = {'id': identifier or secrets.token_hex(12), 'account': account, 'name': name.strip(),
                    'action_id': action_id, 'params': params, 'kind': kind,
                    'interval_seconds': interval, 'daily_time': daily, 'enabled': enabled,
                    'last_status': existing.get('last_status', '') if existing else '',
                    'last_time': existing.get('last_time', 0) if existing else 0}
            task['next_run'] = next_time(task, time.time()) if enabled else 0
            if existing:
                rows[rows.index(existing)] = task
            else:
                rows.append(task)
            self._write(rows)
        self.start()
        return {'ok': True, 'task': task}

    def toggle(self, data):
        enabled = data.get('enabled')
        if type(enabled) is not bool:
            raise PanelError('任务状态无效')
        account = self.controller.store.load().get('wechat_account')
        with self.lock:
            rows = self._read()
            row = next((r for r in rows if r.get('id') == data.get('id') and r.get('account') == account), None)
            if row is None:
                raise PanelError('任务不存在或不属于当前账号', 404)
            row['enabled'] = enabled
            row['next_run'] = next_time(row, time.time()) if enabled else 0
            row.pop('claim', None)
            self._write(rows)
        self.start()
        return {'ok': True}

    def delete(self, data):
        account = self.controller.store.load().get('wechat_account')
        with self.lock:
            rows = self._read()
            remaining = [r for r in rows if not (r['id'] == data.get('id') and r['account'] == account)]
            if len(remaining) == len(rows):
                raise PanelError('任务不存在或不属于当前账号', 404)
            self._write(remaining)
        return {'ok': True}

    def tick(self, now=None):
        live_clock = now is None
        now = time.time() if live_clock else now
        with self.lock:
            rows = self._read()
            due = []
            changed = False
            for row in rows:
                if row.get('enabled') and row.get('next_run', 0) <= now:
                    if row.get('claim') or row['id'] in self.inflight:
                        row['next_run'] = next_time(row, now)
                        changed = True
                        continue
                    task = deepcopy(row)
                    task['claim'] = secrets.token_hex(12)
                    due.append(task)
                    # 发送之前持久化推进时间；重启绝不重试可能已经发出的消息。
                    row.update(next_run=next_time(row, now), last_time=now, last_status='执行中', claim=task['claim'])
                    changed = True
            if changed:
                self._write(rows)
                self.inflight.update(task['id'] for task in due)
        for task in due:
            if live_clock:
                if self.slots.acquire(blocking=False):
                    threading.Thread(target=self._finish_async, args=(task,), name='ScheduledAction', daemon=True).start()
                else:
                    self._finish(task, '已跳过（任务繁忙）')
            else:
                status = '已跳过（错过执行时间）' if now - task['next_run'] > 10 else self._deliver(task)
                self._finish(task, status)

    def _finish_async(self, task):
        try:
            status = '已跳过（错过执行时间）' if time.time() - task['next_run'] > 10 else self._deliver(task)
            self._finish(task, status)
        finally:
            self.slots.release()

    def _finish(self, task, status):
        with self.lock:
            try:
                rows = self._read()
                for row in rows:
                    if row['id'] == task['id'] and row.get('claim') == task['claim']:
                        row['last_status'] = status
                        row.pop('claim', None)
                self._write(rows)
            finally:
                self.inflight.discard(task['id'])

    def _deliver(self, task):
        try:
            with self.lock:
                current = next((row for row in self._read() if row['id'] == task['id']), None)
                if not current or not current.get('enabled') or current.get('claim') != task['claim']:
                    return '已取消'
                if self.stop_event.is_set() or self.controller.store.load().get('wechat_account') != task['account']:
                    return '已跳过（账号已变化）'
            connection = self.controller.send_connection()
            with self.lock:
                current = next((row for row in self._read() if row['id'] == task['id']), None)
                if self.stop_event.is_set() or not current or not current.get('enabled') or current.get('claim') != task['claim']:
                    return '已取消'
                action_id = task.get('action_id', 'builtin.send_text')
                params = task.get('params', {'chat_id': task.get('chat_id'), 'content': task.get('content')})
                if action_id == 'builtin.send_text':
                    request_id = secrets.token_hex(16)
                    result = self.controller.send_text({'request_id': request_id, 'session_id': connection['session_id'], **params})
                else:
                    # 仅预约快照；插件回调在锁外运行，避免长任务阻塞配置及其他任务。
                    result = None
            if action_id != 'builtin.send_text':
                self.controller.execute_scheduled_plugin(action_id, params, connection['session_id'], task['account'],
                                                         lambda: self._claim_current(task))
                return '执行完成'
            deadline = time.monotonic() + 120
            while result['status'] == 'sending' and time.monotonic() < deadline and not self.stop_event.wait(0.5):
                result = self.controller.send_status(request_id)
            return {'sent': '已发送', 'failed': '发送失败，请核对微信', 'sending': '结果待确认，请核对微信'}.get(result['status'], '结果待确认，请核对微信')
        except (Exception, SystemExit) as error:
            logging.getLogger('ScheduledTasks').warning('定时任务未执行：%s', type(error).__name__)
            return ('未发送（未连接、发送忙或会话受限）' if task.get('action_id', 'builtin.send_text') == 'builtin.send_text'
                    else '执行失败（请检查连接、插件、白名单和日志）')

    def _claim_current(self, task):
        with self.lock:
            row = next((item for item in self._read() if item['id'] == task['id']), None)
            return bool(not self.stop_event.is_set() and row and row.get('enabled') and row.get('claim') == task['claim'])

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                logging.getLogger('ScheduledTasks').exception('定时任务处理失败')
            self.stop_event.wait(1)
