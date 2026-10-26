"""朋友圈发布任务：离线校验、会话绑定和保守的异步结果管理。

本模块不包含微信调用；deliver 必须自行核对 expected session，并且不能自动重试。
仅收到带 sns_id 的 published 回执才确认发布，未知结果不能据此重新提交。
"""
from __future__ import annotations

import base64
import binascii
from collections import OrderedDict
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import re
import threading
import uuid
import warnings

from PIL import Image

from .panel_common import PanelError

MAX_IMAGES = 9
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 40_960_000
MAX_FINISHED_JOBS = 64
_REQUEST_ID = re.compile(r'[A-Za-z0-9_-]{16,80}\Z')
_SESSION_ID = re.compile(r'[A-Za-z0-9_-]{1,80}\Z')
_SNS_ID = re.compile(r'[A-Za-z0-9_-]{1,160}\Z')
_DATA_URL = re.compile(r'data:image/(png|jpeg);base64,([A-Za-z0-9+/]*={0,2})\Z')
_UUID_NAME = re.compile(r'[a-f0-9]{32}\Z')
_UNKNOWN = '发布结果尚未确认，请到微信朋友圈核对；本任务不会自动重发'
_RETAINED = '原生发布任务仍未确认退出，请到微信朋友圈核对并重新连接管理服务；当前账号暂不能继续发布'
_FAILED = '发布前检查未通过，未提交到微信，请检查连接后重新操作'


def _request_id(value):
    if not isinstance(value, str) or not _REQUEST_ID.fullmatch(value):
        raise PanelError('发布请求编号应为 16–80 位字母、数字、下划线或短横线')
    return value


