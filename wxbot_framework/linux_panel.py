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


def _backend_capabilities(backend):
    capabilities = getattr(backend, 'capabilities', None)
    if isinstance(capabilities, (set, frozenset)) and all(isinstance(item, str) for item in capabilities):
        return frozenset(capabilities)
    return PANEL_CAPABILITIES


class LinuxBotApplication(BotApplication):
    def __init__(self, root, store, backend):
        super().__init__(root, store, backend_factory=lambda: backend,
                         initial_capabilities=_backend_capabilities(backend), defer_members=True)
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
        self.emoticon_jobs = {}

    def _emoticon_current(self, app, backend, session, account):
        return bool(self.app is app and getattr(app, '_connection_ready', False) and
            app.backend is backend and backend.session_id == session and
            app.config.get('wechat_account') == account and app.service.session_is_current(session))

    def emoticon_export_status(self, identifier):
        with self.lock:
            job = self.emoticon_jobs.get(identifier)
            if job is None:
                raise PanelError('表情导出任务不存在，请重新导出', 404)
            if not self._emoticon_current(*job['identity']):
                return dict(request_id=identifier, status='cancelled', error='微信连接已变化，已隐藏旧账号的导出结果')
            return {key: job[key] for key in ('request_id', 'status', 'error', 'result')}

    def export_emoticons(self, data):
        """账号绑定的后台导出，不发送消息，也不开放任意本地文件读取。"""
        import re
        identifier, session, convert = data.get('request_id'), data.get('session_id'), data.get('convert', True)
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,80}', identifier):
            raise PanelError('导出请求编号无效')
        if type(convert) is not bool:
            raise PanelError('请选择是否生成 GIF 预览')
        if session != self.send_connection()['session_id']:
            raise PanelError('微信连接已变化，请刷新页面', 409)
        with self.lock:
            app, backend = self.app, self.app.backend
            account = app.config.get('wechat_account')
            if backend is None or 'emoticon.extract' not in getattr(backend, 'capabilities', ()):
                raise PanelError('当前 Linux 微信连接尚未开放表情导出', 409)
            identity = (app, backend, session, account)
            if not self._emoticon_current(*identity):
                raise PanelError('微信连接已变化，请刷新页面', 409)
            payload = (session, account, convert)
            previous = self.emoticon_jobs.get(identifier)
            if previous is not None:
                if previous['payload'] != payload:
                    raise PanelError('导出编号已使用，请重新提交', 409)
                return self.emoticon_export_status(identifier)
            if any(job['status'] == 'exporting' for job in self.emoticon_jobs.values()):
                raise PanelError('上一项表情导出仍在处理，请等待完成', 409)
            self.emoticon_jobs[identifier] = dict(request_id=identifier, payload=payload,
                identity=identity, status='exporting', error='', result=None)
            while len(self.emoticon_jobs) > 16:
                del self.emoticon_jobs[next(iter(self.emoticon_jobs))]

        def export():
            result, failed = None, False
            try:
                rows = app.service.export_emoticons(convert=convert, expected_session=session)
                if not isinstance(rows, (list, tuple)) or len(rows) > 256:
                    raise ValueError('invalid export result')
                items, directories = [], set()
                for number, row in enumerate(rows, 1):
                    item = dict(number=number, format=str(row.format)[:16], sendable=bool(row.sendable))
                    for key in ('original_path', 'thumbnail_path', 'gif_path'):
                        path = getattr(row, key, None)
                        if path is not None:
                            path = Path(path)
                            if not path.is_absolute():
                                raise ValueError('invalid export path')
                            item[key] = str(path)
                            directories.add(str(path.parent.parent))
                    error = str(row.error)
                    item['error'] = error if re.fullmatch(r'[a-z0-9_:;, .-]{0,256}', error) else 'export_failed'
                    items.append(item)
                result = dict(count=len(items), sendable_count=sum(item['sendable'] for item in items),
                    output_directories=sorted(directories), items=items)
            except Exception:
                failed = True
            with self.lock:
                job = self.emoticon_jobs[identifier]
                if not self._emoticon_current(*identity):
                    job.update(status='cancelled', error='微信连接已变化，已隐藏旧账号的导出结果', result=None)
                elif failed:
                    job.update(status='failed', error='表情导出未完成，请检查本地缓存和当前微信连接', result=None)
                else:
                    job.update(status='done', result=result)
        threading.Thread(target=export, name='LinuxEmoticonExport', daemon=True).start()
        return self.emoticon_export_status(identifier)

    def _application(self, root, store):
        config = store.load()
        if self.session is not None:
            lease = self.session.lease(config.get('chat_allowlist', ()),
                                       image_on_demand_chats=config.get('image_on_demand_chats', ()))
            return LinuxBotApplication(root, store, lease)
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

    def _capabilities(self):
        # 运行中的 lease 优先；未启动机器人时使用已登录微信的握手结果。
        # 不获取 session.lock，避免与会话侧的 panel 调用形成反向锁顺序。
        with self.lock:
            backend = getattr(self.app, 'backend', None)
            if backend is None:
                backend = getattr(self.session, 'backend', None)
            return _backend_capabilities(backend)

    def snapshot(self):
        result = super().snapshot()
        backend = getattr(self.app, 'backend', None)
        account = (self.session.profile.get('wxid', '') if self.session is not None else getattr(backend, 'account', ''))
        if account:
            self._last_account = account
        return dict(result, platform='linux', account=account or self._last_account,
            connected=bool(result['connected'] and account),
            login_pending=bool((self.session is not None and self.session.wechat_running() and not account) or (result['connected'] and not account)),
            capabilities=sorted(self._capabilities()))

    def linux_contact_labels(self):
        def query(app):
            if not app._connection_ready or app.backend is None:
                raise PanelError('微信尚未连接', 409)
            backend, session = app.backend, app.backend.session_id
            try:
                result = app.service.contact_labels(expected_session=session)
            except RuntimeError as error:
                raise PanelError(str(error), 409) from None
            if (not app._connection_ready or app.backend is not backend or
                    backend.session_id != session or not app.service.session_is_current(session)):
                raise PanelError('标签所属的微信连接已结束，请重新扫描', 409)
            return result
        return self._call(query)

    def robot_list(self):
        snapshot = self.snapshot()
        return {'robots': [dict(snapshot, id='default', name='Linux 微信')]}

    def scheduled_account(self):
        if self.session is not None:
            profile = self.session.profile
            return profile.get('wxid', '') if profile.get('logged_in') else ''
        app = self.app
        return app.config.get('wechat_account', '') if app and app._connection_ready else ''

    def guard_route(self, path, write):
        unsupported = ('/api/moments', '/api/article', '/api/contact',
                       '/api/wechat-login', '/api/accounts/', '/api/account')
        if any(path.startswith(prefix) for prefix in unsupported) and path not in ('/api/accounts', '/api/contacts'):
            raise PanelError('Linux 版尚未适配此功能', 409)

    def plugin_rows(self):
        rows = super().plugin_rows()
        capabilities = self._capabilities()
        for row in rows:
            manifest = json.loads((self.plugin_sources.path(row['name']) / 'plugin.json').read_text())
            missing = sorted(set(manifest.get('requires', ())) - capabilities)
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
        return super().send_text(data)

    def _deliver_at_text(self, service, target, content, mentions, session):
        rows = service.group_members(target, expected_session=session)
        names = None
        if rows is not None:
            directory = {row['wxid']: row for row in rows}
            if any(member not in directory for member in mentions):
                raise ValueError('所选成员已不在群里，请刷新成员后重试')
            names = [directory[member].get('nickname') or member for member in mentions]
        # An unavailable cache still permits the explicit ID fallback. It never
        # resolves ambiguous display names to identities or guesses membership.
        return service.send_at_text(target, content, mentions, expected_session=session, display_names=names)

    def send_media(self, data):
        """本机路径发送；单号去重和会话检查与文字发送保持一致。"""
        from .text import validate_target
        import re
        target = validate_target(data.get('chat_id'))
        kind, path = data.get('kind'), data.get('path')
        identifier, session = data.get('request_id'), data.get('session_id')
        if kind not in ('image', 'file', 'voice', 'video') or not isinstance(path, str) or not path or not Path(path).is_absolute():
            raise PanelError('请选择图片、文件、语音或视频，并填写 Linux 本机的绝对路径')
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
            if kind in ('voice', 'video'):
                backend = getattr(app, 'backend', None)
                if backend is None or backend.session_id != session:
                    raise PanelError('微信连接已变化，请刷新会话', 409)
                if kind + '.send' not in getattr(backend, 'capabilities', ()):
                    label = '语音' if kind == 'voice' else '视频'
                    raise PanelError(f'当前 Linux 微信连接尚未开放{label}发送，请更新桥接并重新登录', 409)
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
