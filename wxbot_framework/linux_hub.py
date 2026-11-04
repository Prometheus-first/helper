"""Linux 多账号管理：独立 worker、微信 HOME 和按账号隔离的业务工作区。"""
import json
from pathlib import Path
import re
import threading
import uuid

from .panel_common import PanelError, redact
from .robot_hub import RobotProxy, RobotWorker
from .web_panel import atomic_json, read_json
from .linux_sessions import private_directory


class LinuxRobotWorker(RobotWorker):
    def __init__(self, root, workspace, wechat_path, *, headless=False):
        super().__init__(root, workspace)
        self.wechat_path = Path(wechat_path)
        self.headless = headless

    def command(self):
        command = [*super().command(), '--linux-worker', '--wechat', str(self.wechat_path)]
        return command + (['--headless'] if self.headless else [])


class LinuxRobotHub:
    platform = 'linux'

    def __init__(self, root, *, wechat_path, worker_factory=None, headless=False):
        self.root = Path(root).resolve()
        self.wechat_path = Path(wechat_path).expanduser().absolute()
        self.directory = self.root / '.runtime/linux'
        private_directory(self.directory, self.root)
        private_directory(self.directory / 'sessions', self.directory)
        self.registry = self.directory / 'sessions.json'
        if not self.registry.exists():
            atomic_json(self.registry, {'robots': [{'id':'default','name':'微信会话 1'}]})
        self.workers = {}
        self.factory = worker_factory or (lambda root, workspace: LinuxRobotWorker(root, workspace, self.wechat_path, headless=headless))
        self.lock = threading.RLock()
        self.stop_event, self.stopped_event = threading.Event(), threading.Event()
        self.login_requests = {}

    def entries(self):
        rows = read_json(self.registry).get('robots')
        if not isinstance(rows, list) or not 1 <= len(rows) <= 32:
            raise PanelError('Linux 会话列表损坏')
        seen = set()
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get('id'), str) or
                not re.fullmatch(r'default|[a-f0-9]{12}', row['id']) or row['id'] in seen or
                not isinstance(row.get('name'), str) or not 1 <= len(row['name']) <= 32):
                raise PanelError('Linux 会话列表损坏')
            seen.add(row['id'])
        return rows

    def workspace(self, identifier):
        if identifier not in {row['id'] for row in self.entries()}:
            raise PanelError('未找到这个微信会话', 404)
        return private_directory(self.directory / 'sessions' / identifier, self.directory)

    def robot(self, identifier):
        self.workspace(identifier)
        return RobotProxy(self, identifier)

    def request(self, identifier, route, data=None):
        with self.lock:
            if self.stop_event.is_set():
                raise PanelError('管理服务正在退出', 409)
            workspace = self.workspace(identifier)
            worker = self.workers.get(identifier)
            if worker is None:
                worker = self.workers[identifier] = self.factory(self.root, workspace)
        return worker.request(route, data)

    def create_robot(self, data):
        with self.lock:
            rows = self.entries()
            if len(rows) >= 32:
                raise PanelError('最多保留 32 个 Linux 微信会话')
            identifier = uuid.uuid4().hex[:12]
            name = data.get('name') or '微信会话 ' + str(len(rows) + 1)
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 32 or any(ord(c)<32 for c in name):
                raise PanelError('请输入 1–32 字的会话名称')
            entry = {'id':identifier,'name':name.strip()}
            atomic_json(self.registry, {'robots': [*rows, entry]})
            self.workspace(identifier)
            return entry

    def _offline_state(self, entry):
        metadata = self.workspace(entry['id']) / 'identity.json'
        identity = read_json(metadata) if metadata.exists() else {}
        return dict(entry, platform='linux', account=identity.get('wxid', ''),
            account_name=identity.get('name', ''), state='stopped', connected=False,
            logged_in=False, login_pending=False, login_state='closed', pid=None, error='')

    def robot_list(self):
        with self.lock:
            entries, workers = self.entries(), dict(self.workers)
        result = []
        for entry in entries:
            if entry['id'] in workers:
                try:
                    snapshot = workers[entry['id']].request('state')
                    result.append(dict(entry, **snapshot))
                    continue
                except PanelError as error:
                    row=self._offline_state(entry)
                    row.update(state='error',error=redact(error))
                    result.append(row)
                    continue
            result.append(self._offline_state(entry))
        return {'robots':result}

    def logged_accounts(self):
        profiles = self.robot_list()['robots']
        result = []
        for row in profiles:
            # Historical profiles remain available for reuse, but are not live accounts.
            if not (row.get('logged_in') or row.get('login_state') == 'starting' or
                    (row.get('pid') and row.get('login_state') in ('waiting_login', 'error'))):
                continue
            duplicate = bool(row['account'] and row.get('logged_in') and
                sum(other.get('logged_in') and other['account']==row['account'] for other in profiles)>1)
            reason = '同一账号存在多个登录会话' if duplicate else ''
            result.append(dict(wxid=row.get('account', ''), name=row.get('account_name') or row['name'],
                pid=row.get('pid'), robot_id=row['id'], state=row['state'], connected=row.get('connected',False),
                logged_in=row.get('logged_in',False), login_state=row.get('login_state','closed'),
                login_pending=row.get('login_pending',False), error=row.get('error',''),
                selectable=bool(row.get('logged_in') and not duplicate), reason=reason))
        return {'accounts':result}

    def _selection(self, data):
        account, pid = data.get('wxid'), data.get('pid')
        if not isinstance(account,str) or not account or type(pid) is not int or pid<=0:
            raise PanelError('请选择已登录的微信账号')
        rows=self.logged_accounts()['accounts']
        matches=[r for r in rows if r['wxid']==account and r['logged_in']]
        if len(matches)!=1 or matches[0]['pid']!=pid or not matches[0]['selectable']:
            raise PanelError('账号已退出或进程已变化，请刷新后重选',409)
        return matches[0]

    def prepare_account(self, data):
        row=self._selection(data)
        return {'id':row['robot_id'],'name':row['name'],'account':row['wxid']}

    def start_selected(self, data):
        selected=data.get('accounts')
        if not isinstance(selected,list) or not 1<=len(selected)<=32 or any(not isinstance(r,dict) for r in selected):
            raise PanelError('请勾选 1–32 个账号')
        keys=[(r.get('wxid'),r.get('pid')) for r in selected]
        if any(not isinstance(a,str) or type(pid) is not int for a,pid in keys) or len(set(keys))!=len(keys):
            raise PanelError('账号选择无效或重复')
        results=[]
        for selection in selected:
            result=dict(wxid=selection['wxid'],pid=selection['pid'])
            try:
                row=self._selection(selection)
                result['robot_id']=row['robot_id']
                if row['state'] in ('starting','running'):
                    result['status']='already_running'
                elif row['state']=='stopping':
                    raise PanelError('正在停止，请稍后启动',409)
                else:
                    self.request(row['robot_id'],'start',{})
                    result['status']='started'
            except Exception as error:
                result.update(status='error',error=redact(error))
            results.append(result)
        return {'results':results,'started':sum(r['status']=='started' for r in results),
            'already_running':sum(r['status']=='already_running' for r in results),
            'failed':sum(r['status']=='error' for r in results)}

    def open_session(self, data):
        with self.lock:
            identifier=data.get('id')
            request_id=data.get('request_id')
            if request_id is not None and (not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}',request_id)):
                raise PanelError('登录请求编号无效')
            if request_id in self.login_requests:
                return dict(self.login_requests[request_id])
            if identifier is None:
                rows=self.robot_list()['robots']
                empty=next((r for r in rows if not r.get('account') and r.get('login_state')=='closed'),None)
                reusable=empty or next((r for r in rows if r.get('login_state')=='closed' and not r.get('pid')),None)
                identifier=reusable['id'] if reusable else self.create_robot({})['id']
            self.workspace(identifier)
            self.request(identifier,'linux-session/open',{})
            result={'id':identifier,'opened':True}
            if request_id:
                self.login_requests[request_id]=result
                while len(self.login_requests)>64:self.login_requests.pop(next(iter(self.login_requests)))
            return result

    def close_session(self, data):
        identifier=data.get('id')
        self.workspace(identifier)
        return self.request(identifier,'linux-session/close',{})

    def close(self):
        self.stop_event.set()
        with self.lock:workers=list(self.workers.values())
        threads=[threading.Thread(target=w.close) for w in workers]
        for t in threads:t.start()
        for t in threads:t.join()
        self.stopped_event.set()
