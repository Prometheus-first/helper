"""受管 Linux 微信会话：登录与机器人运行分离，停止机器人可复用登录。"""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import uuid

from .account import validate_account
from .linux_messages import LinuxTextAdapter
from .linux_panel import LinuxPanelController
from .linux_panel_backend import LinuxPanelBackend, PANEL_CAPABILITIES
from .panel_common import PanelError


def private_directory(path, parent):
    path, parent = Path(path), Path(parent).resolve()
    if path.is_symlink() or not path.absolute().is_relative_to(parent):
        raise PanelError('Linux 会话目录不在允许范围')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.resolve().is_relative_to(parent) or path.stat().st_uid != os.geteuid():
        raise PanelError('Linux 会话目录归属不正确')
    path.chmod(0o700)
    return path


class ManagedLinuxBackend(LinuxPanelBackend):
    def __init__(self, *args, instance_home, display=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance_home = Path(instance_home).resolve()
        self.identity_ready = False
        self.display = display

    def launch_environment(self, env):
        env = super().launch_environment(env)
        # WeChat's native config.ini F_SETLK namespace is rooted at HOME/.xwechat.
        # Keep DBus/Wayland/XDG_RUNTIME_DIR to preserve the same graphical session.
        env.update(HOME=str(self.instance_home), XDG_CONFIG_HOME=str(self.instance_home / '.config'),
            XDG_DATA_HOME=str(self.instance_home / '.local/share'),
            XDG_CACHE_HOME=str(self.instance_home / '.cache'), TMPDIR=str(self.instance_home / 'tmp'))
        if self.display is not None:
            env.update(self.display.environment())
            env.pop('WAYLAND_DISPLAY', None)
        return env

    def set_identity(self, profile):
        account = validate_account(profile['wxid'])
        if self.account and self.account != account:
            raise RuntimeError('登录账号发生变化，请关闭该微信会话后重新登录')
        self.account = account
        self.adapter = LinuxTextAdapter(account, self.session_id)
        self.identity_ready = True

    def convert_event(self, event):
        # No message-based identity guessing in multi-account mode.
        if not self.identity_ready or self.account not in (event.get('sender'), event.get('receiver')):
            return None
        return super().convert_event(event)

    def read_profile(self):
        return self._sender.current_profile()

    def bind_identity(self, account):
        return self._sender.bind_account(account)


class SessionLease:
    capabilities = PANEL_CAPABILITIES
    scope = ManagedLinuxBackend.scope

    def __init__(self, owner, allowed):
        self.owner, self.allowed = owner, frozenset(allowed)
        self.account = owner.profile['wxid']
        self.session_id = uuid.uuid4().hex
        self.closed = False

    @property
    def pid(self):
        return self.owner.backend.pid if self.owner.backend else None

    @property
    def last_error(self):
        if not self.owner.wechat_running():
            return RuntimeError('Linux 微信已退出')
        if not self.owner.profile.get('logged_in') or self.owner.profile.get('wxid') != self.account:
            return RuntimeError('微信账号已退出或变化，请重新登录后启动机器人')
        return self.owner.backend.last_error

    def start(self, emit):
        self.owner.attach(self, emit)
        return True

    def stop(self):
        self.closed = True
        self.owner.detach(self)
        return {'capture_stopped': True, 'wechat_left_running': self.owner.wechat_running()}

    def _send(self, method, chat_id, body):
        with self.owner.lock:
            if self.closed or self.owner.active_lease is not self or self.last_error:
                raise RuntimeError('这条消息所属的机器人连接已停止')
            if self.allowed and chat_id not in self.allowed:
                raise ValueError('该会话不在收发白名单中')
            backend = self.owner.backend
        return getattr(backend, method)(chat_id, body)

    def send_text(self, chat_id, text): return self._send('send_text', chat_id, text)
    def send_image(self, chat_id, path): return self._send('send_image', chat_id, path)
    def send_file(self, chat_id, path): return self._send('send_file', chat_id, path)
    def chats(self): return self.owner.backend.chats() if self.owner.backend else []


class LinuxSessionController:
    """一个 worker 拥有一个微信；账号工作区在原生识别后才选择。"""
    platform = 'linux'

    def __init__(self, root, workspace, *, wechat_path, backend_factory=ManagedLinuxBackend, headless=False):
        self.root, self.workspace = Path(root).resolve(), Path(workspace).resolve()
        self.wechat_path = Path(wechat_path).expanduser().absolute()
        self.backend_factory = backend_factory
        self.headless, self.display = headless, None
        self.lock = threading.RLock()
        self.actions = threading.Lock()
        self.stop_event, self.stopped_event = threading.Event(), threading.Event()
        self.capture_stop = threading.Event()
        self.profile = {'logged_in': False, 'wxid': '', 'name': ''}
        self.expected_account, self.login_error, self.login_state = '', '', 'closed'
        self.login_started_at = None
        self.backend = self.login_thread = None
        self.active_lease = self.emitter = None
        self._closed = False
        directory = self.root / '.runtime/linux'
        private_directory(self.workspace, directory)
        metadata = self.workspace / 'identity.json'
        if metadata.exists():
            identity = json.loads(metadata.read_text())
            self.expected_account = validate_account(identity.get('wxid'))
            self.profile = {'logged_in': False, 'wxid': self.expected_account, 'name': identity.get('name', '')}
            digest = hashlib.sha256(self.expected_account.encode('ascii')).hexdigest()[:24]
            self.panel = self._new_panel(self.root / '.runtime/linux/accounts' / digest)
        else:
            self.panel = self._new_panel(self.workspace / 'pending')

    def _new_panel(self, workspace):
        return LinuxPanelController(self.root, wechat_path=self.wechat_path, workspace=workspace, session=self)

    def __getattr__(self, name):
        return getattr(self.panel, name)

    def wechat_running(self):
        return bool(self.backend and self.backend.process and self.backend.process.poll() is None)

    def open_wechat(self):
        with self.actions, self.lock:
            if self.stop_event.is_set():
                raise PanelError('管理服务正在退出', 409)
            if self.wechat_running() or (self.login_thread and self.login_thread.is_alive()):
                return self.snapshot()
            self.capture_stop = threading.Event()
            self.login_state, self.login_error = 'starting', ''
            self.login_started_at = time.monotonic()
            self.profile = {'logged_in': False, 'wxid': '', 'name': ''}
            self.expected_account = ''
            old = self.panel
            self.panel = self._new_panel(self.workspace / 'pending')
            old.close()
            home = private_directory(self.workspace / 'home', self.workspace)
            for child in ('.config', '.local/share', '.cache', 'tmp'):
                private_directory(home / child, home)
            receive = self.root / 'native/linux/build/panel/text_bridge.so'
            send = self.root / 'native/linux/build/panel/send_bridge.so'
            if self.headless:
                from .linux_display import HeadlessDisplay
                self.display = HeadlessDisplay(self.workspace)
            try:
                options = {'instance_home': home}
                if self.display is not None:
                    options['display'] = self.display
                self.backend = self.backend_factory(self.wechat_path, receive, send, **options)
            except BaseException:
                self._close_display()
                raise
            self.login_thread = threading.Thread(target=self._watch, name='LinuxWechatSession', daemon=True)
            self.login_thread.start()
        return self.snapshot()

    def _watch(self):
        try:
            self.backend.start(self._on_message)
            self.login_state = 'waiting_login'
            while not self.capture_stop.is_set() and not self.stop_event.is_set():
                if not self.wechat_running():
                    self.login_state = 'closed'
                    break
                if self.backend.last_error:
                    raise RuntimeError('Linux 微信接收通道已结束')
                profile = self.backend.read_profile()
                self.accept_profile(profile)
                self.capture_stop.wait(1)
        except Exception as error:
            # Keep native and configuration secrets out of status and worker logs.
            self.login_error = 'Linux 微信会话失败（' + type(error).__name__ + '），请关闭该会话后重新登录'
            self.login_state = 'error'
        finally:
            self.profile = dict(self.profile, logged_in=False)
            self.panel.stop()
            if not self.wechat_running():
                self._close_display()

    def accept_profile(self, profile):
        if not profile.get('logged_in'):
            with self.lock:
                self.profile = dict(self.profile, logged_in=False)
                self.login_state = 'waiting_login'
            self.panel.stop()
            return
        account = validate_account(profile.get('wxid'))
        old = None
        with self.lock:
            changed = bool(self.expected_account and self.expected_account != account)
            if changed:
                self.profile = dict(self.profile, logged_in=False)
                self.login_error = '此会话已切换账号，请关闭它并重新登录微信'
                self.login_state = 'error'
            elif not self.expected_account:
                self.backend.bind_identity(account)
                self.backend.set_identity(profile)
                digest = hashlib.sha256(account.encode('ascii')).hexdigest()[:24]
                directory = self.root / '.runtime/linux/accounts'
                private_directory(directory, self.root / '.runtime/linux')
                workspace = private_directory(directory / digest, directory)
                old = self.panel
                self.panel = self._new_panel(workspace)
                self.expected_account = account
                # Chat logs follow the verified account, even when a session is reused.
                import logging
                from main import configure_logging
                configure_logging(self.panel.store.load(), directory=workspace / 'logs', console_enabled=False)
                logging.getLogger().addHandler(self.panel.logs)
                from .web_panel import atomic_json
                atomic_json(self.workspace / 'identity.json', {'wxid':account, 'name':profile.get('name', '')})
            if not changed:
                self.profile = {'logged_in': True, 'wxid': account, 'name': profile.get('name', '')}
                self.login_state, self.login_error = 'logged_in', ''
        if changed:
            self.panel.stop()
        if old is not None:
            old.close()

    def _on_message(self, message):
        with self.lock:
            lease, emit = self.active_lease, self.emitter
            if not lease or lease.closed or not emit or not self.profile.get('logged_in'):
                return
            if self.profile['wxid'] != lease.account:
                return
        emit(replace(message, session_id=lease.session_id))

    def lease(self, allowed):
        with self.lock:
            if not self.profile.get('logged_in') or not self.wechat_running():
                raise PanelError('请先登录微信并等待账号识别', 409)
            return SessionLease(self, allowed)

    def attach(self, lease, emit):
        with self.lock:
            if self.active_lease is not None and not self.active_lease.closed:
                raise RuntimeError('此微信账号已有正在运行的机器人')
            if lease.account != self.profile.get('wxid') or not self.profile.get('logged_in'):
                raise RuntimeError('微信登录账号已变化')
            self.active_lease, self.emitter = lease, emit

    def detach(self, lease):
        with self.lock:
            if self.active_lease is lease:
                self.active_lease = self.emitter = None

    def snapshot(self):
        # Never hold the owner lock while taking the panel lock (start takes them in reverse order).
        with self.lock:
            panel = self.panel
            identity = dict(platform='linux', account=self.profile.get('wxid') or self.expected_account,
                account_name=self.profile.get('name', ''), logged_in=bool(self.profile.get('logged_in')),
                pid=self.backend.pid if self.wechat_running() else None, login_state=self.login_state,
                login_pending=self.wechat_running() and not self.profile.get('logged_in'))
            error = self.login_error
        result = panel.snapshot()
        return dict(result, **identity, error=result.get('error') or error)

    def linux_login_qr(self):
        with self.lock:
            backend, display = self.backend, self.display
            if self.profile.get('logged_in'):
                return {'status': 'logged_in'}
            if self.login_state == 'error':
                return {'status': 'error', 'error': self.login_error}
            if display is None:
                return {'status': 'unavailable', 'error': '请以 --headless 启动管理台'}
            if not self.wechat_running():
                return {'status': 'starting' if self.login_state == 'starting' else 'exited'}
        encoded = display.capture_qr()
        with self.lock:
            if backend is not self.backend or display is not self.display or self.profile.get('logged_in'):
                return {'status': 'logged_in' if self.profile.get('logged_in') else 'exited'}
        if encoded:
            return {'status': 'qr_available', 'mime_type': 'image/png', 'qrcode_base64': encoded}
        elapsed = max(0, int(time.monotonic() - self.login_started_at)) if self.login_started_at is not None else 0
        return {'status': 'waiting', 'elapsed_seconds': elapsed}

    def _close_display(self):
        with self.lock:
            display, self.display = self.display, None
        if display is not None:
            display.close()

    def configuration(self):
        with self.lock:
            return dict(self.panel.configuration(), account=self.profile.get('wxid') or self.expected_account)

    def validate_request(self, path, data):
        if path in ('/api/linux-session/open', '/api/linux-session/close', '/api/start', '/api/stop', '/api/restart'):
            return
        if path in ('/api/config', '/api/plugin', '/api/plugin-source', '/api/plugin-business', '/api/plugin-reset'):
            account = self.profile.get('wxid') or self.expected_account
            if not account or data.get('expected_account') != account:
                raise PanelError('配置所属账号已变化，请刷新当前账号后再保存', 409)

    def guard_route(self, path, write):
        if path not in ('/api/linux-session/open', '/api/linux-session/close', '/api/linux-login-qr'):
            return self.panel.guard_route(path, write)

    def close_wechat(self):
        with self.actions:
            self.panel.stop()
            worker = self.panel.worker
            if worker is not None:
                worker.join(15)
                if worker.is_alive():
                    raise PanelError('机器人仍在清理，请稍后再关闭微信', 409)
            self.capture_stop.set()
            backend = self.backend
            if backend is not None:
                backend.stop()
                process = backend.process
                if process is not None and process.poll() is None:
                    # This Popen instance is our own child; pidfd avoids PID reuse.
                    descriptor = os.pidfd_open(process.pid)
                    try:
                        if Path(os.readlink(f'/proc/{process.pid}/exe')).resolve() != self.wechat_path.resolve():
                            raise PanelError('微信进程身份已变化，停止自动关闭', 409)
                        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
                    finally:
                        os.close(descriptor)
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        raise PanelError('微信尚未退出，请在该微信窗口手动退出', 409) from None
            if self.login_thread is not None:
                self.login_thread.join(3)
            self._close_display()
            with self.lock:
                self.profile = dict(self.profile, logged_in=False)
                self.login_state = 'closed'
                self.backend = None
        return {'ok': True}

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.stop_event.set()
        try:
            self.close_wechat()
        finally:
            self.panel.close()
            self.stopped_event.set()
