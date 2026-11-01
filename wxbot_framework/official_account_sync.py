"""已登录微信的公众号主动同步；私有分页游标只在本进程短期持有。"""
from __future__ import annotations

from collections import OrderedDict
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
import hashlib
import re
import secrets
import threading
import time

from .articles import MAX_ARTICLES
from .official_accounts import (OfficialAccountReadError, _Budget, _inputs,
                                _login_snapshot, _platform, read_loaded_articles)
from .official_account_parser import _date_fields, _metadata, canonical_url, url_biz

_MAX_PAGES = 50
_TOKEN_SECONDS = 120.0
_MAX_SYNCS = 8
_BODY_SECONDS = 32.0
_CURSOR = re.compile(r"[A-Za-z0-9+/=_-]{1,8192}\Z")
_TOKEN = re.compile(r"[a-f0-9]{64}\Z")
_CHANGED = "微信登录会话已变化，请重新同步文章列表"


def _transport():
    from native import article_transport
    return article_transport


def capabilities(pid, account):
    from .official_accounts import capabilities as cached_capabilities
    base = cached_capabilities(pid, account)
    try:
        info = _transport().article_capabilities(pid, account)
    except Exception:
        return dict(base, supported=False, reason="文章主动读取组件暂不可用，请检查本机原生桥接")
    return dict(base, supported=info.get('supported') is True,
                reason=info.get('reason', '') if isinstance(info.get('reason', ''), str) else '')


