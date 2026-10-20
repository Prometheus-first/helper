"""共享语音回复层：音色按群保存、切换后取消旧音频、失败回退文字。"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
from pathlib import Path
import tempfile
import threading
import time

from .members import SnapshotExpired
from .summary import split_text
from .tts import FishTTS, TTSError, VOICE_ID


def speech_chunks(text, limit):
    text = text.strip()
    while text:
        if len(text) <= limit:
            yield text
            return
        cut = max((text.rfind(mark, 0, limit) + 1 for mark in '。！？!?；;\n'), default=0)
        if cut < limit // 2:
            cut = limit
        part, text = text[:cut].strip(), text[cut:].strip()
        if part:
            yield part


class SpeechController:
    def __init__(self, config_path, directory, members, active):
        self.path, self.directory = Path(config_path), Path(directory)
        self.members, self._active = members, active
        self._lock = threading.RLock()
        self._client = None
        self._catalog = ()
        self._catalog_time = 0
        self._public_pages = {}
        self._public_voices = {}
        self._generation = 0
        self._closed = False

    @property
    def enabled(self):
        return not self._closed and self._active() is True

    def reload(self):
        if self.members is None:
            raise TTSError("语音回复需要配置成员目录，以保存各群音色")
        client = FishTTS.from_file(self.path)
        with self._lock:
            self._client, self._catalog, self._catalog_time = client, (), 0
            self._public_pages.clear()
            self._public_voices.clear()
            self._generation += 1
            self._closed = False

    def catalog(self, *, refresh=False):
        with self._lock:
            if self._closed or self._client is None:
                raise TTSError("语音回复服务未启用")
            client, generation = self._client, self._generation
            if not refresh and self._catalog and time.monotonic() - self._catalog_time < 600:
                return self._catalog
        catalog = client.voices()
        with self._lock:
            if generation != self._generation or self._closed:
                raise SnapshotExpired("语音配置已更新")
            self._catalog, self._catalog_time = catalog, time.monotonic()
            return catalog

    def public_catalog(self, *, page=1, keyword='', page_size=10, all_languages=False):
        key = (page, keyword, page_size, all_languages)
        with self._lock:
            if self._closed or self._client is None:
                raise TTSError('语音回复服务未启用')
            cached = self._public_pages.get(key)
            if cached and time.monotonic() - cached[0] < 600:
                return cached[1]
            client, generation = self._client, self._generation
        result = client.public_voices(page=page, keyword=keyword, page_size=page_size, all_languages=all_languages)
        with self._lock:
            if self._closed or generation != self._generation:
                raise SnapshotExpired('语音配置已更新')
            now = time.monotonic()
            self._public_pages[key] = (now, result)
            for voice in result.voices:
                self._public_voices[voice.id] = (now, voice)
            while len(self._public_pages) > 64:
                self._public_pages.pop(next(iter(self._public_pages)))
            while len(self._public_voices) > 2000:
                self._public_voices.pop(next(iter(self._public_voices)))
        return result

    def _public_voice(self, identifier):
        with self._lock:
            cached = self._public_voices.get(identifier)
            if cached and time.monotonic() - cached[0] < 600:
                return cached[1]
            if self._closed or self._client is None:
                raise TTSError('语音回复服务未启用')
            client, generation = self._client, self._generation
        voice = client.public_voice(identifier)
        with self._lock:
            if self._closed or generation != self._generation:
                raise SnapshotExpired('语音配置已更新')
            self._public_voices[identifier] = (time.monotonic(), voice)
        return voice

    def resolve(self, value):
        # 个人音色优先，保留已有配置；公开重名音色必须用 ID 指定。
        if not isinstance(value, str) or not value or len(value) > 128:
            raise TTSError('请发送 /查看音色，选择音色名称或 ID')
        matches = [voice for voice in self.catalog() if voice.id == value or voice.name.casefold() == value.casefold()]
        if not matches and VOICE_ID.fullmatch(value):
            return self._public_voice(value)
        if not matches:
            candidates = {}
            for page in range(1, 11):
                result = self.public_catalog(page=page, keyword=value, page_size=100, all_languages=True)
                for voice in result.voices:
                    if voice.name.casefold() == value.casefold():
                        candidates[voice.id] = voice
                if len(candidates) > 1 or not result.has_more:
                    break
            else:
                raise TTSError('同名搜索范围过大，请使用 /音色 ID 选择')
            matches = list(candidates.values())
        if len(matches) > 1:
            identifiers = '\n'.join(voice.id for voice in matches[:3])
            raise TTSError('存在同名音色，请用 /音色 ID 选择：\n' + identifiers)
        if not matches:
            raise TTSError('音色未找到，请发送 /查看音色 关键词 搜索；切换时使用完整名称或 ID')
        return matches[0]

    def selected(self, room):
        state = self.members.voice_selection(room)
        if not state.enabled:
            return state, None
        with self._lock:
            client = self._client
        if client is None:
            raise TTSError("语音配置未加载")
        return state, self.resolve(state.voice_id or client.default_voice)

    def select(self, room, name):
        if name in ('关闭', '文字'):
            return self.members.set_voice(room, '', '', enabled=False)
        voice = self.resolve(name)
        return self.members.set_voice(room, voice.id, voice.name)

    def close(self):
        with self._lock:
            self._closed = True
            self._generation += 1
            self._catalog = ()
            self._public_pages.clear()
            self._public_voices.clear()

    def reply(self, ctx, message, text, *, delivery_guard=None):
        state, voice = self.selected(message.chat_id)
        if voice is None:
            return False
        with self._lock:
            client, generation = self._client, self._generation

        @contextmanager
        def guard():
            if not ctx._service.session_is_current(message.session_id):
                raise SnapshotExpired("微信连接已结束")
            if not self.enabled:
                raise SnapshotExpired("语音回复已停用")
            with delivery_guard() if delivery_guard else nullcontext():
                with self.members.voice_delivery_guard(state), self._lock:
                    if self._closed or generation != self._generation:
                        raise SnapshotExpired("语音回复配置已变更")
                    yield

        # 先合成完整回复的所有片段；失败时还没发送，可安全回退整段文字。
        account = hashlib.sha256(self.members.account.encode('ascii')).hexdigest()[:16]
        directory = self.directory / account
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='tts-', dir=directory) as temporary:
            owned = Path(temporary).resolve()
            if not owned.is_relative_to(directory.resolve()):
                raise TTSError("语音临时目录超出预期范围")
            paths = []
            for index, part in enumerate(speech_chunks(text, client.max_chars_per_clip)):
                with guard():
                    pass
                data = client.synthesize(part, voice.id)
                with guard():
                    pass
                path = owned / f'{index}.wav'
                path.write_bytes(data)
                paths.append(path)
            with guard():
                pass
            for path in paths:
                if not ctx.reply_voice(message, path, delivery_guard=guard):
                    # 微信是否已送达不确定，不在此处重复发文字或重试。
                    raise RuntimeError("原生语音发送未确认成功")
        return True


def deliver_model_reply(ctx, message, text, *, display_name=None, delivery_guard=None):
    speech = getattr(ctx, '_speech', None)
    if speech is not None and speech.enabled is True:
        try:
            if speech.reply(ctx, message, text, delivery_guard=delivery_guard):
                return True
        except TTSError as error:
            ctx.logger.warning("语音合成失败，回退文字：%s", error)
    budget = 1023 if display_name is None else 1023 - len(('@' + display_name + '\u2005\n').encode('utf-8'))
    for part in split_text(text, byte_limit=budget):
        sent = (ctx.reply(message, part, delivery_guard=delivery_guard) if display_name is None else
                ctx.reply_at(message, part, display_name=display_name, delivery_guard=delivery_guard))
        if not sent:
            raise RuntimeError("原生回复发送未成功")
    return True
