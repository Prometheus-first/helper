"""Fish Audio 的音色查询和 WAV 合成；密钥与服务响应不写入日志。"""
from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
import json
import http.client
import math
from pathlib import Path
import re
import struct
import urllib.error
import urllib.parse
import urllib.request
import wave

MAX_AUDIO_BYTES = 20 * 1024 * 1024
VOICE_ID = re.compile(r"^[0-9a-f]{32}$")


class TTSError(RuntimeError):
    pass


@dataclass(frozen=True)
class Voice:
    id: str
    name: str


@dataclass(frozen=True)
class VoicePage:
    voices: tuple[Voice, ...]
    page: int
    page_size: int
    has_more: bool
    window_limited: bool = False


def _voice(item, *, public=False):
    if not isinstance(item, dict):
        return None
    identifier, name = item.get('_id'), item.get('title')
    if (item.get('type') != 'tts' or item.get('state') != 'trained' or item.get('dmca_taken_down') or
            (public and item.get('visibility') != 'public') or
            not isinstance(identifier, str) or not VOICE_ID.fullmatch(identifier) or
            not isinstance(name, str) or not name.strip() or len(name) > 128 or
            any(ord(ch) < 32 or ord(ch) == 127 for ch in name)):
        return None
    return Voice(identifier, name.strip())


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def normalize_wav(data):
    """将 Fish 流式 WAV 的占位长度换成实际长度，保持 PCM 原样。"""
    if not isinstance(data, bytes) or not 44 <= len(data) <= MAX_AUDIO_BYTES:
        raise TTSError("语音接口未返回有效音频")
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise TTSError("语音接口未返回 WAV 音频")
    try:
        riff_size = struct.unpack_from('<I', data, 4)[0]
        with wave.open(BytesIO(data), 'rb') as source:
            rate, frames = source.getframerate(), source.getnframes()
            if (source.getnchannels() != 1 or source.getsampwidth() != 2 or
                    source.getcomptype() != 'NONE' or rate not in (8000, 12000, 16000, 24000)):
                raise TTSError("合成音频必须是单声道 16-bit PCM，采样率为 8/12/16/24 kHz")
            # 实机 Fish WAV 使用 data=0xffffff00、RIFF=0xffffff24；
            # 同时兼容采用 0xffffffff 的流式未知长度头。
            streaming = riff_size >= 0xffff0000 and frames >= 0x7fff0000
            if not streaming and riff_size + 8 != len(data):
                raise TTSError("语音 WAV 长度不完整")
            pcm = source.readframes(rate * 60 + 1)
            if not pcm or len(pcm) % 2 or len(pcm) > rate * 60 * 2:
                raise TTSError("单条语音必须非空且不超过 60 秒")
            if not streaming and len(pcm) != frames * 2:
                raise TTSError("语音 WAV 数据被截断")
        output = BytesIO()
        with wave.open(output, 'wb') as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(rate)
            target.writeframes(pcm)
        return output.getvalue()
    except (wave.Error, EOFError, struct.error, ValueError):
        raise TTSError("无法解析语音 WAV 数据") from None


