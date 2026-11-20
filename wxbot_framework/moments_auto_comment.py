"""Account-bound fixed comments for newly discovered homepage posts."""
from copy import deepcopy
import hashlib
import math
import threading
import time
import uuid

from .account import validate_account
from .panel_common import PanelError

DEFAULT = {'enabled': False, 'content': '', 'interval_seconds': 120, 'authors': []}


def validate(data):
    if not isinstance(data, dict) or set(data) - {*DEFAULT, 'session_id'}:
        raise PanelError('自动评论配置格式无效')
    result = {key: deepcopy(data.get(key, value)) for key, value in DEFAULT.items()}
    if type(result['enabled']) is not bool:
        raise PanelError('自动评论开关必须是布尔值')
    content = result['content']
    try:
        valid = isinstance(content, str) and '\0' not in content and len(content.encode('utf-8')) <= 1023
    except UnicodeError:
        valid = False
    if not valid or result['enabled'] and not content.strip():
        raise PanelError('请填写固定评论，最多 1023 个 UTF-8 字节')
    interval = result['interval_seconds']
    if type(interval) is not int or not 60 <= interval <= 86400:
        raise PanelError('检查间隔应为 60–86400 秒的整数')
    authors = result['authors']
    if not isinstance(authors, list) or len(authors) > 100:
        raise PanelError('指定作者最多 100 个微信 ID')
    try:
        for author in authors:
            validate_account(author)
    except (ValueError, TypeError):
        raise PanelError('指定作者包含无效的微信 ID') from None
    result['authors'] = list(dict.fromkeys(authors))
    return result


