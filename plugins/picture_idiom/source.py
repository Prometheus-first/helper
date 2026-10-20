"""飞飞题源：只取题图和答案，不发送微信账号、群号或玩家输入。"""
from __future__ import annotations

import hashlib
import http.client
from io import BytesIO
import json
import math
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request
import uuid

from PIL import Image, ImageOps, UnidentifiedImageError

from wxbot_framework.text import FOUR_CHINESE
from .bank import Puzzle

ENDPOINT = 'https://ffapi.cn/int/v1/ktccy'
MAX_JSON_BYTES = 64 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_PIXELS = 4_000_000


class SourceError(RuntimeError):
    pass


class RepeatedQuestion(SourceError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def image_url(value):
    """只允许题源自身的图片入口；旧 HTTP 链接统一使用 HTTPS。"""
    if (not isinstance(value, str) or not value or len(value) > 4096 or '\\' in value or
            any(ord(ch) <= 32 or ord(ch) == 127 for ch in value)):
        raise SourceError('题源返回的图片地址无效')
    try:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or parsed.hostname not in ('ffapi.cn', 'www.ffapi.cn') or
                parsed.username or parsed.password or parsed.port not in (None, 80, 443) or parsed.fragment or
                parsed.path not in ('/int/v1/ktccy', '/int/v1/ktccy/')):
            raise ValueError()
    except ValueError:
        raise SourceError('题源返回的图片地址不在允许范围') from None
    return urllib.parse.urlunsplit(('https', parsed.hostname, parsed.path, parsed.query, ''))


def clean_image(data):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise SourceError('题源图片为空或超过大小限制')
    try:
        with Image.open(BytesIO(data)) as check:
            if (check.format not in ('PNG', 'JPEG') or getattr(check, 'n_frames', 1) != 1 or
                    min(check.size) < 32 or check.width * check.height > MAX_PIXELS):
                raise SourceError('题源图片格式或尺寸不符合要求')
            check.verify()
        with Image.open(BytesIO(data)) as source:
            source = ImageOps.exif_transpose(source).convert('RGBA')
            # 新建画布，保留可见像素；不继承 EXIF、文本、缩略图等元数据。
            clean = Image.new('RGB', source.size, 'white')
            clean.paste(source, (0, 0), source.getchannel('A'))
            output = BytesIO()
            clean.save(output, format='PNG')
        return output.getvalue()
    except (OSError, ValueError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError):
        raise SourceError('题源未返回有效的完整图片') from None


class FFAPI:
    def __init__(self, directory, *, timeout_seconds=8):
        if (type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds) or
                not 1 <= timeout_seconds <= 30):
            raise ValueError('picture_idiom.timeout_seconds 应为 1–30 秒')
        self.directory = Path(directory).resolve()
        self.timeout = timeout_seconds

    def _get(self, url, *, limit, accept):
        request = urllib.request.Request(url, headers={'User-Agent': 'helper-picture-idiom', 'Accept': accept})
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=self.timeout) as response:
                data = response.read(limit + 1)
        except urllib.error.HTTPError as error:
            raise SourceError(f'题源请求失败（HTTP {error.code}）') from None
        except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException):
            raise SourceError('题源连接失败或超时') from None
        if len(data) > limit:
            raise SourceError('题源响应超过大小限制')
        return data

    def fetch(self, *, exclude=()):
        raw = self._get(ENDPOINT, limit=MAX_JSON_BYTES, accept='application/json')
        try:
            result = json.loads(raw)
            if not isinstance(result, dict) or type(result.get('code')) is not int or result['code'] != 1:
                raise ValueError()
            answer = result['answer']
            if not isinstance(answer, str) or not FOUR_CHINESE.fullmatch(answer):
                raise ValueError()
            url = image_url(result['imgurl'])
        except (KeyError, ValueError, TypeError):
            raise SourceError('题源未返回有效的四字成语和图片地址') from None
        if answer in exclude:
            raise RepeatedQuestion('在线题目近期已出现')
        png = clean_image(self._get(url, limit=MAX_IMAGE_BYTES, accept='image/png,image/jpeg'))
        code = 'online-' + hashlib.sha256(png + answer.encode('utf-8')).hexdigest()[:24]
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / (code + '.png')
        if not path.is_file():
            temporary = self.directory / ('.question-' + uuid.uuid4().hex + '.tmp')
            try:
                temporary.write_bytes(png)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        return Puzzle(code, answer, f'答案首字是“{answer[0]}”。', '', 'online', image_path=path)
