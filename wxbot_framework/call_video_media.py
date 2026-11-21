"""Bounded, account-scoped video uploads for the local call-video library."""
import base64
import hashlib
from io import BytesIO
import json
import re
import subprocess
import time
import uuid

from PIL import Image
from .panel_common import PanelError

MAX_VIDEO_BYTES = 128 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
IDENTIFIER = re.compile(r'[a-f0-9]{32}\Z')


def inspect_video(path):
    """Decode one bounded thumbnail; admit containers, never playlists/URLs."""
    from .emoticons import _ffmpeg_executable
    with path.open('rb') as stream:
        magic = stream.read(16)
    if magic[4:8] == b'ftyp':
        container = 'mov'
    elif magic[:4] == b'\x1aE\xdf\xa3':
        container = 'matroska'
    else:
        raise PanelError('视频须为 MP4、MOV、WebM 或 MKV 容器')
    try:
        result = subprocess.run([_ffmpeg_executable(), '-hide_banner', '-nostdin', '-threads', '2',
            '-protocol_whitelist', 'file,pipe', '-f', container, '-i', str(path), '-map', '0:v:0',
            '-frames:v', '1', '-vf', 'scale=180:180:force_original_aspect_ratio=decrease',
            '-an', '-sn', '-dn', '-c:v', 'mjpeg', '-f', 'image2pipe', 'pipe:1'],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        match = re.search(rb'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', result.stderr)
        if result.returncode or not match or not 0 < len(result.stdout) <= 100000:
            raise ValueError()
        duration = int(match[1]) * 3600 + int(match[2]) * 60 + float(match[3])
        if not .1 <= duration <= 900:
            raise ValueError()
        with Image.open(BytesIO(result.stdout)) as image:
            image.verify()
        return {'duration': round(duration, 3), 'thumbnail': result.stdout}
    except Exception:
        raise PanelError('视频无法读取，或时长超过 15 分钟；请选择有效的短视频') from None


class VideoUploads:
    def __init__(self, panel):
        self.panel = panel

    def request(self, data):
        panel = self.panel
        account = panel._connection()['account']
        folder = panel._directory(account)
        action = data.get('action')
        with panel.lock:
            # Incomplete transfers are bounded and expire without entering the library.
            for manifest in folder.glob('*.upload.json'):
                if IDENTIFIER.fullmatch(manifest.name.split('.')[0]) and time.time() - manifest.stat().st_mtime > 3600:
                    (folder / (manifest.name.split('.')[0] + '.part')).unlink(missing_ok=True)
                    manifest.unlink(missing_ok=True)
            for staged in folder.glob('*.checking'):
                if IDENTIFIER.fullmatch(staged.stem) and time.time() - staged.stat().st_mtime > 3600:
                    staged.unlink(missing_ok=True)
            if action == 'begin':
                name, size = data.get('name'), data.get('size')
                if not isinstance(name, str) or not name.strip() or len(name) > 120 or any(ord(c) < 32 or c in '/\\' for c in name):
                    raise PanelError('视频名称无效')
                if type(size) is not int or not 0 < size <= MAX_VIDEO_BYTES:
                    raise PanelError('视频须为非空且不超过 128 MiB')
                inflight = len(list(folder.glob('*.upload.json'))) + len(list(folder.glob('*.checking')))
                if len(panel._photos(account)) >= 24 or inflight >= 2:
                    raise PanelError('素材库已满，或有视频仍在上传，请稍后再试')
                identifier = uuid.uuid4().hex
                (folder / (identifier + '.part')).touch(exist_ok=False)
                (folder / (identifier + '.upload.json')).write_text(json.dumps({'name': name, 'size': size}), encoding='utf8')
                return {'upload_id': identifier, 'chunk_bytes': CHUNK_BYTES}
            identifier = data.get('upload_id')
            if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
                raise PanelError('视频上传编号无效')
            manifest = folder / (identifier + '.upload.json')
            partial = folder / (identifier + '.part')
            if (not manifest.is_file() or not partial.is_file() or manifest.is_symlink() or partial.is_symlink()):
                raise PanelError('上传已结束或失效，请重新选择视频', 409)
            record = json.loads(manifest.read_text(encoding='utf8'))
            if action == 'chunk':
                encoded = data.get('bytes')
                if (type(data.get('offset')) is not int or data['offset'] != partial.stat().st_size or
                        not isinstance(encoded, str) or len(encoded) > 4 * ((CHUNK_BYTES + 2) // 3)):
                    raise PanelError('视频分块顺序或大小无效', 409)
                try:
                    chunk = base64.b64decode(encoded, validate=True)
                except ValueError:
                    raise PanelError('视频分块无效') from None
                if not chunk or len(chunk) > CHUNK_BYTES or partial.stat().st_size + len(chunk) > record['size']:
                    raise PanelError('视频分块超过声明大小')
                with partial.open('ab') as stream:
                    stream.write(chunk)
                manifest.touch()
                return {'received': partial.stat().st_size}
            if action == 'cancel':
                partial.unlink(missing_ok=True); manifest.unlink(missing_ok=True)
                return {'ok': True}
            if action != 'finish' or partial.stat().st_size != record['size']:
                raise PanelError('视频上传尚未完成', 409)
            # Claim the transfer so concurrent finish/chunk requests cannot race validation.
            staging = folder / (identifier + '.checking')
            partial.rename(staging)
            staging.touch()
            manifest.unlink()
        try:
            with staging.open('rb') as stream:
                checksum = hashlib.sha256()
                for chunk in iter(lambda: stream.read(CHUNK_BYTES), b''):
                    checksum.update(chunk)
                digest = checksum.hexdigest()
            with panel.lock:
                for row in panel._photos(account):
                    metadata = json.loads((folder / (row['id'] + '.json')).read_text(encoding='utf8'))
                    if row['kind'] == 'video' and metadata.get('digest') == digest:
                        return {'video_id': row['id']}
            inspected = inspect_video(staging)
            with panel.lock:
                if len(panel._photos(account)) >= 24:
                    raise PanelError('当前账号最多保存 24 个素材')
                staging.rename(folder / (identifier + '.video'))
                (folder / (identifier + '.thumb.jpg')).write_bytes(inspected['thumbnail'])
                (folder / (identifier + '.json')).write_text(json.dumps({'name': record['name'], 'kind': 'video',
                    'duration': inspected['duration'], 'size': record['size'], 'digest': digest}, ensure_ascii=False), encoding='utf8')
                return {'video_id': identifier}
        except Exception:
            for suffix in ('.video', '.thumb.jpg', '.json'):
                (folder / (identifier + suffix)).unlink(missing_ok=True)
            raise
        finally:
            staging.unlink(missing_ok=True)
