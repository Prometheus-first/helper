"""群聊图库插件：固定指令取图，不把账号、群号或聊天内容交给图库。"""
from contextlib import contextmanager
import hashlib
import math
from pathlib import Path
import threading
import time

from .source import GalleryCancelled, GalleryClient, GalleryError, finite_number


class Plugin:
    def on_load(self, ctx):
        commands=ctx.config.get('commands',['/来张图','/美女'])
        if (not isinstance(commands,list) or not 1<=len(commands)<=8 or
                any(not isinstance(value,str) or not value.startswith('/') or not 1<len(value)<=32
                    or any(char.isspace() or ord(char)<32 for char in value) for value in commands)):
            raise ValueError('image_gallery.commands 应为 1–8 个无空格的斜杠指令')
        self.commands=frozenset(commands)
        self.cooldown=finite_number(ctx.config.get('cooldown_seconds',10),'cooldown_seconds',0,300)
        self.members=ctx.members
        account=hashlib.sha256(self.members.account.encode('ascii')).hexdigest()[:16]
        directory=Path(__file__).resolve().parents[2]/'.runtime/image_gallery'/account
        self.client=GalleryClient(directory,ctx.config)
        self.stopped=threading.Event()
        self.last_request={}
        self.session_id=None

    def on_unload(self, ctx):
        self.stopped.set()
        self.last_request.clear()

    def _current(self, message, ctx):
        service=getattr(ctx,'_service',None)
        return (not self.stopped.is_set() and ctx.plugin_is_active('image_gallery') and
                message.chat_id in self.members.groups and
                (service is None or service.session_is_current(message.session_id)))

    @contextmanager
    def _guard(self, message, ctx):
        if not self._current(message,ctx):
            raise GalleryCancelled('图库回复已取消')
        yield

    def on_message(self, message, ctx):
        if (message.type!=1 or not message.is_group or message.is_self or
                message.data.get('bot_generated') or message.data.get('is_at_self') or not self._current(message,ctx)):
            return
        text=message.text.strip()
        if text=='/help':
            if not any(ctx.plugin_is_active(name) for name in ('idiom_chain','picture_idiom')):
                self._failure(message,ctx,'1. /来张图\n\n2. /help')
            return
        if text not in self.commands:
            return
        if self.session_id!=message.session_id:
            self.last_request.clear();self.session_id=message.session_id
        now=time.monotonic()
        received=message.received_at
        if type(received) not in (int,float) or not math.isfinite(received) or not 0<=now-received<=30:
            return
        previous=self.last_request.get(message.chat_id)
        if previous is not None and received-previous<self.cooldown:
            return
        self.last_request[message.chat_id]=received
        current=lambda:self._current(message,ctx)
        guard=lambda:self._guard(message,ctx)
        try:
            with self.client.fetch(current=current) as image:
                self._guard_check(message,ctx)
                try:
                    success=ctx.reply_image(message,image.path,delivery_guard=guard)
                except GalleryCancelled:
                    raise
                except Exception:
                    image.retain()
                    raise
                if not success:
                    image.retain()
                    ctx.logger.warning('图库图片发送结果未确认，保留单次缓存，不自动重发')
                    return
        except GalleryCancelled:
            return
        except GalleryError as error:
            ctx.logger.warning('图库取图失败：%s',error)
            self._failure(message,ctx,'取图失败，请稍后再试。')
        except Exception as error:
            ctx.logger.warning('图库图片处理未完成（%s），不自动重发',type(error).__name__)

    def _guard_check(self, message, ctx):
        if not self._current(message,ctx):
            raise GalleryCancelled('图库回复已取消')

    def _failure(self, message, ctx, text):
        if self._current(message,ctx):
            try:
                ctx.reply(message,text,delivery_guard=lambda:self._guard(message,ctx))
            except (GalleryCancelled,RuntimeError):
                pass
