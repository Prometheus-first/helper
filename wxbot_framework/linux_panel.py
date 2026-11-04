"""Linux 管理台集成；单实例，账号仅由本轮来信确认，不读取 Windows 绑定。"""
from pathlib import Path
import json
import threading

from .linux_panel_backend import LinuxPanelBackend, PANEL_CAPABILITIES
from .linux_wechat import discover_wechat
from .members import MemberStore
from .panel_common import PanelError
from .runtime import BotApplication
from .web_panel import PanelController


class LinuxBotApplication(BotApplication):
    def __init__(self, root, store, backend):
        super().__init__(root, store, backend_factory=lambda: backend,
                         initial_capabilities=PANEL_CAPABILITIES, defer_members=True)
        self.config['wechat_account'] = None
        self._identity_lock = threading.Lock()
        self._attempted_connection = False

    def _connect(self):
        # Linux preload 随进程启动；失败后交还用户处理，不能反复拉起微信。
        if self._attempted_connection:
            raise RuntimeError(self._last_connection_error or 'Linux 微信连接已结束，请停止后重新启动')
        self._attempted_connection = True
        super()._connect()
        if not self._connection_ready:
            raise RuntimeError(self._last_connection_error or 'Linux 微信桥接连接失败')
        self._adopt_account()

    def _adopt_account(self):
        with self._identity_lock:
            if not self.config.get('wechat_account') and self.backend is not None and self.backend.account:
                self.config['wechat_account'] = self.backend.account
                self.members = MemberStore.from_config(self.config, self.store.path.parent)
                if self.members is not None:
                    self.members.initialize()
                self.plugins.member_store = self.members
                self.plugins.reload()

    def _emit(self, message):
        self._adopt_account()
        super()._emit(message)


