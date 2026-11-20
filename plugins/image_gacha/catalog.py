"""Read the site's catalog as opaque strings, without saving or displaying prompts."""
import json
import secrets
import threading
import time
import urllib.error
import urllib.request

from plugins.image_generation.client import GenerationCancelled, GenerationError, NoRedirect

CATALOG_URL = 'https://image.52131415.xyz/catalog.json'
MAX_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 5000
MAX_PROMPT_LENGTH = 16000
CACHE_SECONDS = 300


class PromptCatalog:
    def __init__(self):
        self.opener = urllib.request.build_opener(NoRedirect())
        self._lock = threading.Lock()
        self._entries = ()
        self._expires = 0.0

    def _load(self):
        request = urllib.request.Request(CATALOG_URL, headers={
            'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json',
            'Referer': 'https://image.52131415.xyz/'})
        try:
            with self.opener.open(request, timeout=20) as response:
                length = response.headers.get('Content-Length')
                if length and (not length.isdecimal() or int(length) > MAX_BYTES):
                    raise GenerationError('抽卡目录大小无效')
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES or (length and len(raw) != int(length)):
                    raise GenerationError('抽卡目录过大或不完整')
            entries = json.loads(raw)
            if (not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ENTRIES or
                    any(not isinstance(value, str) or not value.strip() or
                        len(value) > MAX_PROMPT_LENGTH or '\0' in value for value in entries)):
                raise GenerationError('抽卡目录格式不受支持')
            return tuple(entries)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise GenerationError(f'抽卡目录返回 HTTP {status}') from None
        except (OSError, TimeoutError, urllib.error.URLError):
            raise GenerationError('抽卡目录连接失败或超时') from None
        except (ValueError, RecursionError):
            raise GenerationError('抽卡目录格式不受支持') from None

    def pick(self, *, current):
        if not current():
            raise GenerationCancelled('会话已结束')
        with self._lock:
            if not current():
                raise GenerationCancelled('会话已结束')
            if not self._entries or time.monotonic() >= self._expires:
                entries = self._load()
                if not current():
                    raise GenerationCancelled('会话已结束')
                self._entries = entries
                self._expires = time.monotonic() + CACHE_SECONDS
            if not current():
                raise GenerationCancelled('会话已结束')
            return secrets.choice(self._entries)
