"""受管 Linux 微信会话：登录与机器人运行分离，停止机器人可复用登录。"""
from dataclasses import replace
from collections import OrderedDict
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
        self.adapter = LinuxTextAdapter(account, self.session_id, include_self_groups=True)
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

    def __init__(self, owner, allowed, *, image_on_demand_chats=()):
        self.owner, self.allowed = owner, frozenset(allowed)
        self.account = owner.profile['wxid']
        self.capabilities = getattr(owner.backend, 'capabilities', PANEL_CAPABILITIES)
        self._image_on_demand_chats = frozenset(image_on_demand_chats)
        if self._image_on_demand_chats:
            self.capabilities = self.capabilities | {'image.fetch'}
        self.session_id = uuid.uuid4().hex
        self.closed = False
        self._native_backend = owner.backend
        self._delivery_lock = threading.RLock()
        self._media_receiver = None
        self._voice_receiver = None
        self._pending_media = {}
        self._delivered_media = OrderedDict()
        self._emitter = None
        self._image_scope_generation = None
        from .friend_requests import PendingFriendRequests
        self._friend_requests = PendingFriendRequests(self.account, self.session_id)
        self._friend_seen = OrderedDict()

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
        self._emitter = emit
        self.owner.attach(self, emit)
        configure = getattr(self._native_backend, 'configure_image_downloads', None)
        if callable(configure):
            try:
                self._image_scope_generation = configure(self.allowed)
            except (OSError, ValueError, RuntimeError):
                # 旧桥接或上下文未就绪时仍可接收文字与已经落盘的附件。
                self._image_scope_generation = None
        return True

    def stop(self):
        self.closed = True
        self.owner.detach(self)
        with self._delivery_lock:
            receiver, self._media_receiver = self._media_receiver, None
            voice_receiver, self._voice_receiver = self._voice_receiver, None
            for gate in self._pending_media.values():
                gate.set()
            self._pending_media.clear()
            self._delivered_media.clear()
            self._friend_requests.close()
            self._friend_seen.clear()
        if receiver is not None:
            receiver.close()
        if voice_receiver is not None:
            voice_receiver.close()
        if self._image_scope_generation is not None:
            try:
                self._native_backend.clear_image_downloads(self._image_scope_generation)
            except (OSError, ValueError, RuntimeError):
                pass
        return {'capture_stopped': True, 'wechat_left_running': self.owner.wechat_running()}

    def _request_image(self, message, metadata):
        from .text import SYSTEM_PEERS
        if (self._image_scope_generation is None or
                message.session_id != self.session_id or
                (self.allowed and message.chat_id not in self.allowed) or
                message.chat_id == 'filehelper' or message.chat_id.startswith('gh_') or
                message.chat_id in SYSTEM_PEERS or not self._is_current()):
            return False
        return self._native_backend.request_image(message, metadata,
            self._image_scope_generation, self._is_current)

    def _is_current(self):
        with self.owner.lock:
            return bool(not self.closed and self.owner.active_lease is self and
                self.owner.backend is self._native_backend and self.owner.wechat_running() and
                self.owner.profile.get('logged_in') and self.owner.profile.get('wxid') == self.account)

    def fetch_image(self, request_id):
        with self._delivery_lock:
            receiver = self._media_receiver
        if receiver is None or not self._is_current():
            raise RuntimeError('当前微信连接没有可用的图片取图凭证')
        return receiver.fetch_image(request_id)

    @staticmethod
    def _media_key(message):
        return message.chat_id, message.data.get('native_message_id'), message.type

    def _emit_current(self, message):
        # 保存本 lease 的 emitter，不能在检查后再读取 owner 的新 emitter。
        if message.session_id == self.session_id and self._is_current() and self._emitter:
            message = self._prepare_media_delivery(message)
            self._emitter(message)

    def _prepare_media_delivery(self, message):
        """只有完整 ready 缓存交给普通插件，最近 1000 条媒体每条只交付一次。"""
        media = {3: 'image', 49: 'file', 34: 'voice'}.get(message.type)
        if media is None or not message.data.get('media_cache_only'):
            return message
        data = dict(message.data, monitor_only=True, dispatch_to_plugins=False)
        from .recall import message_id
        key = self._media_key(message)
        receiver = self._voice_receiver if message.type == 34 else self._media_receiver
        if (message.is_self or data.get('bot_generated') or not message_id(data.get('native_message_id')) or
                data.get('self_wxid') != self.account or
                (self.allowed and message.chat_id not in self.allowed) or
                media + '.receive' not in self.capabilities or receiver is None or
                not callable(getattr(receiver, 'owns_ready', None)) or not receiver.owns_ready(message)):
            return replace(message, data=data)
        with self._delivery_lock:
            if not self.closed and key not in self._delivered_media:
                self._delivered_media[key] = None
                while len(self._delivered_media) > 1000:
                    self._delivered_media.popitem(last=False)
                data.update(monitor_only=False, dispatch_to_plugins=True)
        return replace(message, data=data)

    def _media_ready(self, message):
        with self._delivery_lock:
            gate = self._pending_media.get(self._media_key(message))
        # emit 可能进入服务锁；等待和投递都不能持有 delivery/owner 锁。
        if gate is not None:
            while not gate.wait(.1):
                if not self._is_current():
                    return
        self._emit_current(message)

    def receive(self, message):
        if message.session_id != getattr(self._native_backend, 'session_id', message.session_id):
            return
        if not self._is_current():
            return
        message = replace(message, session_id=self.session_id)
        if message.type == 37:
            request = message.data.get('_linux_friend_request')
            if request is None:
                return
            from .friend_requests import FriendRequestError
            with self._delivery_lock:
                if self.closed or not self._is_current():
                    return
                try:
                    public = self._friend_requests.add(request)
                except (FriendRequestError, TypeError, RuntimeError):
                    return
                identifier = public['request_id']
                if identifier in self._friend_seen:
                    return
                self._friend_seen[identifier] = None
                while len(self._friend_seen) > 512:
                    self._friend_seen.popitem(last=False)
            self._emit_current(replace(message, text='', data={'self_wxid':self.account,
                'friend_request':public, 'dispatch_to_plugins':True}))
            return
        payload = message.data.get('_linux_voice') if message.type == 34 else message.data.get('_linux_media')
        if payload is None:
            self._emit_current(message)
            return
        gate, key = threading.Event(), self._media_key(message)
        with self._delivery_lock:
            if not self._is_current():
                return
            permitted = not self.allowed or message.chat_id in self.allowed
            home = getattr(self._native_backend, 'instance_home', None)
            self._pending_media[key] = gate
            try:
                if permitted and home is not None:
                    if message.type == 34:
                        if self._voice_receiver is None:
                            from .linux_voice_receiver import LinuxVoiceReceiver
                            from .linux_voice_database import LinuxVoiceDatabaseReader
                            from .linux_backend import LINUX_WECHAT_SHA256
                            source = LinuxVoiceDatabaseReader(self.pid, expected_account=self.account,
                                expected_sha256=LINUX_WECHAT_SHA256, instance_home=home)
                            self._voice_receiver = LinuxVoiceReceiver(account=self.account,
                                session_id=self.session_id, cache_root=self.owner.workspace / 'recall_voice',
                                is_current=self._is_current, allowed_targets=self.allowed,
                                callback=self._media_ready, source=source)
                        message = self._voice_receiver.submit(message)
                    elif self._media_receiver is None:
                        from .linux_media_receiver import LinuxMediaReceiver
                        self._media_receiver = LinuxMediaReceiver(account=self.account,
                            session_id=self.session_id, pid=self.pid,
                            storage_root=Path(home) / 'xwechat_files',
                            cache_root=self.owner.workspace / 'recall_media',
                            is_current=self._is_current, callback=self._media_ready,
                            request_image=self._request_image if self._image_scope_generation is not None else None)
                    if message.type != 34:
                        if message.type == 3 and message.chat_id in self._image_on_demand_chats:
                            message = self._media_receiver.submit(message, defer_image=True)
                        else:
                            message = self._media_receiver.submit(message)
            except (OSError, ValueError, RuntimeError):
                data = dict(message.data)
                media = 'voice' if message.type == 34 else 'image' if message.type == 3 else 'file'
                data.update({media + '_status': 'failed', media + '_error': '媒体缓存暂不可用'})
                data['media_cache_only'] = True
                message = replace(message, data=data)
            data = dict(message.data)
            data.pop('_linux_media', None)
            data.pop('_linux_voice', None)
            message = replace(message, data=data)
        try:
            self._emit_current(message)
        finally:
            gate.set()
            with self._delivery_lock:
                if self._pending_media.get(key) is gate:
                    self._pending_media.pop(key)

    def _send(self, method, chat_id, *args):
        with self.owner.lock:
            if self.closed or self.owner.active_lease is not self or self.last_error:
                raise RuntimeError('这条消息所属的机器人连接已停止')
            if self.allowed and chat_id not in self.allowed:
                raise ValueError('该会话不在收发白名单中')
            backend = self.owner.backend
        return getattr(backend, method)(chat_id, *args)

    def friend_requests(self):
        if not self._is_current():
            raise RuntimeError('好友申请所属的微信连接已结束')
        return self._friend_requests.list()

    def send_text(self, chat_id, text): return self._send('send_text', chat_id, text)
    def send_at_text(self, chat_id, text, at_list): return self._send('send_at_text', chat_id, text, at_list)
    def send_image(self, chat_id, path): return self._send('send_image', chat_id, path)
    def send_file(self, chat_id, path): return self._send('send_file', chat_id, path)
    def send_video(self, chat_id, path): return self._send('send_video', chat_id, path)
    def send_emoticon(self, chat_id, path): return self._send('send_emoticon', chat_id, path)
    def send_voice(self, chat_id, path): return self._send('send_voice', chat_id, path)
    def group_members(self, room):
        result = self._send('group_members', room)
        with self.owner.lock:
            if self.closed or self.owner.active_lease is not self or self.last_error:
                raise RuntimeError('群成员读取所属的微信连接已结束')
        return result
    def chats(self): return self.owner.backend.chats() if self.owner.backend else []

    def contacts(self):
        with self.owner.lock:
            if self.closed or self.owner.active_lease is not self or self.last_error:
                raise RuntimeError('联系人所属的微信连接已结束')
            backend = self.owner.backend
        result = backend.contacts()
        with self.owner.lock:
            if (self.closed or self.owner.active_lease is not self or
                    self.owner.backend is not backend or self.last_error or
                    result.get('account') != self.account):
                raise RuntimeError('联系人所属的微信连接已结束')
        return dict(result, session_id=self.session_id)

    def export_emoticons(self, *, convert=True):
        with self.owner.lock:
            if self.closed or self.owner.active_lease is not self or self.last_error:
                raise RuntimeError('表情导出所属的微信连接已结束')
            backend = self.owner.backend
        result = backend.export_emoticons(convert=convert, is_current=self._is_current)
        if not self._is_current():
            raise RuntimeError('表情导出所属的微信连接已结束')
        return result

    def contact_labels(self):
        with self.owner.lock:
            if self.closed or self.owner.active_lease is not self or self.last_error:
                raise RuntimeError('联系人所属的微信连接已结束')
            backend = self.owner.backend
        result = backend.contact_labels()
        with self.owner.lock:
            if (self.closed or self.owner.active_lease is not self or
                    self.owner.backend is not backend or self.last_error or
                    result.get('account') != self.account):
                raise RuntimeError('联系人所属的微信连接已结束')
        return dict(result, session_id=self.session_id)


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
        lease.receive(message)

    def lease(self, allowed, *, image_on_demand_chats=()):
        with self.lock:
            if not self.profile.get('logged_in') or not self.wechat_running():
                raise PanelError('请先登录微信并等待账号识别', 409)
            return SessionLease(self, allowed, image_on_demand_chats=image_on_demand_chats)

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
        if path in ('/api/config', '/api/plugin', '/api/plugin-source', '/api/plugin-business', '/api/plugin-reset',
                    '/api/scheduled-tasks', '/api/scheduled-tasks/delete', '/api/scheduled-tasks/toggle'):
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
                sender = getattr(backend, '_sender', None)
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
                cleanup = getattr(sender, 'cleanup_retained_files', None)
                if callable(cleanup) and process is not None and process.poll() is not None:
                    cleanup()
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