class _Guard(AbstractContextManager):
    def __init__(self, pid, account, query):
        self.pid, self.expected = pid, account
        self.platform = _platform()
        self.memory = None
        try:
            self.target = self.platform.target(pid, _Budget())
            self.memory = self.platform.memory(pid)
            self.verify()
            self.owner = self.platform.publisher(self.memory, self.target, account, query) if query is not None else None
            self.identity = self._identity()
            self.verify()
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def _identity(self):
        # FILETIME is obtained through the held read-only process handle. A PID
        # reused by a later WeChat process cannot reuse a pagination session.
        import ctypes as c
        from ctypes.wintypes import FILETIME
        values = [FILETIME() for _ in range(4)]
        function = self.memory.kernel.GetProcessTimes
        function.argtypes = [c.c_void_p, *([c.POINTER(FILETIME)] * 4)]
        function.restype = c.c_int
        if not function(self.memory.handle, *(c.byref(value) for value in values)):
            raise OfficialAccountReadError(_CHANGED, 409)
        created = (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime
        login = _login_snapshot(self.memory.read, self.target.core)
        if not login:
            raise OfficialAccountReadError(_CHANGED, 409)
        return created, self.target.module, self.target.file_identity, login

    def verify(self):
        if (self.platform.account(self.memory, self.target) != self.expected or
                not self.platform.unchanged(self.pid, self.target)):
            raise OfficialAccountReadError(_CHANGED, 409)
        if hasattr(self, 'identity') and self._identity() != self.identity:
            raise OfficialAccountReadError(_CHANGED, 409)

    def __exit__(self, *args):
        if self.memory is not None:
            self.memory.close()
            self.memory = None


@dataclass
class _Sync:
    pid: int
    account: str
    publisher: str
    identity: tuple
    expires: float
    offset: str | None = None
    biz: str | None = None
    pages: int = 0
    urls: set[str] = field(default_factory=set)
    cursor_hashes: set[str] = field(default_factory=set)


def _page(response, owner, expected_biz=None):
    """Project only attributed public metadata from the authenticated CGI."""
    if (not isinstance(response, dict) or not isinstance(response.get('BaseResponse'), dict) or
            type(response['BaseResponse'].get('Ret')) is not int or response['BaseResponse']['Ret'] != 0):
        raise OfficialAccountReadError("微信未能返回文章列表，请稍后重试", 502)
    account = response.get('AccountInfo')
    if not isinstance(account, dict) or account.get('UserName') != owner['username']:
        raise OfficialAccountReadError("返回的公众号来源不一致，已丢弃本页", 409)
    listing = response.get('MsgList')
    if not isinstance(listing, dict) or not isinstance(listing.get('Msg'), list) or len(listing['Msg']) > 128:
        raise OfficialAccountReadError("微信文章目录格式无效，请重新同步", 502)
    paging = listing.get('PagingInfo')
    if (not isinstance(paging, dict) or type(paging.get('IsEnd')) is not int or
            paging['IsEnd'] not in (0, 1)):
        raise OfficialAccountReadError("微信文章分页格式无效，请重新同步", 502)
    more = paging['IsEnd'] == 0
    offset = paging.get('Offset') if more else None
    if more and (not isinstance(offset, str) or not _CURSOR.fullmatch(offset)):
        raise OfficialAccountReadError("微信文章分页已失效，请重新同步", 409)
    biz, rows = expected_biz, {}
    for message in listing['Msg']:
        if not isinstance(message, dict) or not isinstance(message.get('AppMsg'), dict):
            continue
        app = message['AppMsg']
        details = app.get('DetailInfo')
        if not isinstance(details, list) or len(details) > 64:
            raise OfficialAccountReadError("微信文章条目格式无效", 502)
        inherited = _date_fields(message.get('BaseInfo')) or _date_fields(message)
        for detail in details:
            meta = _metadata(detail, fallback=app.get('BaseInfo'), inherited_date=inherited,
                             allow_base_url=len(details) == 1)
            if meta is None:
                continue
            source = url_biz(meta['url'])
            if biz is not None and source != biz:
                raise OfficialAccountReadError("文章链接来源不一致，已丢弃本页", 409)
            biz = source
            rows[meta['url']] = meta
    public_owner = {key: owner.get(key, '') for key in ('username', 'nickname', 'alias')}
    if biz is not None:
        public_owner['biz'] = biz
    return public_owner, list(rows.values()), offset, more


class OfficialAccountSync:
    def __init__(self, *, guard_factory=_Guard, transport_factory=_transport,
                 clock=time.monotonic, sleeper=time.sleep, cache_reader=read_loaded_articles):
        self.guard_factory, self.transport_factory = guard_factory, transport_factory
        self.clock, self.sleep, self.cache_reader = clock, sleeper, cache_reader
        self._states = OrderedDict()
        self._lock = threading.Lock()

    def clear(self):
        with self._lock:
            self._states.clear()

    def _check(self, guard, cancelled):
        if cancelled():
            raise OfficialAccountReadError("文章读取已取消，请重新选择账号", 409)
        guard.verify()

    @staticmethod
    def _call(transport, method, *args, cancelled):
        try:
            return getattr(transport, method)(*args, cancelled=cancelled)
        except Exception as error:
            known = getattr(transport, 'ArticleTransportError', ())
            if known and isinstance(error, known):
                raise OfficialAccountReadError(str(error), getattr(error, 'status', 502)) from None
            raise OfficialAccountReadError("暂时无法通过微信读取文章，请稍后重试", 502) from None

    def list_publishers(self, pid, account, *, cancelled=lambda: False):
        _inputs(pid, account)
        try:
            with self.guard_factory(pid, account, None) as guard:
                self._check(guard, cancelled)
                rows = guard.platform.publishers(guard.memory, guard.target, account)
                self._check(guard, cancelled)
                if not isinstance(rows, list) or len(rows) > 1000:
                    raise ValueError()
                seen, public = set(), []
                for row in rows:
                    username = row.get('username') if isinstance(row, dict) else None
                    if (not isinstance(username, str) or not re.fullmatch(r'gh_[A-Za-z0-9_]{1,124}', username)
                            or username in seen):
                        raise ValueError()
                    item = {'username': username}
                    for key in ('nickname', 'alias'):
                        value = row.get(key, '')
                        if not isinstance(value, str) or len(value.encode('utf-8')) > 256:
                            raise ValueError()
                        item[key] = ''.join(ch for ch in value if ord(ch) >= 32 and ord(ch) != 127).strip()
                    seen.add(username); public.append(item)
                return {'publishers': public, 'source': 'wechat_contacts', 'complete': False}
        except OfficialAccountReadError:
            raise
        except Exception:
            raise OfficialAccountReadError('暂时无法读取关注的公众号，请稍后重试', 409) from None

    def read_page(self, pid, account, publisher, *, continuation=None, cancelled=lambda: False):
        _inputs(pid, account, publisher)
        if continuation is not None and (not isinstance(continuation, str) or not _TOKEN.fullmatch(continuation)):
            raise OfficialAccountReadError("文章同步已失效，请重新同步", 409)
        now = self.clock()
        with self._lock:
            for token in list(self._states):
                if self._states[token].expires <= now:
                    del self._states[token]
            state = self._states.pop(continuation, None) if continuation is not None else None
        if continuation is not None and state is None:
            raise OfficialAccountReadError("文章同步已过期，请重新同步", 409)
        with self.guard_factory(pid, account, publisher) as guard:
            self._check(guard, cancelled)
            owner = guard.owner
            if state is None:
                state = _Sync(pid, account, owner['username'], guard.identity, now + _TOKEN_SECONDS)
            elif (state.pid, state.account, state.publisher, state.identity) != (pid, account, owner['username'], guard.identity):
                raise OfficialAccountReadError(_CHANGED, 409)
            transport = self.transport_factory()
            response = self._call(transport, 'request_page', pid, account, owner['username'], state.offset,
                                  cancelled=cancelled)
            self._check(guard, cancelled)
            public_owner, rows, offset, more = _page(response, owner, state.biz)
            # A cursor is opaque and retained only in this worker's RAM. Its
            # digest detects loops without logging or persisting its value.
            if more:
                digest = hashlib.sha256(offset.encode('ascii')).hexdigest()
                if digest in state.cursor_hashes:
                    raise OfficialAccountReadError("微信文章分页没有前进，请重新同步", 409)
                state.cursor_hashes.add(digest)
            rows = [row for row in rows if row['url'] not in state.urls]
            remaining = MAX_ARTICLES - len(state.urls)
            clipped = len(rows) > remaining
            rows = rows[:remaining]
            state.urls.update(row['url'] for row in rows)
            state.pages += 1
            limited = clipped or more and (len(state.urls) >= MAX_ARTICLES or state.pages >= _MAX_PAGES)
            more = more and not limited
            token = secrets.token_hex(32) if more else None
            state.offset, state.biz, state.expires = offset, public_owner.get('biz'), self.clock() + _TOKEN_SECONDS
            if token is not None:
                with self._lock:
                    self._states[token] = state
                    while len(self._states) > _MAX_SYNCS:
                        self._states.popitem(last=False)
            return {'publisher': public_owner, 'articles': rows, 'has_more': more, 'continuation': token,
                    'complete_remote_history': not more and not limited, 'limit_reached': bool(limited),
                    'page_count': state.pages, 'fetched_count': len(state.urls), 'truncated': bool(limited)}

    def read_article(self, pid, account, publisher, url, *, cancelled=lambda: False):
        _inputs(pid, account, publisher, url)
        url = canonical_url(url)
        deadline = self.clock() + _BODY_SECONDS
        with self.guard_factory(pid, account, publisher) as guard:
            self._check(guard, cancelled)
            try:
                result = self.cache_reader(pid, account, guard.owner['username'], url=url, timeout=4.0)
            except OfficialAccountReadError as error:
                if error.status not in (409,):
                    raise
            else:
                self._check(guard, cancelled)
                return result
            self._check(guard, cancelled)
            self._call(self.transport_factory(), 'open_article', pid, account, guard.owner['username'], url,
                       cancelled=cancelled)
            # The client URL load is issued once. Polling the source-bound cache
            # never retries the navigation, even when loading is slow.
            while self.clock() < deadline:
                self._check(guard, cancelled)
                self.sleep(min(0.3, max(0, deadline - self.clock())))
                try:
                    result = self.cache_reader(pid, account, guard.owner['username'], url=url,
                                               timeout=min(8.0, max(0.05, deadline - self.clock())))
                except OfficialAccountReadError as error:
                    if error.status != 409:
                        raise
                    continue
                self._check(guard, cancelled)
                return result
            raise OfficialAccountReadError("微信尚未加载完成，请稍后在网页重试读取正文", 504)