class LinuxPanelController(PanelController):
    platform = 'linux'

    def __init__(self, root, *, wechat_path, receive_bridge=None, send_bridge=None,
                 backend_factory=LinuxPanelBackend, scan=discover_wechat, workspace=None, session=None, **kwargs):
        self.session = session
        self.wechat_path = Path(wechat_path).expanduser().absolute()
        self.receive_bridge = Path(receive_bridge or Path(root) / 'native/linux/build/panel/text_bridge.so')
        self.send_bridge = Path(send_bridge or Path(root) / 'native/linux/build/panel/send_bridge.so')
        self._scan_linux, self._backend_factory = scan, backend_factory
        workspace = Path(workspace or Path(root) / '.runtime/linux')
        workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        workspace.chmod(0o700)
        super().__init__(root, workspace=workspace,
                         app_factory=self._application, scan=lambda _: [], **kwargs)
        self._last_account = ''

    def _application(self, root, store):
        config = store.load()
        if self.session is not None:
            return LinuxBotApplication(root, store, self.session.lease(config.get('chat_allowlist', ())))
        backend = self._backend_factory(self.wechat_path, self.receive_bridge, self.send_bridge,
                                       allowed_targets=config.get('chat_allowlist', ()))
        return LinuxBotApplication(root, store, backend)

    def prepare_start_config(self):
        if self.session is not None:
            if not self.session.profile.get('logged_in') or not self.session.backend:
                raise PanelError('请先登录微信，等待自动识别账号', 409)
            self._last_account = self.session.profile['wxid']
            self.messages.reset()
            return self.store.load()
        if not self.wechat_path.is_file():
            raise PanelError('未找到 Linux 微信，请用 --wechat 指定可执行文件', 409)
        if not self.receive_bridge.is_file() or not self.send_bridge.is_file():
            raise PanelError('Linux 桥接尚未编译，请运行 ./start_web.sh', 409)
        if self._scan_linux():
            raise PanelError('请先退出已有的 Linux 微信窗口，再点击启动；Windows 微信可以保持打开', 409)
        # 账号没有固定绑定；上次账号的消息视图也不能混入本轮。
        self._last_account = ''
        self.messages.reset()
        return self.store.load()

    def snapshot(self):
        result = super().snapshot()
        backend = getattr(self.app, 'backend', None)
        account = (self.session.profile.get('wxid', '') if self.session is not None else getattr(backend, 'account', ''))
        if account:
            self._last_account = account
        return dict(result, platform='linux', account=account or self._last_account,
            connected=bool(result['connected'] and account),
            login_pending=bool((self.session is not None and self.session.wechat_running() and not account) or (result['connected'] and not account)),
            capabilities=sorted(PANEL_CAPABILITIES))

    def robot_list(self):
        snapshot = self.snapshot()
        return {'robots': [dict(snapshot, id='default', name='Linux 微信')]}

    def guard_route(self, path, write):
        unsupported = ('/api/moments', '/api/article', '/api/contact', '/api/group-members',
                       '/api/scheduled-', '/api/wechat-login', '/api/accounts/', '/api/account')
        if any(path.startswith(prefix) for prefix in unsupported) and path != '/api/accounts':
            raise PanelError('Linux 版尚未适配此功能', 409)

    def plugin_rows(self):
        rows = super().plugin_rows()
        for row in rows:
            manifest = json.loads((self.plugin_sources.path(row['name']) / 'plugin.json').read_text())
            missing = sorted(set(manifest.get('requires', ())) - PANEL_CAPABILITIES)
            row['supported'] = not missing
            if missing:
                row['error'] = 'Linux 暂缺能力：' + ', '.join(missing)
        return rows

    def plugin_action(self, data):
        if data.get('action') in ('enable', 'reload'):
            row = next((r for r in self.plugin_rows() if r['name'] == data.get('name')), None)
            if row and not row['supported']:
                raise PanelError(row['error'], 409)
        return super().plugin_action(data)

    def send_connection(self):
        if not self.snapshot()['connected']:
            raise PanelError('请先登录，并用另一账号发一条消息以确认当前账号', 409)
        return super().send_connection()

    def send_text(self, data):
        self.send_connection()
        if data.get('at_list'):
            raise PanelError('Linux 版暂不支持真实群艾特', 409)
        return super().send_text(data)

    def send_media(self, data):
        """本机路径发送；单号去重和会话检查与文字发送保持一致。"""
        from .text import validate_target
        import re
        target = validate_target(data.get('chat_id'))
        kind, path = data.get('kind'), data.get('path')
        identifier, session = data.get('request_id'), data.get('session_id')
        if kind not in ('image', 'file') or not isinstance(path, str) or not path or not Path(path).is_absolute():
            raise PanelError('请选择图片或文件，并填写 Linux 本机的绝对路径')
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', identifier):
            raise PanelError('发送请求编号无效')
        if session != self.send_connection()['session_id']:
            raise PanelError('微信连接已变化，请刷新会话', 409)
        payload = (target, kind, path, session)
        with self.lock:
            previous = self.send_jobs.get(identifier)
            if previous is not None:
                if previous['payload'] != payload:
                    raise PanelError('发送编号已使用，请重新提交', 409)
                return self.send_status(identifier)
            if any(job['status'] == 'sending' for job in self.send_jobs.values()):
                raise PanelError('上一条消息仍在发送，请等待结果', 409)
            app = self.app
            allowed = app.config.get('chat_allowlist', [])
            if allowed and target not in allowed:
                raise PanelError('该会话不在收发白名单中')
            self.send_jobs[identifier] = {'request_id': identifier, 'payload': payload, 'status': 'sending', 'error': ''}
            while len(self.send_jobs) > 64:
                del self.send_jobs[next(iter(self.send_jobs))]
        def deliver():
            try:
                result = getattr(app.service, 'send_' + kind)(target, path, expected_session=session)
                if result is False:
                    raise RuntimeError('微信未确认发送')
            except Exception:
                with self.lock:
                    self.send_jobs[identifier].update(status='failed', error='发送未完成，请核对微信后再决定是否重发')
            else:
                with self.lock:
                    self.send_jobs[identifier].update(status='sent')
        threading.Thread(target=deliver, name='LinuxPanelMedia', daemon=True).start()
        return self.send_status(identifier)