class MomentsAutoComment:
    def __init__(self, controller, write_json, read_json, *, clock=time.time):
        self.controller, self.write, self.read, self.clock = controller, write_json, read_json, clock
        self.directory = controller.workspace / '.runtime/moments-auto'
        self.path = self.directory / 'settings.json'
        self.lock, self.scan_lock = threading.RLock(), threading.Lock()
        self.closed = threading.Event()
        self.thread = None
        self.generation = 0
        self.identity = None
        self.since = self.next_at = self.last_scan = 0
        self.phase, self.error = 'disabled', ''
        self.completed = 0

    def _settings(self):
        if not self.path.exists():
            return dict(deepcopy(DEFAULT), account='')
        stored = self.read(self.path)
        result = validate({key: stored[key] for key in DEFAULT if key in stored})
        account = stored.get('account', '')
        if account:
            validate_account(account)
        return dict(result, account=account)

    def view(self):
        with self.lock:
            settings = self._settings()
            return {**{key: settings[key] for key in DEFAULT}, 'phase': self.phase,
                    'error': self.error, 'completed': self.completed,
                    'last_scan_at': self.last_scan, 'next_scan_at': self.next_at}

    def configure(self, data):
        settings = validate(data)
        current = self.controller._moments_session()
        if settings['enabled']:
            if current is None or data.get('session_id') != current['session_id']:
                raise PanelError('微信连接已变化，请重新选择账号', 409)
            capability = self.controller.moments_connection()
            if not capability.get('comment_supported', capability.get('interaction_supported', False)):
                raise PanelError('当前微信连接尚未支持评论', 409)
        with self.lock:
            if self.closed.is_set():
                raise PanelError('管理服务正在退出', 409)
            account = current['account'] if current else self._settings()['account']
            self.directory.mkdir(parents=True, exist_ok=True)
            self.write(self.path, dict(settings, account=account))
            self.generation += 1
            self.identity = None
            self.since, self.next_at = self.clock(), 0
            self.phase = 'waiting' if settings['enabled'] else 'disabled'
            self.error = ''
        if settings['enabled']:
            self.start()
        return self.view()

    def start(self):
        with self.lock:
            try:
                enabled = self._settings()['enabled']
            except Exception:
                self.phase, self.error = 'error', '自动评论配置无法读取，请检查本地配置。'
                return
            if self.thread is None and not self.closed.is_set() and enabled:
                self.thread = threading.Thread(target=self._loop, name='MomentsAutoComment', daemon=True)
                self.thread.start()

    def _loop(self):
        while not self.closed.is_set():
            self.run_once()
            self.closed.wait(1)

    def _valid(self, generation, current):
        # Do not acquire the controller lock while holding our settings lock.
        connection = self.controller._moments_session()
        with self.lock:
            return (not self.closed.is_set() and self.generation == generation and
                    self._settings()['enabled'] and connection == current)

    def _history(self, account):
        path = self.directory / ('seen-' + hashlib.sha256(account.encode()).hexdigest() + '.json')
        value = self.read(path) if path.exists() else {'posts': {}}
        posts = value.get('posts')
        if not isinstance(posts, dict) or len(posts) > 20000:
            raise PanelError('自动评论去重记录无效或已满，请检查本地记录', 409)
        for identifier, row in posts.items():
            if (not isinstance(identifier, str) or not identifier.isdecimal() or
                    not isinstance(row, dict) or type(row.get('time')) not in (int, float) or
                    not math.isfinite(row['time']) or row.get('status') not in
                    ('seen', 'pending', 'claimed', 'completed', 'failed', 'unknown')):
                raise PanelError('自动评论去重记录损坏，已停止发送', 409)
        cutoff = self.clock() - 7 * 86400
        return path, {key: row for key, row in posts.items() if row['time'] >= cutoff}

    def _pause_unknown(self, generation):
        with self.lock:
            if generation != self.generation:
                return
            settings = self._settings()
            settings['enabled'] = False
            self.write(self.path, settings)
            self.generation += 1
            self.phase, self.error = 'paused', '评论结果未确认，已暂停；请在微信核对后再启用。'

    def run_once(self):
        if not self.scan_lock.acquire(blocking=False):
            return
        try:
            with self.lock:
                settings, generation = self._settings(), self.generation
                if self.closed.is_set() or not settings['enabled'] or self.clock() < self.next_at:
                    return
            current = self.controller._moments_session()
            if current is None or current['account'] != settings['account']:
                with self.lock:
                    self.phase = 'waiting_connection'
                return
            capability = self.controller.moments_connection()
            if not capability.get('comment_supported', capability.get('interaction_supported', False)):
                raise PanelError('当前微信连接尚未支持评论', 409)
            with self.lock:
                if generation != self.generation:
                    return
                self.phase = 'reading'
            result = self.controller.read_moments({'session_id': current['session_id'], 'limit': 20})
            if not self._valid(generation, current):
                return
            now = self.clock()
            rows = result['rows']
            path, posts = self._history(current['account'])
            identity = (generation, current['account'], current['session_id'])
            with self.lock:
                baseline = self.identity != identity
                if baseline:
                    self.identity, self.since = identity, int(now)
            candidates = []
            for row in rows:
                identifier = row['sns_id']
                own_comment = any(entry.get('username') == current['account'] for entry in row.get('comments', []))
                eligible = (not row['is_self'] and not row['private'] and not own_comment and
                            row['timestamp'] >= self.since and 0 <= now-row['timestamp'] <= 3600 and
                            (not settings['authors'] or row['author'] in settings['authors']))
                if identifier not in posts:
                    posts[identifier] = {'time': now, 'status': 'pending' if eligible and not baseline else 'seen'}
                if eligible and not baseline and posts[identifier]['status'] == 'pending':
                    candidates.append(row)
            if len(posts) > 20000:
                raise PanelError('自动评论去重记录已满，已停止发送', 409)
            self.directory.mkdir(parents=True, exist_ok=True)
            self.write(path, {'posts': posts})
            # Work oldest-first within the current homepage; at most three per pass.
            for row in sorted(candidates, key=lambda entry: entry['timestamp'])[:3]:
                if not self._valid(generation, current):
                    return
                record = posts[row['sns_id']]
                record['status'] = 'claimed'
                self.write(path, {'posts': posts})  # durable claim precedes any external submission
                if not self._valid(generation, current):
                    return
                payload = {'request_id': uuid.uuid4().hex, 'session_id': current['session_id'],
                           'sns_id': row['sns_id'], 'action': 'comment', 'content': settings['content']}
                with self.lock:
                    if self.generation == generation:
                        self.phase = 'commenting'
                try:
                    job = self.controller.interact_moments(payload, _guard=lambda: self._valid(generation, current))
                    while job['status'] == 'pending':
                        if not self._valid(generation, current) or self.closed.wait(.2):
                            break
                        job = self.controller.moments_interaction_status(payload['request_id'])
                    status = job['status'] if job['status'] in ('completed', 'failed') else 'unknown'
                except Exception:
                    status = 'unknown'
                record['status'] = status
                self.write(path, {'posts': posts})
                if status == 'unknown':
                    self._pause_unknown(generation)
                    return
                if status == 'completed':
                    with self.lock:
                        self.completed += 1
            with self.lock:
                if self.generation == generation:
                    self.phase, self.error = ('baseline' if baseline else 'watching'), ''
                    self.last_scan, self.next_at = now, self.clock()+settings['interval_seconds']
        except Exception:
            with self.lock:
                self.phase, self.error = 'error', '自动评论检查失败，已保留去重记录，请检查微信连接及本地记录。'
                self.next_at = self.clock()+60
        finally:
            self.scan_lock.release()

    def close(self):
        self.closed.set()
        if self.thread is not None:
            self.thread.join(3)
