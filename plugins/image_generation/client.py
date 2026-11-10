"""受限读取 API 结果，不记录密钥、提示词和返回地址。"""
import base64
import binascii
from contextlib import contextmanager
from io import BytesIO
import ipaddress
import json
from pathlib import Path
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid
import warnings

from PIL import Image, ImageOps
from plugins.image_gallery.source import ImageLease

MAX_BYTES = 20 * 1024 * 1024
MAX_RESPONSE = 30 * 1024 * 1024
MAX_PIXELS = 40_000_000


class GenerationError(RuntimeError):
    pass


class GenerationCancelled(GenerationError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _check(current):
    if not current():
        raise GenerationCancelled('会话已结束')


def _public_url(value):
    try:
        parsed = urllib.parse.urlsplit(value)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError()
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)
        if not addresses or not all(ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError()
    except (TypeError, ValueError, OSError):
        raise GenerationError('图片下载地址无效') from None
    return value


class GenerationClient:
    def __init__(self, config_path, directory):
        self.config_path = Path(config_path)
        self.directory = Path(directory)
        self.opener = urllib.request.build_opener(NoRedirect())

    def _config(self):
        try:
            config = json.loads(self.config_path.read_text(encoding='utf-8-sig'))
            bases = config.get('base_urls', [config.get('base_url')])
            if not isinstance(bases, list) or not 1 <= len(bases) <= 8:
                raise ValueError()
            normalized = []
            for base in bases:
                if not isinstance(base, str) or not base:
                    raise ValueError()
                base = base.rstrip('/')
                parsed = urllib.parse.urlsplit(base)
                if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError()
                normalized.append(base)
            if len(set(normalized)) != len(normalized):
                raise ValueError()
            key, model = config['api_key'], config['model']
            timeout = config.get('timeout_seconds', 180)
            if (not isinstance(key, str) or not key or any(c in key for c in '\r\n') or
                    not isinstance(model, str) or not model or type(timeout) not in (int, float) or not 1 <= timeout <= 600):
                raise ValueError()
            return normalized, key, model, timeout
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise GenerationError('本地生图配置无效') from None

    def channels(self):
        return self._config()[0]

    def _read(self, request, limit, timeout, *, label='生图接口'):
        try:
            with self.opener.open(request, timeout=timeout) as response:
                declared = response.headers.get('Content-Length')
                if declared and (not declared.isdecimal() or int(declared) > limit):
                    raise GenerationError('接口返回内容过大')
                data = response.read(limit + 1)
                if len(data) > limit or (declared and len(data) != int(declared)):
                    raise GenerationError('接口返回内容过大或不完整')
                return data
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise GenerationError(f'{label}返回 HTTP {status}') from None
        except (OSError, TimeoutError, urllib.error.URLError):
            raise GenerationError('连接接口失败或超时') from None

    def _image_bytes(self, item, timeout):
        try:
            if isinstance(item.get('b64_json'), str):
                encoded = item['b64_json']
                if len(encoded) > (MAX_BYTES * 4 // 3 + 8):
                    raise GenerationError('图片超过微信发送大小限制')
                return base64.b64decode(encoded, validate=True)
            if isinstance(item.get('url'), str):
                url = _public_url(item['url'])
                return self._read(urllib.request.Request(url, headers={'Accept': 'image/*', 'User-Agent': 'helper-image-generation/1.0'}), MAX_BYTES, timeout, label='图片下载')
        except (ValueError, binascii.Error):
            raise GenerationError('图片数据无效') from None
        raise GenerationError('接口没有返回图片')

    def _clean_image(self, raw):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(BytesIO(raw)) as source:
                    if source.format not in ('PNG', 'JPEG', 'WEBP') or not 0 < source.width * source.height <= MAX_PIXELS:
                        raise ValueError()
                    source.load()
                    picture = ImageOps.exif_transpose(source).convert('RGBA')
                    clean = Image.new('RGB', picture.size, 'white')
                    clean.paste(picture, (0, 0), picture.getchannel('A'))
                    output = BytesIO()
                    clean.save(output, format='JPEG', quality=94)
            result = output.getvalue()
            if len(result) > MAX_BYTES:
                raise GenerationError('图片超过微信发送大小限制')
            return result
        except (OSError, ValueError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError):
            raise GenerationError('接口返回的图片无效') from None

    @contextmanager
    def generate(self, prompt, *, current=lambda: True, base_url=None):
        _check(current)
        bases, key, model, timeout = self._config()
        base = bases[0] if base_url is None else base_url
        if base not in bases:
            raise GenerationError('生图渠道配置已变化')
        endpoint = base + ('' if base.endswith('/v1') else '/v1') + '/images/generations'
        body = json.dumps({'model': model, 'prompt': prompt, 'n': 1}, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(endpoint, data=body, headers={'Authorization': 'Bearer ' + key,
                                         'Content-Type': 'application/json', 'Accept': 'application/json',
                                         'User-Agent': 'Mozilla/5.0'})
        raw = self._read(request, MAX_RESPONSE, timeout)
        _check(current)
        try:
            result = json.loads(raw)
            item = result['data'][0]
            if not isinstance(item, dict):
                raise ValueError()
        except (ValueError, TypeError, KeyError, IndexError):
            raise GenerationError('接口返回格式不受支持') from None
        image = self._clean_image(self._image_bytes(item, timeout))
        _check(current)
        folder = self.directory / ('request-' + uuid.uuid4().hex)
        folder.mkdir(parents=True)
        lease = ImageLease(folder / 'image.jpg')
        try:
            lease.path.write_bytes(image)
            yield lease
        finally:
            lease.close()