def _validate(data):
    if not isinstance(data, dict):
        raise PanelError('朋友圈发布请求必须为对象')
    if set(data) - {'request_id', 'session_id', 'content', 'images', 'visibility'}:
        raise PanelError('朋友圈发布请求含不支持的字段')
    identifier = _request_id(data.get('request_id'))
    session = data.get('session_id')
    if not isinstance(session, str) or not _SESSION_ID.fullmatch(session):
        raise PanelError('微信连接已失效，请刷新发布账号', 409)
    content = data.get('content', '')
    if not isinstance(content, str) or '\0' in content or len(content) > 1000:
        raise PanelError('朋友圈文字不能含 NUL，且最多 1000 个字符（本框架限制）')
    try:
        encoded_content = content.encode('utf-8')
    except UnicodeEncodeError:
        raise PanelError('朋友圈文字含无效的 Unicode 字符') from None
    if len(encoded_content) > 4000:
        raise PanelError('朋友圈文字最多 4000 个 UTF-8 字节（本框架限制）')
    visibility = data.get('visibility', 'public')
    if visibility not in ('public', 'private'):
        raise PanelError('可见范围仅支持公开或仅自己可见')
    rows = data.get('images', [])
    if not isinstance(rows, list) or len(rows) > MAX_IMAGES:
        raise PanelError('朋友圈最多可选择 9 张图片')
    images, total = [], 0
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'name', 'data_url'}:
            raise PanelError('图片必须提供 name 和 data_url，不支持网址或磁盘路径')
        name, data_url = row.get('name'), row.get('data_url')
        if (not isinstance(name, str) or not name.strip() or len(name) > 255 or
                any(ord(ch) < 32 or ch in '/\\' for ch in name)):
            raise PanelError('图片名称无效')
        try:
            name.encode('utf-8')
        except UnicodeEncodeError:
            raise PanelError('图片名称含无效的 Unicode 字符') from None
        # 先限制编码长度，避免在解码前复制过大的输入。
        if not isinstance(data_url, str) or len(data_url) > 4 * ((MAX_IMAGE_BYTES + 2) // 3) + 32:
            raise PanelError('每张图片最多 5 MiB')
        match = _DATA_URL.fullmatch(data_url)
        if match is None:
            raise PanelError('图片必须为 PNG/JPEG 的 base64 data_url')
        try:
            raw = base64.b64decode(match[2], validate=True)
        except (binascii.Error, ValueError):
            raise PanelError('图片 base64 数据无效') from None
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            raise PanelError('每张图片应为非空且不超过 5 MiB')
        total += len(raw)
        if total > MAX_TOTAL_IMAGE_BYTES:
            raise PanelError('所有图片合计最多 20 MiB')
        expected_format = {'png': 'PNG', 'jpeg': 'JPEG'}[match[1]]
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(BytesIO(raw)) as picture:
                    width, height = picture.size
                    if (picture.format != expected_format or width < 1 or height < 1 or
                            width * height > MAX_PIXELS or getattr(picture, 'n_frames', 1) != 1 or
                            getattr(picture, 'is_animated', False)):
                        raise ValueError('unsupported image')
                    picture.verify()
                # verify() 不解码像素；再完整读取一次，拒绝截断或损坏的像素数据。
                with Image.open(BytesIO(raw)) as picture:
                    picture.load()
        except Exception:
            raise PanelError('图片必须为完整的静态 PNG/JPEG，且不超过 4096 万像素') from None
        images.append({'name': name, 'bytes': raw, 'extension': '.png' if expected_format == 'PNG' else '.jpg'})
    if not content.strip() and not images:
        raise PanelError('请填写文字或至少选择一张图片')
    # 不保留正文/图片至任务历史；散列仍覆盖图片顺序和展示名称。
    fingerprint = hashlib.sha256(json.dumps({
        'session_id': session, 'content': content, 'visibility': visibility,
        'images': [(row['name'], hashlib.sha256(row['bytes']).hexdigest()) for row in images],
    }, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    return identifier, fingerprint, {'request_id': identifier, 'session_id': session,
                                    'content': content, 'visibility': visibility}, images


def _reparse(path):
    """Windows junction 和符号链接都不能作为上传临时目录。"""
    return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)


class MomentsPublisher:
    """每个账号 workspace 使用一个实例。

    connection_callable() 返回当前就绪连接的 {'session_id': str}，未就绪返回 None 或抛错。
    deliver_callable(payload) 接收 request_id/session_id/content/visibility，以及 images 路径列表。
    明确的提交前失败返回 {'status': 'failed', 'phase': 'preflight'}；其他失败均为 unknown。
    deliver 是唯一原生调用边界，必须原子核对 payload['session_id']，且禁止内部自动重试。
    """

    def __init__(self, workspace, deliver_callable, connection_callable, *, timeout=120):
        if not callable(deliver_callable) or not callable(connection_callable):
            raise TypeError('deliver 和 connection 必须可调用')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout 必须为正有限秒数')
        self.workspace = Path(workspace).resolve()
        self._deliver, self._connection, self._timeout = deliver_callable, connection_callable, timeout
        self._lock = threading.RLock()
        self._jobs = OrderedDict()
        self._active = None
        self._closed = False

    @staticmethod
    def _view(job):
        return {key: job[key] for key in ('request_id', 'status', 'error', 'sns_id')}

    def status(self, identifier):
        _request_id(identifier)
        with self._lock:
            job = self._jobs.get(identifier)
            if job is None:
                raise PanelError('发布记录已失效，请到微信朋友圈核对结果', 404)
            return self._view(job)

    def _current(self, session):
        try:
            current = self._connection()
            return isinstance(current, dict) and current.get('session_id') == session
        except Exception:
            return False

    def submit(self, data):
        identifier, fingerprint, payload, images = _validate(data)
        with self._lock:
            previous = self._jobs.get(identifier)
            if previous is not None:
                if previous['fingerprint'] != fingerprint:
                    raise PanelError('发布编号已用于其他内容，请刷新后重新操作', 409)
                return self._view(previous)
            if self._closed:
                raise PanelError('发布服务已关闭，请重新连接机器人', 409)
            if self._active is not None:
                raise PanelError('上一条朋友圈仍在处理，请等待结果并到微信核对', 409)
            if not self._current(payload['session_id']):
                raise PanelError('微信连接已变化，请刷新发布账号', 409)
            job = {'request_id': identifier, 'fingerprint': fingerprint, 'status': 'pending',
                   'error': '', 'sns_id': '', 'submitted': False, 'finished': False}
            self._jobs[identifier] = job
            self._active = identifier
            timer = threading.Timer(self._timeout, self._expire, args=(identifier,))
            timer.daemon = True
            worker = threading.Thread(target=self._run, args=(job, payload, images, timer),
                                      name='MomentsPublish', daemon=True)
            try:
                worker.start()
            except Exception:
                job.update(status='failed', error=_FAILED, finished=True)
                self._active = None
                self._prune()
            return self._view(job)

    def _expire(self, identifier):
        with self._lock:
            job = self._jobs.get(identifier)
            if job is not None and not job['finished'] and job['status'] == 'pending':
                job.update(status='unknown' if job['submitted'] else 'failed',
                           error=_UNKNOWN if job['submitted'] else _FAILED)
            # 不能释放 active 或清图片：原生函数可能仍在读取图片/等待微信。

    def _base(self, *, create=True):
        if not self.workspace.is_dir() or self.workspace.resolve() != self.workspace:
            raise ValueError('workspace changed')
        path = self.workspace
        for part in ('.runtime', 'moments'):
            path = path / part
            if create:
                path.mkdir(exist_ok=True)
            if _reparse(path) or path.resolve() != path or not path.is_dir():
                raise ValueError('unsafe temporary directory')
        return path

    def _stage(self, images):
        base = self._base()
        directory = base / uuid.uuid4().hex
        directory.mkdir()
        paths = []
        try:
            for row in images:
                if self._base() != base or _reparse(directory) or directory.resolve() != directory:
                    raise ValueError('temporary directory changed')
                path = directory / (uuid.uuid4().hex + row['extension'])
                # 文件名和路径全部由本模块生成，展示名称不参与任何磁盘操作。
                with path.open('xb') as output:
                    paths.append(path)
                    output.write(row['bytes'])
            return directory, paths
        except Exception:
            self._cleanup(directory, paths)
            raise

    def _cleanup(self, directory, paths):
        if directory is None:
            return
        try:
            base = self.workspace / '.runtime' / 'moments'
            if (not _UUID_NAME.fullmatch(directory.name) or directory.parent != base or
                    self._base(create=False) != base or _reparse(directory) or directory.resolve() != directory):
                return
            for path in paths:
                if (path.parent != directory or not re.fullmatch(r'[a-f0-9]{32}\.(?:png|jpg)', path.name) or
                        self._base(create=False) != base or _reparse(directory) or directory.resolve() != directory):
                    return
                path.unlink(missing_ok=True)
            # 不递归删除；目录若被放入其他文件便保留，绝不清理用户来源文件。
            directory.rmdir()
        except OSError:
            pass
        except ValueError:
            pass

    def _run(self, job, payload, images, timer):
        directory, paths = None, []
        status, error, sns_id = 'failed', _FAILED, ''
        retain_images = False
        try:
            timer.start()
            if not self._current(payload['session_id']):
                return
            if images:
                directory, paths = self._stage(images)
            with self._lock:
                if self._closed or job['status'] != 'pending':
                    return
                # 暂存可能耗时，在真正提交前再次验证同一会话。
                if not self._current(payload['session_id']):
                    return
                job['submitted'] = True
            status, error = 'unknown', _UNKNOWN
            result = self._deliver(dict(payload, images=[str(path) for path in paths]))
            if isinstance(result, dict):
                retain_images = result.get('retain_images') is True or result.get('unload_blocked') is True
                if retain_images:
                    status, error = 'unknown', _RETAINED
                elif result.get('status') == 'failed' and result.get('phase') == 'preflight':
                    status, error = 'failed', _FAILED
                elif (result.get('status') == 'published' and isinstance(result.get('sns_id'), str) and
                      _SNS_ID.fullmatch(result['sns_id']) and
                      not result['sns_id'].startswith(('sk-', 'ghp_', 'gho_', 'ghu_', 'ghs_', 'ghr_', 'github_pat_')) and
                      self._current(payload['session_id'])):
                    status, error, sns_id = 'published', '', result['sns_id']
        except Exception:
            # 不回显正文、磁盘路径、密钥或来自原生层的异常文本。
            if job['submitted']:
                status, error = 'unknown', _UNKNOWN
        finally:
            timer.cancel()
            if not retain_images:
                self._cleanup(directory, paths)
            with self._lock:
                # 超时/关闭后的未知结果保持未知；迟到回执不改变用户已看到的结论。
                if retain_images:
                    job.update(status='unknown', error=_RETAINED, sns_id='', retained_images=True)
                elif job['status'] == 'pending':
                    job.update(status=status, error=error, sns_id=sns_id)
                job['finished'] = not retain_images
                if not retain_images and self._active == job['request_id']:
                    self._active = None
                self._prune()

    def _prune(self):
        finished = [identifier for identifier, job in self._jobs.items() if job['finished']]
        for identifier in finished[:-MAX_FINISHED_JOBS]:
            del self._jobs[identifier]

    def close(self):
        """停止接收新请求；原生调用结束前继续持有图片及该账号的忙碌占位。"""
        with self._lock:
            self._closed = True
            if self._active is not None:
                job = self._jobs[self._active]
                if job['status'] == 'pending':
                    job.update(status='unknown' if job['submitted'] else 'failed',
                               error=_UNKNOWN if job['submitted'] else _FAILED)