@dataclass(frozen=True)
class FishTTS:
    base_url: str
    api_key: str = field(repr=False)
    model: str = 's2.1-pro-free'
    default_voice: str = ''
    timeout_seconds: float = 45
    max_chars_per_clip: int = 160

    @classmethod
    def from_file(cls, path):
        try:
            data = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            raise TTSError("请先填写 tts_config.json 语音配置") from None
        return cls.from_options(data)

    @classmethod
    def from_options(cls, data):
        if not isinstance(data, dict):
            raise TTSError("语音配置必须是 JSON 对象")
        base = data.get('base_url', 'https://api.fish.audio')
        key = data.get('api_key')
        model = data.get('model', 's2.1-pro-free')
        default = data.get('default_voice', '')
        timeout = data.get('timeout_seconds', 45)
        size = data.get('max_chars_per_clip', 160)
        if not isinstance(base, str):
            raise TTSError("语音接口地址无效")
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise TTSError("语音接口地址必须是无凭据的 HTTPS 地址")
        if not isinstance(key, str) or not key.strip() or any(not 33 <= ord(ch) <= 126 for ch in key):
            raise TTSError("请在 tts_config.json 填写有效 api_key")
        if model not in ('s2.1-pro-free', 's2.1-pro', 's2-pro', 's1', 'drama-3'):
            raise TTSError("语音 model 不在已支持的 Fish 模型列表中")
        if not isinstance(default, str) or len(default) > 128 or any(ord(ch) < 32 for ch in default):
            raise TTSError("默认音色名称或 ID 无效")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 120:
            raise TTSError("语音 timeout_seconds 应为 1–120 秒")
        if type(size) is not int or not 20 <= size <= 180:
            raise TTSError("max_chars_per_clip 应为 20–180 的整数")
        return cls(base.rstrip('/'), key.strip(), model, default.strip(), timeout, size)

    def _request(self, path, *, payload=None, limit):
        headers = {'Authorization': 'Bearer ' + self.api_key, 'User-Agent': 'helper-fish-tts'}
        body = None
        if payload is not None:
            headers.update({'Content-Type': 'application/json', 'model': self.model})
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        request = urllib.request.Request(self.base_url + path, data=body, headers=headers)
        try:
            with urllib.request.build_opener(_NoRedirect()).open(request, timeout=self.timeout_seconds) as response:
                data = response.read(limit + 1)
        except urllib.error.HTTPError as error:
            raise TTSError(f"Fish Audio 请求失败（HTTP {error.code}）") from None
        except (OSError, TimeoutError, urllib.error.URLError, http.client.HTTPException):
            raise TTSError("Fish Audio 连接失败或超时") from None
        if len(data) > limit:
            raise TTSError("Fish Audio 响应超过大小限制")
        return data

    def voices(self):
        result = {}
        for page in range(1, 21):
            raw = self._request(f'/model?self=true&page_size=100&page_number={page}', limit=2 * 1024 * 1024)
            try:
                body = json.loads(raw)
                items = body['items']
                if not isinstance(items, list):
                    raise ValueError()
                for item in items:
                    voice = _voice(item)
                    if voice is not None:
                        result[voice.id] = voice
                if body.get('has_more') is False or (body.get('has_more') is None and len(items) < 100):
                    return tuple(sorted(result.values(), key=lambda voice: (voice.name.casefold(), voice.id)))
            except (ValueError, KeyError, TypeError, AttributeError):
                raise TTSError("Fish Audio 音色列表格式不正确") from None
        raise TTSError("音色数量超过当前查询预算，请缩小工作区音色范围")

    def public_voices(self, *, page=1, keyword='', page_size=10, all_languages=False):
        if type(page) is not int or not 1 <= page <= 1000:
            raise TTSError('音色页码应为 1–1000 的整数')
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise TTSError('音色分页大小应为 1–100 的整数')
        if type(all_languages) is not bool:
            raise TTSError('音色语言范围应为布尔值')
        if (not isinstance(keyword, str) or len(keyword) > 128 or
                any(ord(ch) < 32 or ord(ch) == 127 for ch in keyword)):
            raise TTSError('音色搜索词应为最多 128 字符的单行文字')
        query = {'self': 'false', 'page_size': page_size, 'page_number': page, 'sort_by': 'score'}
        if not all_languages:
            query.update(language='zh', title_language='zh')
        if keyword.strip():
            query['title'] = keyword.strip()
        raw = self._request('/model?' + urllib.parse.urlencode(query), limit=2 * 1024 * 1024)
        try:
            body = json.loads(raw)
            items = body['items']
            if not isinstance(items, list):
                raise ValueError()
            voices = {}
            for item in items:
                voice = _voice(item, public=True)
                if voice is not None:
                    voices[voice.id] = voice
            more = body.get('has_more')
            return VoicePage(tuple(voices.values()), page, page_size,
                             more if type(more) is bool else len(items) == page_size,
                             body.get('window_limited') is True)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise TTSError('Fish Audio 公开音色列表格式不正确') from None

    def public_voice(self, identifier):
        if not isinstance(identifier, str) or not VOICE_ID.fullmatch(identifier):
            raise TTSError('公开音色 ID 无效')
        raw = self._request('/model/' + identifier, limit=2 * 1024 * 1024)
        try:
            voice = _voice(json.loads(raw), public=True)
        except (ValueError, TypeError):
            voice = None
        if voice is None or voice.id != identifier:
            raise TTSError('该公开音色不可用、尚未训练或已下架')
        return voice

    def synthesize(self, text, voice_id):
        if not isinstance(text, str) or not text.strip() or len(text) > self.max_chars_per_clip or '\0' in text:
            raise TTSError("合成文本为空或超过单条长度限制")
        if not isinstance(voice_id, str) or not VOICE_ID.fullmatch(voice_id):
            raise TTSError("合成音色 ID 无效")
        data = self._request('/v1/tts', payload={'text': text, 'reference_id': voice_id,
            'format': 'wav', 'sample_rate': 24000}, limit=MAX_AUDIO_BYTES)
        return normalize_wav(data)
