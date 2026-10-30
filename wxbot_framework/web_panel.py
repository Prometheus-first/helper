"""仅监听本机的管理台；配置、密钥和原生生命周期仍由当前项目管理。"""
from __future__ import annotations

from collections import deque, OrderedDict
from concurrent.futures import TimeoutError as ControlTimeout
from contextlib import contextmanager
from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading
import time

from .config import ConfigStore, account_is_unbound, initialize_configs, validate_name
from .locking import InstanceLock
from .panel_common import PanelError as PanelError, redact as redact, redact_full
from .panel_http import PanelHandler as PanelHandler, PanelServer as PanelServer
from .message_monitor import MessageBuffer
from .moments import MomentsPublisher
from .moments_interactions import MomentsInteractions
from .plugin_sources import PluginSources
from .plugin_business import view as business_view, merge as merge_business
from .runtime import BotApplication
from .startup import scan_logged_accounts
from .text import validate_target, validate_mentions
from .scheduled_tasks import ScheduledTasks
from .scheduled_actions import discover_actions

__all__ = ['LogBuffer', 'PanelController', 'PanelError', 'PanelHandler', 'PanelServer',
           'atomic_json', 'read_json', 'redact']

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
        self.plugin_sources = PluginSources(self.root / 'plugins', self.workspace)
        self.factory, self.scan, self.lock_factory = app_factory, scan, lock_factory
        self.lock = threading.RLock()
        self.actions = threading.Lock()
        self.app = self.worker = None
        self.state, self.error, self.started = 'stopped', '', None
        self.stop_event, self.stopped_event = threading.Event(), threading.Event()
        self.logs = LogBuffer()
        self.messages = MessageBuffer(sanitize=redact_full)
        self.send_jobs = {}
        self.scheduled_tasks = ScheduledTasks(self, atomic_json, read_json)
        self._moments_identity = None
        self._moments_read_lock = threading.Lock()
        self._moments_seen = OrderedDict()
        self.moments = MomentsPublisher(self.workspace, self._deliver_moments, self._moments_session)
        self.moments_interactions = MomentsInteractions(self._deliver_moments_interaction, self._moments_session,
                                                       self._moments_allowed)
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
                try:
                    self.scheduled_tasks.start()
                except Exception:
                    logging.getLogger('WebPanel').exception('定时任务启动失败')
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
        self.scheduled_tasks.close()
        self.moments.close()
        self.moments_interactions.close()
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
                name = path.parent.name
                path = self.plugin_sources.path(name) / 'plugin.json'
                manifest = read_json(path)
                options = config.get('plugins', {}).get(name, {})
                rows.append({'name': name, 'description': manifest.get('description', ''),
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
                result['group_names'] = value.get('group_names', {})
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
            if any(key in data for key in ('groups', 'group_names', 'history_enabled', 'daily_clear')):
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
                if 'group_names' in data:
                    names = data['group_names']
                    rooms = members.get('groups', {})
                    if not isinstance(names, dict) or len(names) > 1000:
                        raise PanelError('群备注格式不正确')
                    existing = members.get('group_names', {})
                    if not isinstance(existing, dict):
                        raise PanelError('已有群备注格式不正确')
                    updated = dict(existing)
                    for room, name in names.items():
                        if room not in rooms or not isinstance(name, str) or len(name) > 64 or any(ord(ch) < 32 for ch in name):
                            raise PanelError('群备注应属于已配置的群，且不超过 64 个字符')
                        name = name.strip()
                        if name:
                            updated[room] = name
                        else:
                            updated.pop(room, None)
                    members['group_names'] = updated
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

    def plugin_editor(self, name):
        name = validate_name(name)
        with self.lock:
            options = self.store.load().get('plugins', {}).get(name, {})
            result = self.plugin_sources.snapshot(name, options)
            result['running'] = bool(self.app is not None and self.state != 'stopping')
            result['active'] = bool(self.app and self.app.plugins.is_active(name))
            return result

    def plugin_business(self, name):
        return business_view(self.plugin_editor(name))

    def save_plugin_business(self, data):
        if data.get('action', 'save') not in ('save', 'reset'):
            raise PanelError('不支持的业务函数操作')
        return self.save_plugin_source(data, reset=data.get('action') == 'reset', business=True)

    def save_plugin_source(self, data, *, reset=False, business=False):
        name = validate_name(data.get('name'))
        if not isinstance(data.get('revision'), str):
            raise PanelError('缺少源码版本，请重新打开编辑器', 409)
        with self.actions:
            with self.lock:
                if self.state == 'stopping' or self.stop_event.is_set():
                    raise PanelError('机器人正在停止，请稍后重试', 409)
                running = self.app is not None
            if running:
                return self._call(lambda app: self._write_plugin_source(name, data, reset=reset, app=app, business=business))
            with self.lock, self._editing():
                return self._write_plugin_source(name, data, reset=reset, business=business)

    def _write_plugin_source(self, name, data, *, reset, app=None, business=False):
        with self.lock:
            before = self.store.load()
            sources = self.plugin_sources
            current = sources.snapshot(name, before.get('plugins', {}).get(name, {}))
            if data['revision'] != current['revision']:
                raise PanelError('插件已被其他页面修改，请重新读取后再保存', 409)
            options = sources.defaults(name) if reset else data.get('options')
            if not isinstance(options, dict) or ('enabled' in options and type(options['enabled']) is not bool):
                raise PanelError('插件配置应为 JSON 对象，enabled 应为布尔值')
            previous = sources.pointer(name)
            files = merge_business(current['files'], data.get('functions')) if business and not reset else data.get('files')
            revision = None if reset else sources.prepare(name, files)
            config = deepcopy(before)
            config.setdefault('plugins', {})[name] = options
            try:
                sources.activate(name, revision)
                self._save_many({self.store.path: config})
                if app is not None:
                    app.plugins.config = config
                    spec = app.plugins.discover().get(name)
                    if spec is None:
                        raise ValueError('插件清单无法加载')
                    if options.get('enabled', spec.default_enabled):
                        app.plugins.load_one(name)
                    else:
                        app.plugins.disable(name, persist=False)
            except BaseException as error:
                sources.activate(name, previous)
                atomic_json(self.store.path, before)
                if app is not None:
                    app.plugins.config = before
                detail = redact(error) if isinstance(error, Exception) else '插件主动中止加载'
                raise PanelError('保存未生效，已恢复原版本：' + detail, 400) from None
            result = self.plugin_editor(name)
            result['applied'] = bool(app is not None and app.plugins.is_active(name))
            return business_view(result) if business else result

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

    def contacts(self):
        def query(app):
            if not app._connection_ready or app.backend is None:
                raise PanelError('微信尚未连接', 409)
            backend, session = app.backend, app.backend.session_id
            try:
                snapshot = app.service.contacts(expected_session=session)
            except RuntimeError as error:
                raise PanelError(str(error), 409) from None
            if (not app._connection_ready or app.backend is not backend or
                    backend.session_id != session or not app.service.session_is_current(session)):
                raise PanelError('联系人所属的微信连接已结束，请重新扫描', 409)
            if snapshot is None:
                raise PanelError('微信联系人缓存尚未就绪，请稍后重试', 409)
            return dict(snapshot, session_id=session)
        return self._call(query)

    def list_scheduled_tasks(self):
        self.scheduled_tasks.start()
        account = self.store.load().get('wechat_account')
        return {'tasks': [row for row in self.scheduled_tasks.list()['tasks'] if row.get('account') == account]}

    def scheduled_action_types(self):
        return {'actions': discover_actions(self.root, self.plugin_sources)}

    def execute_scheduled_plugin(self, action_id, params, session, account, valid):
        with self.lock:
            app = self.app
            if (self.state != 'running' or app is None or not app._connection_ready or
                    app.config.get('wechat_account') != account or app.stop_event.is_set()):
                raise PanelError('机器人或账号已停止', 409)
            allowed = list(app.config.get('chat_allowlist', []))
        def still_current():
            with self.lock:
                current = (self.app is app and self.state == 'running' and app._connection_ready and
                           not app.stop_event.is_set() and getattr(app.backend, 'session_id', None) == session)
            return bool(current and valid())
        return app.plugins.run_scheduled(action_id, params, session, allowed, still_current)

    def save_scheduled_task(self, data):
        return self.scheduled_tasks.save(data)

    def delete_scheduled_task(self, data):
        return self.scheduled_tasks.delete(data)

    def toggle_scheduled_task(self, data):
        return self.scheduled_tasks.toggle(data)

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
        if not isinstance(content, str) or not content.strip() or '\0' in content:
            raise PanelError('正文不能为空或包含 NUL')
        try:
            content.encode('utf-8')
        except UnicodeEncodeError:
            raise PanelError('正文包含无效的 Unicode 字符') from None
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

    def _moments_session(self):
        """只查看当前运行连接，不启动机器人，也不复用切号前的连接身份。"""
        with self.lock:
            app = self.app
            if (self.stop_event.is_set() or self.state != 'running' or app is None or
                    not app._connection_ready or app.backend is None):
                return None
            backend = app.backend
            session = getattr(backend, 'session_id', None)
            account = app.config.get('wechat_account')
            if not isinstance(session, str) or not session or account_is_unbound(account):
                return None
            actual = getattr(getattr(backend, 'raw', None), 'self_wxid', None)
            if actual is not None and actual != account:
                return None
            identity = (session, account, backend)
            previous = self._moments_identity
            if previous is not None and previous[0] == session and (previous[1] != account or previous[2] is not backend):
                return None
            if previous is not None and previous != identity:
                self._moments_seen.clear()
            self._moments_identity = identity
            return {'session_id': session, 'account': account}

    def moments_connection(self):
        current = self._moments_session()
        if current is None:
            raise PanelError('请先启动并连接需要发布朋友圈的机器人账号', 409)
        with self.lock:
            if self._moments_session() != current:
                raise PanelError('微信连接或发布账号已变化，请刷新后重新选择', 409)
            app, backend = self.app, self.app.backend
            service = getattr(app, 'service', None)
        capability = getattr(service, 'moments_capabilities', None)
        supported, reason = False, '当前服务尚未接入朋友圈发布能力'
        interaction_supported, interaction_reported = False, False
        if callable(capability):
            try:
                result = capability(expected_session=current['session_id'])
            except Exception:
                result = None
                reason = '当前连接暂时无法确认朋友圈发布能力，请刷新后重试'
            if isinstance(result, dict):
                supported = result.get('supported') is True
                interaction_reported = 'interaction_supported' in result
                interaction_supported = (result.get('interaction_supported') is True and
                                         callable(getattr(service, 'interact_moments', None)))
                value = result.get('reason')
                if isinstance(value, str) and value.strip():
                    # 原生诊断仅提取说明；路径、凭据和异常原文不透传到页面。
                    if not re.search(r'[A-Za-z]:[\\/]|(?:^|\s)/(?:Users|home|tmp|var)/', value):
                        reason = redact(value)[:500]
                elif not supported:
                    reason = '当前微信版本的朋友圈原生发布能力尚未验证，暂不能发布'
        if not callable(getattr(service, 'publish_moments', None)):
            supported, reason = False, '当前服务尚未接入朋友圈发布接口'
        if self._moments_session() != current or self.app is not app or app.backend is not backend:
            raise PanelError('微信连接或发布账号已变化，请刷新后重新选择', 409)
        response = dict(current, supported=supported, reason='' if supported else reason, images=True,
                        max_images=9, visibility=['public', 'private'])
        if interaction_reported:
            response['interaction_supported'] = interaction_supported
        return response

    def publish_moments(self, data):
        # 已存在的同一请求交回任务模块核对内容；断线后查询/重复点按不会另发。
        if isinstance(data, dict):
            try:
                self.moments.status(data.get('request_id'))
            except PanelError as error:
                if error.status != 404:
                    raise
            else:
                return self.moments.submit(data)
        connection = self.moments_connection()
        if not connection['supported']:
            raise PanelError(connection['reason'], 409)
        return self.moments.submit(data)

    def moments_status(self, identifier):
        return self.moments.status(identifier)

    def _moments_allowed(self, session, sns_id):
        with self.lock:
            current = self._moments_session()
            return current is not None and current['session_id'] == session and sns_id in self._moments_seen

    def interact_moments(self, data):
        # 已接收的相同编号可查询，不依赖此时是否仍在同一连接。
        if isinstance(data, dict):
            try:
                self.moments_interactions.status(data.get('request_id'))
            except PanelError as error:
                if error.status != 404:
                    raise
            else:
                return self.moments_interactions.submit(data)
        connection = self.moments_connection()
        if not connection.get('interaction_supported'):
            raise PanelError('当前连接的朋友圈互动能力尚未启用', 409)
        return self.moments_interactions.submit(data)

    def moments_interaction_status(self, identifier):
        return self.moments_interactions.status(identifier)

    def read_moments(self, data):
        """仅手动读取当前连接的本地缓存，不保存正文，也不触发微信翻页。"""
        if not isinstance(data, dict) or type(data.get('limit', 20)) is not int or not 1 <= data.get('limit', 20) <= 50:
            raise PanelError('读取条数必须是 1 到 50 的整数')
        current = self._moments_session()
        if current is None or data.get('session_id') != current['session_id']:
            raise PanelError('微信连接已变化，请重新选择账号后读取', 409)
        if not self._moments_read_lock.acquire(blocking=False):
            raise PanelError('正在读取朋友圈，请稍候', 409)
        try:
            with self.lock:
                if self._moments_session() != current:
                    raise PanelError('微信连接已变化，请重新读取', 409)
                app, backend = self.app, self.app.backend
                service = getattr(app, 'service', None)
            read = getattr(service, 'read_moments', None)
            if not callable(read):
                raise PanelError('请更新管理台后重新启动机器人', 409)
            try:
                result = read(data.get('limit', 20), expected_session=current['session_id'])
            except Exception as error:
                from .moments_reader import MomentsReadError
                if isinstance(error, MomentsReadError):
                    raise PanelError(str(error), 409) from None
                raise PanelError('朋友圈读取失败，请确认电脑微信已打开朋友圈后重试', 409) from None
            if self._moments_session() != current or self.app is not app or app.backend is not backend:
                raise PanelError('微信连接已变化，已丢弃旧账号的读取结果', 409)
            # 不转发进程地址、诊断或原生元数据，仅返回页面需要的已验证字段。
            fields = ('sns_id', 'author', 'nickname', 'is_self', 'timestamp', 'time',
                      'private', 'content', 'content_type', 'media_count')
            interactions = ('raw_like_flag', 'likes', 'comments', 'likes_total', 'comments_total',
                            'likes_truncated', 'comments_truncated')
            item_fields = ('username', 'nickname', 'content', 'type', 'timestamp', 'comment_id')
            def clean_row(row):
                result_row = {key: row[key] for key in fields}
                for key in interactions:
                    if key not in row:
                        continue
                    value = row[key]
                    result_row[key] = ([{field: entry[field] for field in item_fields} for entry in value]
                                       if key in ('likes', 'comments') else value)
                return result_row
            rows = [clean_row(row) for row in result['rows']]
            with self.lock:
                if self._moments_session() != current or self.app is not app or app.backend is not backend:
                    raise PanelError('微信连接已变化，已丢弃旧账号的读取结果', 409)
                for row in rows:
                    identifier = row['sns_id']
                    if isinstance(identifier, str) and re.fullmatch(r'[1-9][0-9]{0,19}', identifier) and int(identifier) <= 0xffffffffffffffff:
                        self._moments_seen[identifier] = None
                        self._moments_seen.move_to_end(identifier)
                while len(self._moments_seen) > 1000:
                    self._moments_seen.popitem(last=False)
            return dict(current, source='loaded_pc_moments_cache',
                        rows=rows,
                        loaded_count=result['loaded_count'], truncated=result['truncated'])
        finally:
            self._moments_read_lock.release()

    def _deliver_moments(self, payload):
        current = self._moments_session()
        if current is None or current['session_id'] != payload['session_id']:
            return {'status': 'failed', 'phase': 'preflight'}
        with self.lock:
            if self._moments_session() != current:
                return {'status': 'failed', 'phase': 'preflight'}
            app, backend = self.app, self.app.backend
            service = getattr(app, 'service', None)
        try:
            capability = self.moments_connection()
        except PanelError:
            return {'status': 'failed', 'phase': 'preflight'}
        if (not capability['supported'] or capability['account'] != current['account'] or
                self._moments_session() != current or self.app is not app or app.backend is not backend):
            return {'status': 'failed', 'phase': 'preflight'}
        publish = getattr(service, 'publish_moments', None)
        if not callable(publish):
            return {'status': 'failed', 'phase': 'preflight'}
        # 不用 _call() 的 15 秒 Future 超时：临时图必须一直保留到原生函数真正退出。
        result = publish(payload, expected_session=payload['session_id'])
        if self._moments_session() != current or self.app is not app or app.backend is not backend:
            return {'status': 'unknown', 'unload_blocked': isinstance(result, dict) and result.get('unload_blocked') is True,
                    'retain_images': isinstance(result, dict) and result.get('retain_images') is True}
        return result

    def _deliver_moments_interaction(self, payload):
        current = self._moments_session()
        if current is None or current['session_id'] != payload['session_id']:
            return {'status': 'failed', 'phase': 'preflight'}
        with self.lock:
            if self._moments_session() != current or not self._moments_allowed(payload['session_id'], payload['sns_id']):
                return {'status': 'failed', 'phase': 'preflight'}
            app, backend = self.app, self.app.backend
            service = getattr(app, 'service', None)
        try:
            capability = self.moments_connection()
        except PanelError:
            return {'status': 'failed', 'phase': 'preflight'}
        if (not capability.get('interaction_supported') or capability['account'] != current['account'] or
                self._moments_session() != current or self.app is not app or app.backend is not backend):
            return {'status': 'failed', 'phase': 'preflight'}
        interact = getattr(service, 'interact_moments', None)
        if not callable(interact):
            return {'status': 'failed', 'phase': 'preflight'}
        result = interact(payload, expected_session=payload['session_id'])
        if self._moments_session() != current or self.app is not app or app.backend is not backend:
            return {'status': 'unknown', 'unload_blocked': isinstance(result, dict) and result.get('unload_blocked') is True}
        return result
