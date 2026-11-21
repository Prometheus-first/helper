"""Account-scoped photo library and non-blocking call-video controls."""
import base64
from io import BytesIO
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading
import uuid
import warnings

from PIL import Image, ImageOps
from .panel_common import PanelError, redact

logger = logging.getLogger('CallVideo')

PHOTO_ID = re.compile(r'[a-f0-9]{32}\Z')
MAX_UPLOAD = 8 * 1024 * 1024


class CallVideoPanel:
    def __init__(self, controller):
        self.controller = controller
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.thread = None
        self.enabled = False
        self.generation = 0
        self.selection = 0
        self.session = ''
        self.account = ''
        self.photo = ''
        self.pending = None
        self.playing = None
        from .call_video_media import VideoUploads
        self.video_uploads = VideoUploads(self)
        self.phase, self.error, self.stats = 'stopped', '', {}
        self.settings_path = controller.workspace / '.runtime/call-video/settings.json'

    def _connection(self):
        with self.controller.lock:
            app = self.controller.app
            backend = getattr(app, 'backend', None)
            connected = bool(app and app._connection_ready and not app.stop_event.is_set())
            account = self.controller.store.load().get('wechat_account') or ''
            return {'account': account, 'connected': connected,
                    'supported': connected and 'call_video.replace' in getattr(backend, 'capabilities', ()),
                    'session_id': getattr(backend, 'session_id', '') if connected else ''}

    def _directory(self, account):
        if not account:
            raise PanelError('请先选择微信账号', 409)
        path = self.controller.workspace
        for part in ('.runtime', 'call-video', hashlib.sha256(account.encode()).hexdigest()[:24]):
            path = path / part
            path.mkdir(exist_ok=True)
            if path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
                raise PanelError('换脸照片目录不能为链接')
        return path

    def _photos(self, account):
        if not account:
            return []
        folder = self._directory(account)
        result = []
        for path in sorted(folder.glob('*.json')):
            if not PHOTO_ID.fullmatch(path.stem) or path.is_symlink():
                continue
            row = json.loads(path.read_text(encoding='utf8'))
            thumbnail = folder / (path.stem + '.thumb.jpg')
            if thumbnail.is_symlink():
                continue
            result.append({'id': path.stem, 'name': str(row.get('name', '照片'))[:120],
                'kind': row.get('kind', 'image'), 'duration': row.get('duration'),
                'thumbnail': 'data:image/jpeg;base64,' + base64.b64encode(thumbnail.read_bytes()).decode()})
        return result

    def _photo_path(self, account, identifier):
        return self._asset_path(account, identifier, 'image')

    def _asset_path(self, account, identifier, kind):
        if not isinstance(identifier, str) or not PHOTO_ID.fullmatch(identifier):
            raise PanelError('照片编号无效')
        folder = self._directory(account)
        metadata = folder / (identifier + '.json')
        if not metadata.is_file() or metadata.is_symlink() or json.loads(metadata.read_text(encoding='utf8')).get('kind', 'image') != kind:
            raise PanelError('素材类型不符或不存在', 404)
        path = folder / (identifier + ('.jpg' if kind == 'image' else '.video'))
        if not path.is_file() or path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
            raise PanelError('照片不存在，请重新选择', 404)
        return path

    def settings(self):
        if self.settings_path.exists():
            return json.loads(self.settings_path.read_text(encoding='utf8'))
        base = Path(sys.executable).parent.parent
        return {'python_executable': os.environ.get('WXBOT_FACEFUSION_PYTHON', str(base / 'facefusion-env/python.exe')),
                'repository': os.environ.get('WXBOT_FACEFUSION_REPOSITORY', str(base / 'facefusion')),
                'mode': 'balanced'}

    def _save_settings(self, value):
        if value.get('mode') not in ('balanced', 'fast'):
            raise PanelError('请选择均衡或流畅模式')
        for key in ('python_executable', 'repository'):
            if not isinstance(value.get(key), str) or len(value[key]) > 2048 or '\0' in value[key]:
                raise PanelError('换脸环境路径无效')
        if not Path(value['python_executable']).is_file() or not (Path(value['repository']) / 'facefusion.py').is_file():
            raise PanelError('未找到 FaceFusion 环境，请展开运行环境设置核对路径')
        from .web_panel import atomic_json
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(self.settings_path, {key: value[key] for key in ('python_executable', 'repository', 'mode')})

    def upload(self, data):
        account = self._connection()['account']
        name, encoded = data.get('name'), data.get('data_url')
        if not isinstance(name, str) or not name.strip() or len(name) > 120 or any(ord(c) < 32 or c in '/\\' for c in name):
            raise PanelError('照片名称无效')
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_UPLOAD + 2) // 3) + 64:
            raise PanelError('照片不能超过 8 MiB')
        match = re.fullmatch(r'data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/]*={0,2})', encoded)
        if not match:
            raise PanelError('请选择 PNG、JPEG 或 WebP 照片')
        try:
            raw = base64.b64decode(match[2], validate=True)
            if not raw or len(raw) > MAX_UPLOAD:
                raise ValueError()
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(BytesIO(raw)) as original:
                    if (original.format != {'png': 'PNG', 'jpeg': 'JPEG', 'webp': 'WEBP'}[match[1]] or
                            original.width * original.height > 20_000_000 or getattr(original, 'n_frames', 1) != 1):
                        raise ValueError()
                    picture = ImageOps.exif_transpose(original).convert('RGB')
                    picture.load()
        except Exception:
            raise PanelError('照片必须为完整的静态图片，且不超过 2000 万像素') from None
        normalized = BytesIO()
        picture.save(normalized, format='JPEG', quality=95)
        normalized = normalized.getvalue()
        digest = hashlib.sha256(normalized).hexdigest()
        with self.lock:
            folder = self._directory(account)
            for row in self._photos(account):
                if row['kind'] != 'image':
                    continue
                existing = self._photo_path(account, row['id'])
                if hashlib.sha256(existing.read_bytes()).hexdigest() == digest:
                    return {'photo_id': row['id']}
            if len(self._photos(account)) >= 24:
                raise PanelError('当前账号最多保存 24 个素材，请先移除不用的素材')
            identifier = uuid.uuid4().hex
            (folder / (identifier + '.jpg')).write_bytes(normalized)
            picture.thumbnail((180, 180))
            picture.save(folder / (identifier + '.thumb.jpg'), quality=80)
            (folder / (identifier + '.json')).write_text(json.dumps({'name': name}, ensure_ascii=False), encoding='utf8')
        return {'photo_id': identifier}

    def delete(self, data):
        account = self._connection()['account']
        identifier = data.get('photo_id')
        with self.lock:
            if self.enabled and (identifier == self.photo or self.pending and identifier == self.pending[1]):
                raise PanelError('请先切换到其他照片或停止换脸', 409)
            if self.enabled and self.playing and identifier == self.playing[1]:
                raise PanelError('请先结束视频播放或停止换脸', 409)
            row = next((row for row in self._photos(account) if row['id'] == identifier), None)
            if row is None:
                raise PanelError('素材不存在', 404)
            path = self._asset_path(account, identifier, row['kind'])
            for item in (path, path.with_suffix('.thumb.jpg'), path.with_suffix('.json')):
                item.unlink(missing_ok=True)
        return {'ok': True}

    def upload_video(self, data):
        return self.video_uploads.request(data)

    def play(self, data):
        with self.lock:
            session, generation = self.session, self.generation
            if (not self.enabled or self.phase != 'streaming' or data.get('session_id') != session or
                    self._connection()['session_id'] != session or self.pending):
                raise PanelError('请先开始换脸，模型就绪后再点击视频', 409)
            path = self._asset_path(self.account, data.get('video_id'), 'video')
            self.selection += 1
            selection = self.selection
            previous = self.playing
            self.playing = ('checking', data['video_id'])
        def start_video(service):
            with self.lock:
                if not self.enabled or self.generation != generation or self.selection != selection:
                    raise PanelError('换脸状态已变化', 409)
                return service.play_call_video_clip(path, expected_session=session)
        try:
            result = self._invoke(start_video, session)
        except Exception:
            with self.lock:
                if self.generation == generation and self.selection == selection:
                    self.playing = previous
            raise
        with self.lock:
            if self.generation == generation and self.selection == selection:
                self.playing = (result['request_id'], data['video_id'])
                self.error = ''
        return {'ok': True}

    def _invoke(self, callback, session):
        def execute(app):
            if (not app._connection_ready or app.backend is None or app.backend.session_id != session or
                    'call_video.replace' not in app.backend.capabilities):
                raise PanelError('微信连接已变化或当前版本不支持视频换脸', 409)
            return callback(app.service)
        return self.controller._call(execute)

    def start(self, data):
        if self.closed.is_set():
            raise PanelError('管理服务正在退出', 409)
        connection = self._connection()
        session = data.get('session_id')
        if not session or session != connection['session_id'] or not connection['supported']:
            raise PanelError('请先启动并连接支持视频换脸的 Windows 微信账号', 409)
        self._photo_path(connection['account'], data.get('photo_id'))
        with self.lock:
            if self.enabled:
                raise PanelError('换脸已开启，请使用切换照片', 409)
            self._save_settings(data.get('settings', self.settings()))
            self.session, self.account, self.photo = session, connection['account'], data['photo_id']
            self.generation += 1
            self.selection += 1
            self.enabled, self.phase, self.error, self.stats, self.pending = True, 'waiting_call', '', {}, None
            self.playing = None
            if self.thread is None:
                self.thread = threading.Thread(target=self._run, name='PanelCallVideo', daemon=True)
                self.thread.start()
        return {'ok': True}

    def stop(self, data=None):
        with self.lock:
            session = self.session
            if data is not None and data.get('session_id') != self._connection()['session_id']:
                raise PanelError('微信连接已变化，请刷新页面', 409)
            self.enabled = False
            self.generation += 1
            self.selection += 1
            self.phase, self.pending, self.stats = 'stopped', None, {}
            self.playing = None
        if session and self._connection()['session_id'] == session:
            self._invoke(lambda service: service.stop_call_video(expected_session=session), session)
        return {'ok': True}

    def switch(self, data):
        with self.lock:
            session, account = self.session, self.account
            if not self.enabled or data.get('session_id') != session or self._connection()['session_id'] != session:
                raise PanelError('换脸尚未开启或连接已变化', 409)
            path = self._photo_path(account, data.get('photo_id'))
            if self.phase == 'waiting_call':
                self.photo = data['photo_id']
                return {'ok': True}
            if self.pending:
                raise PanelError('上一张照片仍在检查，请稍候', 409)
            generation = self.generation
            self.selection += 1
            selection = self.selection
            self.pending = ('checking', data['photo_id'])
            self.error = ''
        try:
            def change(service):
                with self.lock:
                    if not self.enabled or self.generation != generation or self.selection != selection:
                        raise PanelError('换脸状态已变化', 409)
                    return service.switch_call_video_photo(path, expected_session=session)
            result = self._invoke(change, session)
            with self.lock:
                if self.generation == generation and self.selection == selection:
                    self.pending = (result['request_id'], data['photo_id'])
        except Exception:
            with self.lock:
                if self.generation == generation and self.selection == selection:
                    self.pending = None
            raise
        return {'ok': True}

    def _tick(self):
        with self.lock:
            if not self.enabled:
                return
            session, generation = self.session, self.generation
        if self._connection()['session_id'] != session:
            with self.lock:
                self.enabled, self.phase, self.stats, self.pending = False, 'stopped', {}, None
                self.playing = None
                self.error = '微信连接已结束，请重新开始换脸'
            return
        def update(service):
            with self.lock:
                if not self.enabled or self.generation != generation:
                    return None
                try:
                    stats = service.call_video_statistics(arm=True, expected_session=session)
                except RuntimeError as error:
                    if str(error).endswith('126'):
                        return {'waiting': True}
                    raise
                from .facefusion_stream import frame_clock_ns
                recent = stats['last_tick'] and 0 <= frame_clock_ns() // 1000000 - stats['last_tick'] < 3000
                if not recent:
                    if self.phase != 'waiting_call':
                        service.stop_call_video(expected_session=session)
                        self.enabled, self.phase = False, 'stopped'
                        self.pending, self.playing = None, None
                    return {'waiting': True}
                if self.phase == 'waiting_call':
                    settings = self.settings()
                    service.start_call_video_facefusion(self._photo_path(self.account, self.photo),
                        python_executable=settings['python_executable'], repository=settings['repository'],
                        width=stats['input_width'], height=stats['input_height'], fps=15, quality='high',
                        model='hyperswap_1a_256' if settings['mode'] == 'balanced' else 'inswapper_128_fp16',
                        enhance=False, preview=True, expected_session=session)
                    self.phase = 'starting'
                return stats
        stats = self._invoke(update, session)
        with self.lock:
            if stats is None or self.generation != generation:
                return
            self.stats = stats
            ai = stats.get('ai') or {}
            if ai.get('failed'):
                raise PanelError(ai.get('error') or '换脸模型加载或处理失败，请检查照片和运行环境')
            if ai.get('phase'):
                self.phase = ai['phase']
            if self.pending and ai.get('source_request_id') == self.pending[0] and ai.get('source_status') != 'pending':
                if ai.get('source_status') == 'applied':
                    self.photo = self.pending[1]
                else:
                    self.error = ai.get('source_error') or '新照片未通过检查，已保留原照片'
                self.pending = None
            if self.playing and ai.get('video_request_id') == self.playing[0] and ai.get('video_status') in ('finished', 'failed', 'cancelled'):
                if ai['video_status'] == 'failed':
                    self.error = '视频无法播放，已回到换脸画面'
                self.playing = None

    def _run(self):
        while not self.closed.wait(.7):
            try:
                self._tick()
            except Exception as error:
                with self.lock:
                    phase = self.phase
                reason = redact(error)
                logger.warning('换脸失败（阶段 %s，%s）%s', phase, type(error).__name__, reason)
                try:
                    self.stop()
                except Exception:
                    pass
                with self.lock:
                    self.phase, self.error = 'failed', reason

    def snapshot(self):
        connection = self._connection()
        with self.lock:
            current = connection['session_id'] == self.session
            ai = self.stats.get('ai') or {}
            playback_status = (ai.get('video_status', 'pending') if self.playing and
                ai.get('video_request_id') == self.playing[0] else 'pending')
            return dict(connection, settings=self.settings(), photos=self._photos(connection['account']),
                enabled=self.enabled and current, phase=self.phase if current else 'stopped',
                error=self.error if current else '', photo_id=self.photo if current else '',
                pending_photo_id=self.pending[1] if current and self.pending else '', stats=self.stats if current else {},
                playing_video_id=self.playing[1] if current and self.enabled and self.playing else '',
                playback_status=playback_status if current and self.enabled and self.playing else 'idle')

    def preview(self):
        with self.lock:
            session, generation = self.session, self.generation
            if not self.enabled or self._connection()['session_id'] != session:
                return {'image': None}
        result = self._invoke(lambda service: service.call_video_preview(expected_session=session), session)
        # Recheck after the frame read: never deliver an old account's preview.
        with self.lock:
            if not self.enabled or self.generation != generation or self._connection()['session_id'] != session:
                return {'image': None}
        return {'image': 'data:image/jpeg;base64,' + base64.b64encode(result['jpeg']).decode(),
                'sequence': result['sequence']} if result and result['jpeg'] else {'image': None}

    def close(self):
        self.closed.set()
        try:
            self.stop()
        except Exception:
            pass
        if self.thread is not None:
            self.thread.join(16)
