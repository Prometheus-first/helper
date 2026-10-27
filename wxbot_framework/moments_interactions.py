"""会话绑定的朋友圈互动任务；本模块不实现微信调用。"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading

from .panel_common import PanelError

_REQUEST = re.compile(r'[A-Za-z0-9_-]{16,80}\Z')
_SESSION = re.compile(r'[A-Za-z0-9_-]{1,80}\Z')
_SNS = re.compile(r'[1-9][0-9]{0,19}\Z')
_MAX_JOBS = 1024
_UNKNOWN = '操作结果尚未确认，请到微信朋友圈核对；不会自动重发'
_FAILED = '提交前检查未通过，未执行朋友圈互动'


def _request_id(value):
    if not isinstance(value, str) or not _REQUEST.fullmatch(value):
        raise PanelError('互动请求编号应为 16–80 位字母、数字、下划线或短横线')
    return value


def _validate(data):
    if not isinstance(data, dict) or set(data) - {'request_id', 'session_id', 'sns_id', 'action', 'content'}:
        raise PanelError('朋友圈互动请求格式无效')
    request_id = _request_id(data.get('request_id'))
    session = data.get('session_id')
    if not isinstance(session, str) or not _SESSION.fullmatch(session):
        raise PanelError('微信连接已失效，请重新选择账号', 409)
    sns_id = data.get('sns_id')
    if not isinstance(sns_id, str) or not _SNS.fullmatch(sns_id) or int(sns_id) > 0xffffffffffffffff:
        raise PanelError('朋友圈 ID 必须是有效的无符号十进制数字字符串')
    action, content = data.get('action'), data.get('content', '')
    if action not in ('like', 'comment') or not isinstance(content, str) or '\0' in content:
        raise PanelError('朋友圈互动内容或操作无效')
    if action == 'like' and content:
        raise PanelError('点赞不能携带评论内容')
    try:
        encoded = content.encode('utf-8')
    except UnicodeEncodeError:
        raise PanelError('评论含无效的 Unicode 字符') from None
    if action == 'comment' and (not content.strip() or len(encoded) > 1023):
        raise PanelError('评论不能为空，且最多 1023 个 UTF-8 字节')
    payload = {'request_id': request_id, 'session_id': session, 'sns_id': sns_id,
               'action': action, 'content': content}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                       separators=(',', ':')).encode('utf-8')).hexdigest()
    return payload, digest


class MomentsInteractions:
    """每个机器人实例最多一个执行中的任务；同一编号不重复投递。

    原生发送需自行检查 session。超时结果保持未知；后台调用未退出前仍占用账号。
    编号保存在本进程内，容量耗尽时拒绝新任务，不通过删除旧编号破坏幂等性。
    """
    def __init__(self, deliver, connection, allowed, *, timeout=120):
        if not all(callable(f) for f in (deliver, connection, allowed)):
            raise TypeError('互动回调必须可调用')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout 必须是正有限数')
        self._deliver, self._connection, self._allowed, self._timeout = deliver, connection, allowed, timeout
        self._lock = threading.RLock()
        self._jobs, self._active, self._closed = {}, None, False

    @staticmethod
    def _view(job):
        return {key: job[key] for key in ('request_id', 'status', 'error', 'outcome')}

    def status(self, identifier):
        _request_id(identifier)
        with self._lock:
            job = self._jobs.get(identifier)
            if job is None:
                raise PanelError('互动记录已失效，请到微信核对结果', 404)
            return self._view(job)

    def _current(self, payload):
        try:
            value = self._connection()
            return isinstance(value, dict) and value.get('session_id') == payload['session_id']
        except Exception:
            return False

    def submit(self, data):
        payload, digest = _validate(data)
        identifier = payload['request_id']
        with self._lock:
            previous = self._jobs.get(identifier)
            if previous is not None:
                if previous['fingerprint'] != digest:
                    raise PanelError('互动编号已用于其他请求，请刷新后重新操作', 409)
                return self._view(previous)
            if self._closed or self._active is not None:
                raise PanelError('上一项朋友圈互动尚未处理完毕', 409)
            if len(self._jobs) >= _MAX_JOBS:
                raise PanelError('互动记录已满，请重新连接管理服务后核对结果', 409)
            if not self._current(payload) or not self._allowed(payload['session_id'], payload['sns_id']):
                raise PanelError('请在当前微信连接中先读取该朋友圈', 409)
            job = {'request_id': identifier, 'fingerprint': digest, 'status': 'pending',
                   'error': '', 'outcome': '', 'submitted': False, 'finished': False}
            self._jobs[identifier], self._active = job, identifier
            timer = threading.Timer(self._timeout, self._expire, args=(identifier,))
            timer.daemon = True
            worker = threading.Thread(target=self._run, args=(job, payload, timer),
                                      name='MomentsInteraction', daemon=True)
            try:
                worker.start()
            except Exception:
                job.update(status='failed', error=_FAILED, finished=True)
                self._active = None
            return self._view(job)

    def _expire(self, identifier):
        with self._lock:
            job = self._jobs.get(identifier)
            if job is not None and not job['finished'] and job['status'] == 'pending':
                job.update(status='unknown' if job['submitted'] else 'failed',
                           error=_UNKNOWN if job['submitted'] else _FAILED)

    def _run(self, job, payload, timer):
        status, error, outcome, blocked = 'failed', _FAILED, '', False
        try:
            timer.start()
            with self._lock:
                if (self._closed or job['status'] != 'pending' or not self._current(payload) or
                        not self._allowed(payload['session_id'], payload['sns_id'])):
                    return
                job['submitted'] = True
            status, error = 'unknown', _UNKNOWN
            result = self._deliver(payload)
            if isinstance(result, dict):
                blocked = result.get('unload_blocked') is True
                if not blocked and result.get('status') == 'failed' and result.get('phase') == 'preflight':
                    status, error = 'failed', _FAILED
                elif (not blocked and self._current(payload) and
                      (result.get('status') == 'completed' or
                       (payload['action'] == 'like' and result.get('status') == 'already_liked'))):
                    status, error, outcome = 'completed', '', result['status']
        except Exception:
            if job['submitted']:
                status, error, blocked = 'unknown', _UNKNOWN, True
        finally:
            timer.cancel()
            with self._lock:
                if job['status'] == 'pending':
                    job.update(status=status, error=error, outcome=outcome)
                job['finished'] = not blocked
                if not blocked and self._active == job['request_id']:
                    self._active = None

    def close(self):
        with self._lock:
            self._closed = True
            if self._active is not None:
                job = self._jobs[self._active]
                if job['status'] == 'pending':
                    job.update(status='unknown' if job['submitted'] else 'failed',
                               error=_UNKNOWN if job['submitted'] else _FAILED)
