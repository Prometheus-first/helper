"""仅监听本机的管理台；配置、密钥和原生生命周期仍由当前项目管理。"""
from __future__ import annotations

from collections import deque
from concurrent.futures import TimeoutError as ControlTimeout
from contextlib import contextmanager
from copy import deepcopy
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
import secrets
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

from .config import ConfigStore, EXAMPLE_ACCOUNT, account_is_unbound, initialize_configs, validate_name
from .locking import InstanceLock
from .message_monitor import MessageBuffer
from .runtime import BotApplication
from .startup import scan_logged_accounts
from .text import validate_target, validate_mentions

SECRET = re.compile(r'(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})')


def redact(text):
    return SECRET.sub('[密钥已隐藏]', str(text))[:8000]


def read_json(path):
    data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError('配置必须为 JSON 对象')
    return data


def atomic_json(path, data):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix='.web-config-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as output:
            output.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class PanelError(RuntimeError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class LogBuffer(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = deque(maxlen=2000)
        self.sequence = 0

    def emit(self, record):
        self.acquire()
        try:
            self.sequence += 1
            self.rows.append({'id': self.sequence, 'time': time.strftime('%H:%M:%S', time.localtime(record.created)),
                              'level': record.levelname, 'logger': record.name,
                              'message': redact(self.format(record))})
        finally:
            self.release()

    def tail(self, after=0):
        self.acquire()
        try:
            return {'rows': [row for row in self.rows if row['id'] > after][-300:], 'cursor': self.sequence}
        finally:
            self.release()


class PanelController:
    def __init__(self, root, *, workspace=None, app_factory=BotApplication, scan=scan_logged_accounts, lock_factory=InstanceLock):
        self.root = Path(root).resolve()
        self.workspace = Path(workspace or root).resolve()
        if self.workspace == self.root:
            initialize_configs(self.root)
        else:
            self.workspace.mkdir(parents=True, exist_ok=True)
            for stem in ('bot_config', 'members_config', 'llm_config', 'tts_config'):
                target = self.workspace / (stem + '.json')
                if not target.exists():
                    with target.open('x', encoding='utf-8') as output:
                        output.write((self.root / (stem + '.example.json')).read_text(encoding='utf-8-sig'))
        self.store = ConfigStore(self.workspace / 'bot_config.json')
        self.factory, self.scan, self.lock_factory = app_factory, scan, lock_factory
        self.lock = threading.RLock()
        self.actions = threading.Lock()
        self.app = self.worker = None
        self.state, self.error, self.started = 'stopped', '', None
        self.stop_event, self.stopped_event = threading.Event(), threading.Event()
        self.logs = LogBuffer()
        self.messages = MessageBuffer(sanitize=redact)
        self.send_jobs = {}
        logging.getLogger().addHandler(self.logs)

    def _paths(self, config):
        return {name: (self.workspace / config.get(field, default)).resolve()
                for name, field, default in [('llm', 'llm_config_file', 'llm_config.json'),
                    ('tts', 'tts_config_file', 'tts_config.json'),
                    ('members', 'members_config_file', 'members_config.json')]
                if config.get(field, default) is not None}

    def _idle(self):
        if self.worker is not None and self.worker.is_alive():
            raise PanelError('请先停止机器人，等清理完成后再保存配置', 409)
        # 另一终端启动的 main.py 也不能被本面板修改正在使用的配置。
        try:
            with self.lock_factory(self.workspace / '.runtime/bot.lock'):
                pass
        except RuntimeError:
            raise PanelError('此项目已在其他窗口运行，请先在原窗口输入 quit', 409) from None

    @contextmanager
    def _editing(self):
        if self.worker is not None and self.worker.is_alive():
            raise PanelError('请先停止机器人，等清理完成后再保存配置', 409)
        lease = self.lock_factory(self.workspace / '.runtime/bot.lock')
        try:
            lease.acquire()
        except RuntimeError:
            raise PanelError('此项目已在其他窗口运行，请先在原窗口输入 quit', 409) from None
        try:
            yield
        finally:
            lease.close()

    def accounts(self):
        return self.scan(self.root)

    def bind_account(self, data):
        with self.actions, self.lock, self._editing():
            wxid, pid = data.get('wxid'), data.get('pid')
            rows = self.accounts()
            if type(pid) is not int or not any(row['wxid'] == wxid and row['pid'] == pid for row in rows):
                raise PanelError('账号已退出或列表已变化，请刷新后重新选择')
            if sum(row['wxid'] == wxid for row in rows) != 1:
                raise PanelError('该账号对应多个微信进程，请先关闭重复会话')
            config = self.store.load()
            changed_account = config.get('wechat_account') != wxid
            config.update(wechat_account=wxid, wechat_pid=None)
            self._save_many({self.store.path: config})
            if changed_account:
                self.messages.reset()

    def start(self):
        with self.actions, self.lock:
            self._idle()
            lease = self.lock_factory(self.workspace / '.runtime/bot.lock')
            lease.acquire()
            try:
                config = self.store.load()
                if account_is_unbound(config.get('wechat_account')):
                    rows = self.accounts()
                    if len(rows) != 1:
                        raise PanelError('请先登录微信，在页面选择并绑定机器人账号', 409)
                    confirmed = self.accounts()
                    if not any(row['wxid'] == rows[0]['wxid'] and row['pid'] == rows[0]['pid'] for row in confirmed):
                        raise PanelError('账号状态已变化，请刷新后选择账号', 409)
                    config = self.store.bind_account(rows[0]['wxid'])
                app = self.factory(self.root, self.store)
                app.config['console'] = False
                def observe(message):
                    name = message.data.get('sender_wechat_name', '')
                    members = getattr(app, 'members', None)
                    if members is not None and message.is_group and message.sender_id and not message.is_self:
                        try:
                            name = members.display_name(message.sender_id, message.chat_id) or name
                        except Exception:
                            pass
                    self.messages.append(message, account=app.config.get('wechat_account') or '', sender_name=name)
                app.message_observer = observe
                self.app, self.state, self.error, self.started = app, 'starting', '', time.time()

                def run():
                    try:
                        app.run()
                    except Exception as error:
                        with self.lock:
                            self.error = redact(error)
                        logging.getLogger('WebPanel').exception('机器人运行失败')
                    finally:
                        try:
                            # run() 自带 finally 清理；构造后未进入 run 的异常也不能留下连接。
                            if not app.stopped_event.is_set():
                                app.stop()
                        except Exception as error:
                            with self.lock:
                                self.error = redact(error)
                        finally:
                            lease.close()
                            with self.lock:
                                self.state = 'error' if self.error else 'stopped'
                                self.app = None

                self.worker = threading.Thread(target=run, name='ManagedBot', daemon=False)
                self.worker.start()
                self.state = 'running'
            except Exception:
                lease.close()
                raise

    def stop(self):
        with self.lock:
            if self.app is not None:
                self.state = 'stopping'
                self.app.stop_event.set()

    def restart(self):
        with self.lock:
            worker = self.worker
            if worker is None or not worker.is_alive():
                raise PanelError('机器人尚未运行，请直接启动', 409)
        self.stop()
        worker.join(15)
        if worker.is_alive():
            raise PanelError('机器人仍在清理，请等停止后再启动', 409)
        with self.lock:
            if self.error:
                raise PanelError('上次退出有异常，请核对日志后再启动', 409)
            if self.stop_event.is_set():
                raise PanelError('管理服务正在退出', 409)
        self.start()

    def close(self):
        self.stop_event.set()
        self.stop()
        if self.worker is not None:
            self.worker.join(15)
        logging.getLogger().removeHandler(self.logs)
        self.stopped_event.set()

    def _call(self, callback):
        with self.lock:
            app = self.app
            if app is None or self.state == 'stopping':
                raise PanelError('请先启动机器人', 409)
            future = app.submit_control(lambda: callback(app))
        try:
            return future.result(timeout=15)
        except ControlTimeout:
            future.cancel()
            raise PanelError('操作仍在等待微信；请查看运行状态后再试', 409) from None

    def plugin_rows(self):
        with self.lock:
            if self.app is not None:
                return self.app.plugins.status()
            config = self.store.load()
            rows = []
            for path in sorted((self.root / 'plugins').glob('*/plugin.json')):
                if path.parent.name.startswith('_') or not (path.parent / '__init__.py').is_file():
                    continue
                manifest = read_json(path)
                options = config.get('plugins', {}).get(path.parent.name, {})
                rows.append({'name': path.parent.name, 'description': manifest.get('description', ''),
                    'enabled': options.get('enabled', manifest.get('default_enabled', True)),
                    'active': False, 'handled': 0, 'errors': 0, 'error': ''})
            return rows

    def snapshot(self):
        with self.lock:
            config = self.store.load()
            return {'state': self.state, 'connected': bool(self.app and self.app._connection_ready),
                    'account': config.get('wechat_account') or '',
                    'pid': getattr(getattr(self.app, 'backend', None), 'pid', None),
                    'started_at': self.started, 'error': self.error,
                    'connection_error': redact(self.app._last_connection_error) if self.app else '',
                    'plugins': self.plugin_rows()}

    def configuration(self):
        config = self.store.load()
        paths = self._paths(config)
        result = {'account': config.get('wechat_account') or '',
            'chat_allowlist': config.get('chat_allowlist', []),
            'text_send_delay': config.get('text_send_delay', {}),
            'plugins': config.get('plugins', {})}
        for name, path in paths.items():
            value = deepcopy(read_json(path))
            if name in ('llm', 'tts'):
                result[name] = {key: value[key] for key in
                    ('base_url', 'model', 'timeout_seconds', 'max_tokens', 'json_mode',
                     'token_limit_parameter', 'extra_body', 'default_voice', 'max_chars_per_clip') if key in value}
                if name == 'llm':
                    for key, default in (('json_mode', True), ('token_limit_parameter', 'max_tokens'), ('extra_body', {})):
                        result[name].setdefault(key, default)
                result[name]['api_key_configured'] = bool(value.get('api_key'))
            else:
                result['groups'] = list(value.get('groups', {}))
                result['history_enabled'] = value.get('history_enabled', True)
                result['daily_clear'] = value.get('daily_clear', True)
        return result

    def message_tail(self, after=0):
        return self.messages.tail(after)

    def _save_many(self, changes):
        backup = self.workspace / '.runtime/web/backups' / str(time.time_ns())
        backup.mkdir(parents=True)
        originals = {path: path.read_bytes() for path in changes}
        for index, (path, raw) in enumerate(originals.items()):
            (backup / f'{index}-{path.name}').write_bytes(raw)
        written = []
        try:
            for path, value in changes.items():
                atomic_json(path, value)
                written.append(path)
        except Exception:
            for path in reversed(written):
                path.write_bytes(originals[path])
            raise

    def save_configuration(self, data):
        with self.actions, self.lock, self._editing():
            config = self.store.load()
            paths, changes = self._paths(config), {}
            if 'chat_allowlist' in data:
                values = data['chat_allowlist']
                if not isinstance(values, list) or len(values) > 1000:
                    raise PanelError('会话白名单应为列表，最多 1000 项')
                for value in values:
                    validate_target(value)
                config['chat_allowlist'] = list(dict.fromkeys(values))
            if 'text_send_delay' in data:
                from .pacing import TextSendDelay
                TextSendDelay.from_config(data['text_send_delay'])
                config['text_send_delay'] = data['text_send_delay']
            for name in ('llm', 'tts'):
                if name not in data:
                    continue
                if name not in paths or not isinstance(data[name], dict):
                    raise PanelError('模型配置格式不正确')
                value = read_json(paths[name])
                fields = {'base_url', 'model', 'timeout_seconds'} | ({'max_tokens', 'json_mode', 'token_limit_parameter', 'extra_body'} if name == 'llm'
                            else {'default_voice', 'max_chars_per_clip'})
                for key in fields:
                    if key in data[name]:
                        value[key] = data[name][key]
                key = data[name].get('api_key', '')
                if not isinstance(key, str) or len(key) > 1024 or any(ord(ch) < 32 for ch in key):
                    raise PanelError('API 密钥格式不正确')
                if data[name].get('clear_api_key') is True:
                    value['api_key'] = ''
                elif key.strip():
                    value['api_key'] = key.strip()
                # 允许先保存未填密钥的模板，其余字段沿用真实客户端验证。
                validation = dict(value, api_key=value.get('api_key') or 'validation-only')
                if name == 'llm':
                    from .llm import LLMClient
                    LLMClient(validation)
                else:
                    from .tts import FishTTS
                    FishTTS.from_options(validation)
                changes[paths[name]] = value
            if any(key in data for key in ('groups', 'history_enabled', 'daily_clear')):
                if 'members' not in paths:
                    raise PanelError('请先在主配置设置 members_config_file')
                members = read_json(paths['members'])
                if 'groups' in data:
                    rooms = data['groups']
                    if not isinstance(rooms, list) or len(rooms) > 1000:
                        raise PanelError('群 ID 应为列表，最多 1000 项')
                    for room in rooms:
                        validate_target(room)
                        if not room.endswith('@chatroom'):
                            raise PanelError('请输入以 @chatroom 结尾的群 ID')
                    members['groups'] = {room: members.get('groups', {}).get(room, []) for room in dict.fromkeys(rooms)}
                    if config.get('chat_allowlist'):
                        config['chat_allowlist'] = list(dict.fromkeys(config['chat_allowlist'] + rooms))
                for key in ('history_enabled', 'daily_clear'):
                    if key in data:
                        if type(data[key]) is not bool:
                            raise PanelError('记录开关必须为布尔值')
                        members[key] = data[key]
                changes[paths['members']] = members
            changes[self.store.path] = config
            self._save_many(changes)

    def plugin_action(self, data):
        name, action = validate_name(data.get('name')), data.get('action')
        if action not in ('enable', 'disable', 'reload', 'configure'):
            raise PanelError('不支持的插件操作')
        if name not in {row['name'] for row in self.plugin_rows()}:
            raise PanelError('未找到该插件', 404)
        with self.actions:
            if action == 'configure':
                with self.lock, self._editing():
                    options = data.get('options')
                    if not isinstance(options, dict) or ('enabled' in options and type(options['enabled']) is not bool):
                        raise PanelError('插件配置应为 JSON 对象，enabled 应为布尔值')
                    config = self.store.load()
                    config.setdefault('plugins', {})[name] = options
                    self._save_many({self.store.path: config})
                return
            with self.lock:
                running = self.app is not None
            if running:
                self._call(lambda app: getattr(app.plugins, action)(name))
            else:
                with self.lock, self._editing():
                    if action == 'reload':
                        raise PanelError('插件未运行；请先启动机器人', 409)
                    self.store.set_enabled(name, action == 'enable')

    def chats(self):
        return self._call(lambda app: app.backend.chats() if app.backend and hasattr(app.backend, 'chats') else [])

    def group_members(self, room):
        from .group_management import validate_group_id
        room = validate_group_id(room)
        def query(app):
            if not app._connection_ready or app.backend is None:
                raise PanelError('微信尚未连接', 409)
            session = app.backend.session_id
            rows = app.service.group_members(room, expected_session=session)
            if rows is None:
                raise PanelError('群成员缓存尚未就绪，请在微信打开该群后重试', 409)
            return {'room': room, 'session_id': session, 'members': list(rows)}
        return self._call(query)

    def send_status(self, identifier):
        with self.lock:
            job = self.send_jobs.get(identifier)
            if job is None:
                raise PanelError('发送记录已失效，请查看消息监控确认结果', 404)
            return {key: job[key] for key in ('request_id', 'status', 'error')}

    def send_text(self, data):
        """立即返回发送单号，等待节奏和原生回调在独立线程完成，HTTP 不重试发送。"""
        target = validate_target(data.get('chat_id'))
        content, mentions = data.get('content'), data.get('at_list', [])
        identifier, session = data.get('request_id'), data.get('session_id')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', identifier):
            raise PanelError('发送请求编号无效')
        if not isinstance(session, str) or not session or len(session) > 80:
            raise PanelError('请先选择会话并刷新连接', 409)
        if not isinstance(content, str) or not content.strip() or '\0' in content or len(content.encode('utf-8')) > 1023:
            raise PanelError('正文应为 1–1023 个 UTF-8 字节，不能包含 NUL')
        if not isinstance(mentions, list):
            raise PanelError('艾特成员必须为列表')
        mentions = validate_mentions(target, mentions) if mentions else ()
        payload = (target, content, mentions, session)
        with self.lock:
            previous = self.send_jobs.get(identifier)
            if previous is not None:
                if previous['payload'] != payload:
                    raise PanelError('发送编号已使用，请重新提交', 409)
                return self.send_status(identifier)
            if any(job['status'] == 'sending' for job in self.send_jobs.values()):
                raise PanelError('上一条消息仍在发送，请等待结果', 409)
            app = self.app
            if app is None or not app._connection_ready or self.state == 'stopping':
                raise PanelError('请先启动并连接机器人', 409)
            if not app.service.session_is_current(session):
                raise PanelError('微信连接已变化，请重新选择会话', 409)
            allowed = app.config.get('chat_allowlist', [])
            if allowed and target not in allowed:
                raise PanelError('该会话不在收发白名单中')
            self.send_jobs[identifier] = {'request_id': identifier, 'payload': payload,
                                          'status': 'sending', 'error': ''}
            while len(self.send_jobs) > 64:
                del self.send_jobs[next(iter(self.send_jobs))]

        def deliver():
            try:
                if mentions:
                    rows = app.service.group_members(target, expected_session=session)
                    if rows is None:
                        raise ValueError('群成员缓存尚未就绪，请刷新成员后重试')
                    directory = {row['wxid']: row for row in rows}
                    if any(member not in directory for member in mentions):
                        raise ValueError('所选成员已不在群里，请刷新成员后重试')
                    names = [directory[member].get('nickname') or member for member in mentions]
                    result = app.service.send_at_text(target, content, mentions,
                        expected_session=session, display_names=names)
                else:
                    result = app.service.send_text(target, content, expected_session=session)
                if result is False:
                    raise RuntimeError('微信未确认发送')
            except Exception:
                # 不回显异常中的原生路径或请求内容，也不自动重发不确定的结果。
                with self.lock:
                    self.send_jobs[identifier].update(status='failed', error='发送未完成，请核对微信和消息监控后再决定是否重发')
            else:
                with self.lock:
                    self.send_jobs[identifier].update(status='sent')
        threading.Thread(target=deliver, name='PanelTextSend', daemon=True).start()
        return self.send_status(identifier)

    def send_connection(self):
        def query(app):
            if not app._connection_ready or app.backend is None:
                raise PanelError('微信尚未连接', 409)
            return {'session_id': app.backend.session_id}
        return self._call(query)


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, controller, port=8765):
        self.controller = controller
        self.token = secrets.token_urlsafe(32)
        super().__init__(('127.0.0.1', port), PanelHandler)
        self.timeout = 0.5
        port = self.server_address[1]
        self.origins = {f'http://127.0.0.1:{port}', f'http://localhost:{port}'}
        self.hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}


