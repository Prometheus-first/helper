"""多机器人管理：共享代码，每个机器人一个子进程和独立本地工作区。"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import uuid

from .config import account_is_unbound, initialize_configs
from .startup import scan_logged_accounts
from .web_panel import PanelController, PanelError, atomic_json, read_json, redact


class RobotWorker:
    def __init__(self, root, workspace):
        self.root, self.workspace = Path(root), Path(workspace)
        self.process = None
        self.endpoint = self.token = None
        self.lock = threading.RLock()
        self.dead = False

    def launch(self):
        with self.lock:
            if self.dead:
                raise PanelError('管理服务正在退出', 409)
            if self.process is not None and self.process.poll() is None:
                if self.endpoint is None or self.token is None:
                    raise PanelError('机器人管理进程仍在退出，请稍后刷新', 503)
                return
            if self.process is not None:
                self.process.stdin.close()
                self.process.stdout.close()
            self.endpoint = self.token = None
            output = self.workspace / 'logs/worker.log'
            output.parent.mkdir(parents=True, exist_ok=True)
            environment = dict(os.environ, PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')
            with output.open('ab') as error_log:
                process = subprocess.Popen([sys.executable, '-B', '-X', 'utf8', str(self.root / 'web.py'),
                    '--worker', '--workspace', str(self.workspace)], cwd=self.root,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=error_log,
                    env=environment, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), close_fds=True)
            self.process = process
            ready = queue.Queue(maxsize=1)
            def read_ready():
                try:
                    ready.put(process.stdout.readline(8192))
                except OSError:
                    ready.put(b'')
            threading.Thread(target=read_ready, daemon=True).start()
            try:
                metadata = json.loads(ready.get(timeout=20))
                port, token = metadata['port'], metadata['token']
                if type(port) is not int or not 1 <= port <= 65535 or not isinstance(token, str) or len(token) < 32:
                    raise ValueError()
            except (queue.Empty, ValueError, KeyError, TypeError):
                process.stdin.close()
                raise PanelError('机器人管理进程未就绪，请查看该机器人的 logs/worker.log', 503) from None
            self.endpoint, self.token = f'http://127.0.0.1:{port}', token

    def request(self, route, body=None):
        self.launch()
        with self.lock:
            endpoint, token = self.endpoint, self.token
        request = urllib.request.Request(endpoint + '/api/' + route,
            data=None if body is None else json.dumps(body, ensure_ascii=False).encode('utf-8'),
            headers={'X-Helper-Token': token, 'Content-Type': 'application/json'})
        try:
            # 不使用系统代理转发本机管理请求。
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=20) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise PanelError('机器人管理响应超过大小限制', 503)
                return json.loads(raw)
        except urllib.error.HTTPError as error:
            try:
                message = json.loads(error.read(16384)).get('error', '机器人操作失败')
            except (ValueError, AttributeError):
                message = '机器人操作失败'
            raise PanelError(message, error.code) from None
        except (OSError, ValueError):
            raise PanelError('机器人管理进程连接已中断，请稍后刷新；原生连接由退出守护清理', 503) from None

    def close(self):
        with self.lock:
            self.dead = True
            process = self.process
            if process is not None and process.stdin and not process.stdin.closed:
                process.stdin.close()  # EOF 让子进程正常停止；不强杀正在清理的微信连接。
        if process is not None:
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                logging.getLogger('RobotHub').warning('机器人进程仍在正常清理，请稍候')
            if process.poll() is not None:
                process.stdout.close()


class RobotProxy:
    def __init__(self, hub, identifier):
        self.hub, self.identifier = hub, identifier
        self.logs = self

    def request(self, route, body=None):
        return self.hub.request(self.identifier, route, body)

    def snapshot(self): return self.request('state')
    def configuration(self): return self.request('config')
    def accounts(self): return self.request('accounts')['accounts']
    def chats(self): return self.request('chats')['chats']
    def contacts(self): return self.request('contacts')
    def group_members(self, room):
        from urllib.parse import urlencode
        return self.request('group-members?' + urlencode({'room': room}))
    def send_text(self, data): return self.request('send-text', data)
    def send_connection(self): return self.request('send-connection')
    def send_status(self, identifier):
        from urllib.parse import urlencode
        return self.request('send-status?' + urlencode({'id': identifier}))
    def moments_connection(self): return self.request('moments-connection')
    def publish_moments(self, data): return self.request('moments-publish', data)
    def read_moments(self, data): return self.request('moments-read', data)
    def interact_moments(self, data): return self.request('moments-interact', data)
    def moments_interaction_status(self, identifier):
        from urllib.parse import urlencode
        return self.request('moments-interaction-status?' + urlencode({'id': identifier}))
    def moments_status(self, identifier):
        from urllib.parse import urlencode
        return self.request('moments-status?' + urlencode({'id': identifier}))
    def tail(self, after=0): return self.request(f'logs?after={after}')
    def message_tail(self, after=0): return self.request(f'messages?after={after}')
    def start(self): return self.request('start', {})
    def stop(self): return self.request('stop', {})
    def restart(self): return self.request('restart', {})
    def bind_account(self, data): return self.request('account', data)
    def save_configuration(self, data): return self.request('config', data)
    def plugin_action(self, data): return self.request('plugin', data)
    def plugin_editor(self, name):
        from urllib.parse import urlencode
        return self.request('plugin-source?' + urlencode({'name': name}))
    def save_plugin_source(self, data, *, reset=False):
        return self.request('plugin-reset' if reset else 'plugin-source', data)
    def plugin_business(self, name):
        from urllib.parse import urlencode
        return self.request('plugin-business?' + urlencode({'name': name}))
    def save_plugin_business(self, data):
        return self.request('plugin-business', data)


class RobotHub:
    def __init__(self, root, *, worker_factory=RobotWorker, scan=scan_logged_accounts):
        self.root = Path(root).resolve()
        initialize_configs(self.root)
        self.directory = self.root / '.runtime/robots'
        self.registry = self.directory / 'index.json'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.workers, self.factory = {}, worker_factory
        self.scan = scan
        self.lock = threading.RLock()
        self.stop_event, self.stopped_event = threading.Event(), threading.Event()
        if not self.registry.exists():
            atomic_json(self.registry, {'robots': []})

    def entries(self):
        entries = read_json(self.registry).get('robots')
        if not isinstance(entries, list) or len(entries) > 32:
            raise PanelError('机器人列表配置损坏')
        seen = {'default'}
        for row in entries:
            if (not isinstance(row, dict) or not isinstance(row.get('id'), str) or
                    not re.fullmatch(r'[a-f0-9]{12}', row['id']) or row['id'] in seen or
                    not isinstance(row.get('name'), str) or not 1 <= len(row['name']) <= 32):
                raise PanelError('机器人列表配置损坏')
            seen.add(row['id'])
        return [{'id': 'default', 'name': '默认机器人'}, *entries]

    def workspace(self, identifier):
        if identifier not in {row['id'] for row in self.entries()}:
            raise PanelError('未找到这个机器人', 404)
        path = self.root if identifier == 'default' else self.directory / identifier
        if identifier != 'default' and (path.is_symlink() or not path.resolve().is_relative_to(self.directory.resolve())):
            raise PanelError('机器人工作目录不在允许范围', 400)
        return path

    def robot(self, identifier):
        self.workspace(identifier)
        return RobotProxy(self, identifier)

    def create_robot(self, data, *, account=None):
        name = data.get('name')
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 32 or any(ord(ch) < 32 for ch in name):
            raise PanelError('机器人名称应为 1–32 字符的单行文本')
        with self.lock:
            entries = self.entries()[1:]
            if len(entries) >= 32:
                raise PanelError('最多创建 32 个附加机器人')
            if name.strip() in {row['name'] for row in self.entries()}:
                raise PanelError('这个机器人名称已存在')
            identifier = uuid.uuid4().hex[:12]
            workspace = self.directory / identifier
            # 只使用公开空模板，不复制默认机器人的密钥、群聊或历史。
            options = {'scan': lambda root: [account]} if account is not None else {}
            controller = PanelController(self.root, workspace=workspace, **options)
            try:
                if account is not None:
                    controller.bind_account(account)
            finally:
                controller.close()
            entries.append({'id': identifier, 'name': name.strip()})
            atomic_json(self.registry, {'robots': entries})
            return {'id': identifier, 'name': name.strip()}

    def _account_profiles(self, account):
        return [entry for entry in self.entries()
                if read_json(self.workspace(entry['id']) / 'bot_config.json').get('wechat_account') == account]

    def _selected_account(self, data):
        wxid, pid = data.get('wxid'), data.get('pid')
        if not isinstance(wxid, str) or not wxid or type(pid) is not int or pid <= 0:
            raise PanelError('请选择有效的微信账号')
        rows = self.scan(self.root)
        matches = [row for row in rows if row['wxid'] == wxid]
        if not any(row['pid'] == pid for row in matches):
            raise PanelError('账号已退出或进程已变化，请刷新账号', 409)
        if len(matches) != 1:
            raise PanelError('同一账号有多个登录进程，请关闭重复会话', 409)
        return matches[0]

    def logged_accounts(self):
        # 发现账号不创建配置、不绑定账号，也不启动机器人。
        rows = self.scan(self.root)
        profiles = self.robot_list()['robots']
        result = []
        for row in rows:
            matches = [profile for profile in profiles if profile['account'] == row['wxid']]
            duplicate = sum(other['wxid'] == row['wxid'] for other in rows) > 1
            reason = '同一账号有多个登录进程' if duplicate else '账号存在重复配置' if len(matches) > 1 else ''
            profile = matches[0] if len(matches) == 1 else {}
            result.append({**row, 'robot_id': profile.get('id'), 'state': profile.get('state', 'stopped'),
                           'connected': profile.get('connected', False), 'selectable': not reason, 'reason': reason})
        return {'accounts': result}

    def prepare_account(self, data):
        with self.lock:
            if self.stop_event.is_set():
                raise PanelError('管理服务正在退出', 409)
            account = self._selected_account(data)
            matches = self._account_profiles(account['wxid'])
            if len(matches) > 1:
                raise PanelError('账号存在重复配置，请先清理重复配置', 409)
            if matches:
                return dict(matches[0], account=account['wxid'])
            default_config = read_json(self.root / 'bot_config.json')
            if account_is_unbound(default_config.get('wechat_account')):
                # 仅首次使用空闲默认配置；后续不同账号绝不互换配置和历史。
                worker = self.workers.get('default')
                if worker is not None and worker.request('state')['state'] in ('starting', 'running', 'stopping'):
                    raise PanelError('默认配置仍在运行，请先停止后再选择账号', 409)
                controller = PanelController(self.root, scan=lambda root: [account])
                try:
                    controller.bind_account(account)
                finally:
                    controller.close()
                return {'id': 'default', 'name': '默认机器人', 'account': account['wxid']}
            name = ''.join(ch for ch in (account.get('name') or account['wxid']) if ord(ch) >= 32).strip()[:32] or '微信账号'
            taken = {entry['name'] for entry in self.entries()}
            candidate, index = name, 2
            while candidate in taken:
                suffix = f' ({index})'
                candidate, index = name[:32-len(suffix)] + suffix, index + 1
            entry = self.create_robot({'name': candidate}, account=account)
            return dict(entry, account=account['wxid'])

    def start_selected(self, data):
        selected = data.get('accounts')
        if not isinstance(selected, list) or not 1 <= len(selected) <= 32:
            raise PanelError('请勾选 1–32 个微信账号')
        seen = set()
        for row in selected:
            if (not isinstance(row, dict) or not isinstance(row.get('wxid'), str) or not row['wxid'] or
                    type(row.get('pid')) is not int or row['pid'] <= 0):
                raise PanelError('账号选择格式不正确')
            if row['wxid'] in seen:
                raise PanelError('不能重复选择同一个微信账号')
            seen.add(row['wxid'])
        results = []
        for selection in selected:
            result = {'wxid': selection['wxid'], 'pid': selection['pid']}
            try:
                with self.lock:
                    entry = self.prepare_account(selection)
                    result['robot_id'] = entry['id']
                    state = self.request(entry['id'], 'state')['state']
                    if state in ('starting', 'running'):
                        result['status'] = 'already_running'
                    elif state == 'stopping':
                        raise PanelError('正在清理连接，请停止完成后再启动', 409)
                    else:
                        self.request(entry['id'], 'start', {})
                        result['status'] = 'started'
            except Exception as error:
                result.update(status='error', error=redact(error))
            results.append(result)
        return {'results': results, 'started': sum(row['status'] == 'started' for row in results),
                'already_running': sum(row['status'] == 'already_running' for row in results),
                'failed': sum(row['status'] == 'error' for row in results)}

    def _unique_account(self, identifier, account):
        if account_is_unbound(account):
            return
        for entry in self.entries():
            if entry['id'] == identifier:
                continue
            other = read_json(self.workspace(entry['id']) / 'bot_config.json').get('wechat_account')
            if other == account:
                raise PanelError(f'这个微信账号已绑定“{entry["name"]}”，请选择另一个账号', 409)

    def request(self, identifier, route, data=None):
        # 账号检查与启动/绑定顺序串行，防止两个并发请求抢到同一账号。
        with self.lock:
            if self.stop_event.is_set():
                raise PanelError('管理服务正在退出', 409)
            workspace = self.workspace(identifier)
            if route == 'account' and data is not None:
                self._unique_account(identifier, data.get('wxid'))
            if route in ('start', 'restart') and data is not None:
                account = read_json(workspace / 'bot_config.json').get('wechat_account')
                if account_is_unbound(account):
                    raise PanelError('请先刷新账号，并为这个机器人选择一个微信账号', 409)
                self._unique_account(identifier, account)
            worker = self.workers.get(identifier)
            if worker is None:
                worker = self.workers[identifier] = self.factory(self.root, workspace)
            if route in ('account', 'start', 'restart'):
                return worker.request(route, data)
        return worker.request(route, data)

    def robot_list(self):
        result = []
        with self.lock:
            entries = self.entries()
            workers = dict(self.workers)
        for entry in entries:
            account = read_json(self.workspace(entry['id']) / 'bot_config.json').get('wechat_account') or ''
            state, connected = 'stopped', False
            worker = workers.get(entry['id'])
            if worker is not None:
                try:
                    snapshot = worker.request('state')
                    state, connected = snapshot['state'], snapshot['connected']
                except PanelError:
                    state = 'error'
            result.append(dict(entry, account=account, state=state, connected=connected))
        return {'robots': result}

    def close(self):
        self.stop_event.set()
        with self.lock:
            workers = list(self.workers.values())
        threads = [threading.Thread(target=worker.close) for worker in workers]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.stopped_event.set()
