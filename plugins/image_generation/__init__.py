"""使用本地配置的 OpenAI 兼容接口生成图片。"""
from contextlib import contextmanager
from pathlib import Path
import queue
import threading

from .client import GenerationClient, GenerationError, GenerationCancelled

MAX_CONCURRENT = 5
MAX_PENDING = 8
MAX_INFLIGHT_CHANNELS = 20
RETRY_DELAYS = (1, 2, 4, 8, 10)


class Plugin:
    def on_load(self, ctx):
        self.members = ctx.members
        self.stopped = threading.Event()
        self.channel_slots = threading.BoundedSemaphore(MAX_INFLIGHT_CHANNELS)
        root = Path(ctx.project_root)
        self.client = GenerationClient(root / '.runtime/image_generation.json', root / '.runtime/generated_images')
        self.jobs = queue.Queue()
        self.slots = threading.BoundedSemaphore(MAX_CONCURRENT + MAX_PENDING)
        self.workers = [threading.Thread(target=self._worker, args=(ctx,), daemon=True,
                                         name=f'ImageGeneration-{index}')
                        for index in range(MAX_CONCURRENT)]
        for worker in self.workers:
            worker.start()

    def on_unload(self, ctx):
        self.stopped.set()
        while True:
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                break
            self.jobs.task_done()
            self.slots.release()

    def _worker(self, ctx):
        while not self.stopped.is_set():
            try:
                message, prompt = self.jobs.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if self._current(message, ctx):
                    self._generate(message, prompt, ctx)
            except Exception as error:
                ctx.logger.warning('生图任务未完成（%s）', type(error).__name__)
            finally:
                self.jobs.task_done()
                self.slots.release()

    def _current(self, message, ctx):
        service = getattr(ctx, '_service', None)
        return (not self.stopped.is_set() and ctx.plugin_is_active('image_generation')
                and (not message.is_group or message.chat_id in self.members.groups)
                and (service is None or service.session_is_current(message.session_id)))

    @contextmanager
    def _guard(self, message, ctx):
        if not self._current(message, ctx):
            raise GenerationCancelled('会话已结束')
        yield

    def _notice(self, message, ctx, text):
        if message.is_group and self._current(message, ctx):
            try:
                ctx.reply(message, text, delivery_guard=lambda: self._guard(message, ctx))
            except GenerationCancelled:
                pass
            except Exception as error:
                ctx.logger.warning('生图提醒发送未完成（%s）', type(error).__name__)

    def on_message(self, message, ctx):
        if message.type != 1 or message.is_self or message.data.get('bot_generated'):
            return
        text = message.text.strip()
        if text != '/生图' and not (text.startswith('/生图') and len(text) > 3 and text[3].isspace()):
            return
        if not self._current(message, ctx):
            return
        prompt = text[3:].strip()
        guard = lambda: self._guard(message, ctx)
        if not prompt:
            ctx.reply(message, '用法：/生图 提示词', delivery_guard=guard)
            return
        if len(prompt) > 4000:
            ctx.reply(message, '提示词太长，请控制在 4000 字以内。', delivery_guard=guard)
            return
        if not self.slots.acquire(blocking=False):
            ctx.reply(message, '生图任务较多，请稍后再试。', delivery_guard=guard)
            return
        try:
            if not self._current(message, ctx):
                self.slots.release()
                return
            self.jobs.put_nowait((message, prompt))
        except BaseException:
            self.slots.release()
            raise

    def _generate(self, message, prompt, ctx):
        guard = lambda: self._guard(message, ctx)
        try:
            channels = self.client.channels()
        except GenerationError as error:
            ctx.logger.warning('生图配置无效：%s', error)
            if self._current(message, ctx):
                ctx.reply(message, '生图失败', delivery_guard=guard)
            return
        self._notice(message, ctx, '开始生图，请稍候。')
        cancel = threading.Event()
        winner_lock = threading.Lock()
        outcomes = queue.Queue()

        def current():
            return not cancel.is_set() and self._current(message, ctx)

        def channel_job(channel_index, channel):
            for attempt in range(len(RETRY_DELAYS) + 1):
                if not current():
                    return
                acquired = False
                while current() and not acquired:
                    acquired = self.channel_slots.acquire(timeout=0.2)
                if not current():
                    if acquired:
                        self.channel_slots.release()
                    return
                try:
                    with self.client.generate(prompt, current=current, base_url=channel) as image:
                        with winner_lock:
                            if not current():
                                return
                            cancel.set()
                        try:
                            with self._guard(message, ctx):
                                try:
                                    result = ctx.reply_image(message, image.path, delivery_guard=guard)
                                except Exception:
                                    image.retain()
                                    raise
                                if result is False:
                                    image.retain()
                                    ctx.logger.warning('生图发送结果未确认，保留临时文件')
                        except GenerationCancelled:
                            pass
                        except Exception as error:
                            ctx.logger.warning('生图发送未完成（%s）', type(error).__name__)
                        finally:
                            outcomes.put('winner')
                        return
                except GenerationCancelled:
                    return
                except Exception as error:
                    detail = str(error) if isinstance(error, GenerationError) else type(error).__name__
                    ctx.logger.warning('生图渠道 %d 请求未完成：%s', channel_index + 1, detail)
                    if attempt == len(RETRY_DELAYS):
                        outcomes.put('failed')
                        return
                finally:
                    self.channel_slots.release()
                if cancel.wait(RETRY_DELAYS[attempt]) or not current():
                    return

        for index, channel in enumerate(channels):
            threading.Thread(target=channel_job, args=(index, channel), daemon=True,
                             name=f'ImageGeneration-channel-{index + 1}').start()
        failures = 0
        try:
            while self._current(message, ctx):
                try:
                    outcome = outcomes.get(timeout=0.2)
                except queue.Empty:
                    continue
                if outcome == 'winner':
                    return
                failures += 1
                if failures == len(channels):
                    ctx.reply(message, '生图失败', delivery_guard=guard)
                    return
        finally:
            cancel.set()