class PanelHandler(BaseHTTPRequestHandler):
    server_version = 'helper-local'

    def log_message(self, *_):
        pass

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def send(self, status, data, kind='application/json; charset=utf-8'):
        raw = json.dumps(data, ensure_ascii=False).encode('utf-8') if isinstance(data, (dict, list)) else data
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        self.wfile.write(raw)

    def _check(self, *, api=False):
        if self.headers.get('Host') not in self.server.hosts:
            raise PanelError('不允许的访问地址', 403)
        origin = self.headers.get('Origin')
        if origin is not None and origin not in self.server.origins:
            raise PanelError('不允许跨站访问管理台', 403)
        if api and not secrets.compare_digest(self.headers.get('X-Helper-Token', ''), self.server.token):
            raise PanelError('页面会话已失效，请刷新页面', 403)

    def _handle(self, write=False):
        try:
            self._check(api=self.path.startswith('/api/'))
            parsed = urlsplit(self.path)
            path = parsed.path
            controller = self.server.controller
            if not write and path in ('/', '/index.html', '/style.css', '/app.js'):
                name = 'index.html' if path == '/' else path[1:]
                raw = (controller.root / 'webui' / name).read_bytes()
                if name == 'index.html':
                    raw = raw.replace(b'__HELPER_TOKEN__', html.escape(self.server.token).encode('ascii'))
                kind = {'html': 'text/html', 'css': 'text/css', 'js': 'application/javascript'}[name.rsplit('.', 1)[1]]
                return self.send(200, raw, kind + '; charset=utf-8')
            hub = controller if hasattr(controller, 'robot') else None
            global_routes = ('/api/robots', '/api/accounts', '/api/accounts/prepare', '/api/accounts/start')
            if hub is not None and path not in global_routes:
                robot_id = parse_qs(parsed.query).get('robot', ['default'])[0]
                controller = hub.robot(robot_id)
            if write:
                if (self.headers.get_content_type() != 'application/json' or self.headers.get('Transfer-Encoding') or
                        not self.headers.get('Content-Length', '').isdigit()):
                    raise PanelError('请使用 JSON 请求')
                size = int(self.headers['Content-Length'])
                if not 1 <= size <= 65536:
                    raise PanelError('请求大小超出限制', 413)
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise PanelError('请求必须为 JSON 对象')
                if path == '/api/robots' and hub is not None:
                    return self.send(200, hub.create_robot(data))
                if path == '/api/accounts/prepare' and hub is not None:
                    return self.send(200, hub.prepare_account(data))
                if path == '/api/accounts/start' and hub is not None:
                    return self.send(200, hub.start_selected(data))
                if path == '/api/send-text':
                    return self.send(200, controller.send_text(data))
                actions = {'/api/start': lambda: controller.start(), '/api/stop': lambda: controller.stop(),
                    '/api/restart': lambda: controller.restart(),
                    '/api/account': lambda: controller.bind_account(data),
                    '/api/config': lambda: controller.save_configuration(data),
                    '/api/plugin': lambda: controller.plugin_action(data)}
                if path not in actions:
                    raise PanelError('接口不存在', 404)
                actions[path]()
                return self.send(200, {'ok': True})
            if path == '/api/state':
                return self.send(200, controller.snapshot())
            if path == '/api/config':
                return self.send(200, controller.configuration())
            if path == '/api/accounts':
                return self.send(200, hub.logged_accounts() if hub is not None else {'accounts': controller.accounts()})
            if path == '/api/chats':
                return self.send(200, {'chats': controller.chats()})
            if path == '/api/group-members':
                return self.send(200, controller.group_members(parse_qs(parsed.query).get('room', [''])[0]))
            if path == '/api/send-connection':
                return self.send(200, controller.send_connection())
            if path == '/api/send-status':
                return self.send(200, controller.send_status(parse_qs(parsed.query).get('id', [''])[0]))
            if path == '/api/logs':
                after = int(parse_qs(parsed.query).get('after', ['0'])[0])
                return self.send(200, controller.logs.tail(max(0, after)))
            if path == '/api/messages':
                after = int(parse_qs(parsed.query).get('after', ['0'])[0])
                return self.send(200, controller.message_tail(after))
            if path == '/api/robots':
                return self.send(200, hub.robot_list() if hub is not None else
                    {'robots': [{'id': 'default', 'name': '默认机器人', 'account': '', 'state': controller.state}]})
            raise PanelError('页面不存在', 404)
        except PanelError as error:
            self.send(error.status, {'error': redact(error)})
        except (ValueError, TypeError, KeyError) as error:
            self.send(400, {'error': redact(error)})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception as error:
            logging.getLogger('WebPanel').exception('管理操作失败')
            self.send(500, {'error': redact(error)})

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle(write=True)
