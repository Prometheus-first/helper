"""版本锁定、账号隔离的公众号已加载列表/正文读取，不请求网络。"""
from __future__ import annotations

from collections import deque
import copy
from contextlib import ExitStack
from dataclasses import dataclass
from functools import lru_cache
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import threading
import time
import unicodedata

from .account import validate_account
from .official_account_parser import (MAX_SPAN_BYTES, canonical_url, history_articles,
                                      json_spans, publisher_proofs, url_biz)

_SUPPORTED = "4.2.0.11"
_SECONDS = 12.0
_WORK_SECONDS = 10.0
_MAX_BYTES = 256 * 1024 * 1024
_RESERVED_BYTES = 1024 * 1024
_MAX_RENDERERS = 6
_MAX_ARTICLES = 500
_SOURCE_SCAN_BYTES = 128 * 1024 * 1024
_PER_RENDERER_SCAN = 64 * 1024 * 1024
_DIRECTORY_RENDERER_SCAN = 128 * 1024 * 1024
_RANK_RENDERER_SCAN = 6 * 1024 * 1024
_DIAGNOSTICS = threading.local()
_OPEN_ARTICLE = "请先在电脑微信打开该公众号一篇文章再重试"
_OPEN_BODY = "请先在电脑微信打开这篇文章，待加载完成后重试"
_CHANGED = "微信登录账号或窗口归属已变化，请重新选择账号后重试"
_UNSUPPORTED = "当前微信核心指纹的公众号读取尚未适配，仅支持已验证的 4.2.0.11"
_LIMITED = "公众号读取已到单次资源上限，请保持目标公众号页面打开后重试"


class OfficialAccountReadError(RuntimeError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class _Limit(Exception):
    pass


class _Budget:
    def __init__(self, clock=time.monotonic, timeout=_SECONDS):
        self.clock = clock
        self.started = clock()
        self.seconds = timeout
        self.work_seconds = timeout - min(2.0, timeout / 4)
        self.bytes = 0
        self.final = False
        self.truncated = False
        self.phase = "setup"
        self.stats = {"phases": {}, "renderers": {}}

    def check(self):
        seconds = self.seconds if self.final else self.work_seconds
        if self.clock() - self.started >= seconds:
            self.truncated = True
            raise _Limit()

    def charge(self, amount):
        self.check()
        maximum = _MAX_BYTES if self.final else _MAX_BYTES - _RESERVED_BYTES
        if type(amount) is not int or amount <= 0 or self.bytes + amount > maximum:
            self.truncated = True
            raise _Limit()
        self.bytes += amount
        phase = self.stats["phases"].setdefault(self.phase, {"read_bytes": 0, "scan_bytes": 0})
        phase["read_bytes"] += amount

    def remaining(self):
        return max(0.01, self.seconds - (self.clock() - self.started))


@dataclass(frozen=True)
class _Process:
    pid: int
    parent: int
    name: str
    created: str
    render_type: int | None = None


@dataclass(frozen=True)
class _Context:
    main: _Process
    browser: _Process
    renderers: tuple[_Process, ...]
    truncated: bool = False


@dataclass(frozen=True)
class _Target:
    core: int
    profile: object
    module: tuple
    file_identity: tuple


class _BudgetMemory:
    def __init__(self, memory, budget, pid=None):
        self.raw, self.budget, self.pid = memory, budget, pid

    def read(self, address, size):
        self.budget.charge(size)
        if self.pid is not None:
            row = self.budget.stats["renderers"].setdefault(str(self.pid), {})
            key = self.budget.phase + "_read_bytes"
            row[key] = row.get(key, 0) + size
        value = self.raw.read(address, size)
        if len(value) != size:
            raise OSError("incomplete read")
        self.budget.check()
        return value

    def regions(self):
        self.budget.check()
        return [(address, size) for address, size in self.raw.regions(self.budget)
                if type(address) is int and type(size) is int and 0x10000 <= address < address + size < 0x800000000000]

    def close(self):
        self.raw.close()


def _lookup(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _inputs(pid, expected_account, publisher=None, url=None):
    if type(pid) is not int or not 0 < pid <= 0xFFFFFFFF:
        raise OfficialAccountReadError("目标微信进程无效")
    try:
        validate_account(expected_account)
    except ValueError:
        raise OfficialAccountReadError("所选微信账号无效") from None
    if publisher is not None and (not isinstance(publisher, str) or not 1 <= len(publisher.strip()) <= 256 or
                                  any(ord(character) < 32 or ord(character) == 127 for character in publisher)):
        raise OfficialAccountReadError("请输入公众号用户名、微信号或昵称")
    if url is not None and not canonical_url(url):
        raise OfficialAccountReadError("文章链接必须是带完整公开标识的微信公众平台文章链接")


def _select_processes(rows, pid):
    main = next((row for row in rows if row.pid == pid), None)
    browsers = [row for row in rows if row.parent == pid and row.name.casefold() == "wechatappex.exe"
                and row.render_type is None]
    if main is None or main.name.casefold() != "weixin.exe" or not main.created or len(browsers) != 1:
        raise OfficialAccountReadError("无法唯一确认所选微信的公众号浏览器窗口，请先在电脑微信打开公众号", 409)
    browser = browsers[0]
    if not browser.created:
        raise OfficialAccountReadError(_CHANGED, 409)
    renderers = [row for row in rows if row.parent == browser.pid and row.name.casefold() == "wechatappex.exe"
                 and row.render_type in (6, 7) and row.created]
    renderers.sort(key=lambda row: (row.render_type == 6, row.created, row.pid), reverse=True)
    if not renderers:
        raise OfficialAccountReadError(_OPEN_ARTICLE, 409)
    return _Context(main, browser, tuple(renderers[:_MAX_RENDERERS]), len(renderers) > _MAX_RENDERERS)


def _login_snapshot(read, core):
    """The independently checked 4.2.0.11 login getter, without calling it."""
    def field(address, size):
        if not 0x10000 <= address < address + size < 0x800000000000:
            raise ValueError("invalid login object")
        value = read(address, size)
        if len(value) != size:
            raise ValueError("incomplete login object")
        return value

    def pointer(address):
        return struct.unpack("<Q", field(address, 8))[0]

    root = pointer(core + 0xBD0CE30)
    table = pointer(root)
    if table != core + 0x9054B98 or pointer(table + 0x68) != core + 0x4FFD0:
        return None
    if field(core + 0x4FFD0, 7) != b"\x48\x8b\x49\x68\x48\x85\xc9":
        return None
    context = pointer(root + 0x68)
    context_table = pointer(context)
    if context_table != core + 0x90D4F88 or pointer(context_table) != core + 0x38BDB0:
        return None
    if field(core + 0x38BDB0, 7) != b"\x0f\xb6\x41\x38\x24\x01\xc3":
        return None
    flag = field(context + 0x38, 1)[0]
    if not flag & 1:
        return None
    return root, table, context, context_table, flag


@lru_cache(maxsize=8)
def _fingerprint(path, size, modified):
    from native.moments_transport import _core_profile
    result = _core_profile(Path(path))
    stat = Path(path).stat()
    if (stat.st_size, stat.st_mtime_ns) != (size, modified):
        raise OfficialAccountReadError(_CHANGED, 409)
    return result


class _WindowsPlatform:
    """Windows access is delayed; parser and orchestration tests inject fakes."""
    clock = staticmethod(time.monotonic)

    def target(self, pid, budget):
        if sys.platform != "win32":
            raise OfficialAccountReadError("公众号原生读取仅支持 Windows", 501)
        from native.moments_transport import _local_path, _native
        budget.check()
        module = _native().modules(pid).get("weixin.dll")
        if not module:
            raise OfficialAccountReadError("未找到所选微信核心，请重新选择账号", 409)
        path = _local_path(Path(module[1]), suffix={".dll"})
        stat = path.stat()
        profile = _fingerprint(str(path), stat.st_size, stat.st_mtime_ns)
        if profile is None or profile.version != _SUPPORTED:
            raise OfficialAccountReadError(_UNSUPPORTED, 501)
        budget.check()
        return _Target(module[0], profile, tuple(module), (stat.st_size, stat.st_mtime_ns))

    def unchanged(self, pid, target):
        from native.moments_transport import _native
        module = _native().modules(pid).get("weixin.dll")
        if not module or tuple(module) != target.module:
            return False
        stat = Path(module[1]).stat()
        return (stat.st_size, stat.st_mtime_ns) == target.file_identity

    def memory(self, pid):
        from .moments_reader import _Memory
        return _Memory(pid)

    def account(self, memory, target):
        from .account import read_current_account
        if target.profile.version != _SUPPORTED:
            return None
        try:
            before = read_current_account(memory.read, target.core, profiles=(target.profile,))
            if not before:
                return None
            login = _login_snapshot(memory.read, target.core)
            if login is None:
                return None
            after = read_current_account(memory.read, target.core, profiles=(target.profile,))
            if after != before or _login_snapshot(memory.read, target.core) != login:
                return None
            return before
        except (OSError, ValueError, struct.error):
            return None

    def publisher(self, memory, target, expected_account, query):
        from .contacts import _DirectoryReader, _directory_cache, _directory_records
        if self.account(memory, target) != expected_account:
            raise OfficialAccountReadError(_CHANGED, 409)
        reader = _DirectoryReader(memory.read)
        _, cache = _directory_cache(reader, target.core, target.profile.rva)
        matches = []
        for username, record in _directory_records(reader, cache):
            if not username.startswith("gh_"):
                continue
            if query.startswith("gh_") and _lookup(username) != _lookup(query):
                continue
            deleted = struct.unpack("<I", reader.read(record + 0x68, 4))[0]
            if deleted:
                continue
            nickname = reader.string(record + 0xD8, 256)
            alias = reader.string(record + 0x28, 256)
            if _lookup(query) in {_lookup(username), _lookup(nickname), _lookup(alias)}:
                matches.append({"username": username, "nickname": nickname, "alias": alias})
        reader.verify()
        if self.account(memory, target) != expected_account:
            raise OfficialAccountReadError(_CHANGED, 409)
        if len(matches) != 1:
            raise OfficialAccountReadError("当前账号的已加载公众号联系人未找到唯一匹配，请输入准确用户名或微信号", 409)
        return matches[0]

    def publishers(self, memory, target, expected_account):
        from .contacts import _DirectoryReader, _directory_cache, _directory_records
        if self.account(memory, target) != expected_account:
            raise OfficialAccountReadError(_CHANGED, 409)
        budget, reader = _Budget(), _DirectoryReader(memory.read)
        _, cache = _directory_cache(reader, target.core, target.profile.rva)
        rows = []
        for username, record in _directory_records(reader, cache):
            budget.check()
            if not re.fullmatch(r'gh_[A-Za-z0-9_]{1,124}', username):
                continue
            # 4.2.0.11: F6EFE0/F6F160 apply this contact-membership bit in
            # the main directory, alongside the native BE5AF0 gh_ predicate.
            deleted, flags = struct.unpack('<II', reader.read(record + 0x68, 8))
            if deleted or not flags & 1:
                continue
            rows.append({'username': username, 'nickname': reader.string(record + 0xD8, 256).strip(),
                         'alias': reader.string(record + 0x28, 256).strip()})
            if len(rows) > 1000:
                raise OfficialAccountReadError('关注的公众号数量超过单次读取上限', 413)
        reader.verify()
        budget.check()
        if self.account(memory, target) != expected_account:
            raise OfficialAccountReadError(_CHANGED, 409)
        return sorted(rows, key=lambda row: _lookup(row['nickname'] or row['alias'] or row['username']))

    def processes(self, pid, budget):
        budget.check()
        command = ("[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); "
                   f'$taskRoots=@(Get-CimInstance Win32_Process -Filter "ProcessId={pid} OR '
                   f'(ParentProcessId={pid} AND Name=\'WeChatAppEx.exe\')"); '
                   "$taskBrowsers=@($taskRoots | Where-Object { $_.Name -eq 'WeChatAppEx.exe' -and "
                   f"$_.ParentProcessId -eq {pid} -and $_.CommandLine -notmatch '(?:^|\\s)--type=' }}); "
                   "$taskRows=@($taskRoots); "
                   "if($taskBrowsers.Count -eq 1){$taskBrowserId=$taskBrowsers[0].ProcessId; "
                   '$taskRows+=@(Get-CimInstance Win32_Process -Filter "ParentProcessId=$taskBrowserId '
                   "AND Name='WeChatAppEx.exe'\")}; "
                   "$taskRows | ForEach-Object { $taskRole=$null; "
                   "if($_.CommandLine -match '(?:^|\\s)--type=renderer(?:\\s|$)' -and "
                   "$_.CommandLine -match '(?:^|\\s)--wmpf-render-type=(6|7)(?:\\s|$)'){ $taskRole=[int]$Matches[1] }; "
                   "[pscustomobject]@{ProcessId=$_.ProcessId;ParentProcessId=$_.ParentProcessId;Name=$_.Name;"
                   "CreationDate=$_.CreationDate;RenderType=$taskRole;"
                   "IsBrowser=($_.ProcessId -in $taskBrowsers.ProcessId)} } | ConvertTo-Json -Compress")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=min(2.0, budget.remaining()))
        decoded = json.loads(result.stdout.decode("utf-8-sig"))
        decoded = decoded if isinstance(decoded, list) else [decoded]
        browser_ids = {row["ProcessId"] for row in decoded if row.get("ParentProcessId") == pid and
                       str(row.get("Name", "")).casefold() == "wechatappex.exe" and row.get("IsBrowser") is True}
        rows = []
        for row in decoded:
            process_id, parent = row.get("ProcessId"), row.get("ParentProcessId")
            if process_id != pid and process_id not in browser_ids and parent not in browser_ids:
                continue
            if type(process_id) is not int or type(parent) is not int:
                continue
            role = None
            if parent in browser_ids:
                if type(row.get("RenderType")) is not int or row["RenderType"] not in (6, 7):
                    continue
                role = row["RenderType"]
            created = row.get("CreationDate")
            rows.append(_Process(process_id, parent, str(row.get("Name", "")), str(created) if created else "", role))
        budget.check()
        return rows


def _platform():
    return _WindowsPlatform()


def last_diagnostics():
    """Value-free counters from the current thread's most recent native call."""
    return copy.deepcopy(getattr(_DIAGNOSTICS, "value", {}))


def _save_diagnostics(budget):
    _DIAGNOSTICS.value = dict(budget.stats, total_read_bytes=budget.bytes,
                              elapsed_seconds=max(0.0, budget.clock() - budget.started),
                              truncated=budget.truncated)


class _Renderer:
    def __init__(self, process, memory, budget):
        self.process, self.memory, self.budget = process, memory, budget
        self.regions = memory.regions()
        self.windows = {}
        self.seen_markers, self.seen_spans = set(), set()
        self.stats = budget.stats["renderers"].setdefault(str(process.pid), {})
        self.stats["render_type"] = process.render_type

    def chunks(self):
        regions = sorted(self.regions, key=lambda item: (min(item[1], 1024 * 1024), item[0]))
        # Sample every region before walking the remainder of large V8 heaps.
        for base, size in regions:
            yield base, base, min(size, 64 * 1024)
        for base, size in regions:
            for offset in range(64 * 1024, size, 1024 * 1024):
                overlap = 64
                yield base, base + offset - overlap, min(size - offset, 1024 * 1024) + overlap

    def window(self, location, encoding, *, source, wanted=None):
        region = next(((base, size) for base, size in self.regions if base <= location < base + size), None)
        if region is None:
            return None
        base, size = region
        before = 16 * 1024 if source else 4096
        start = max(base, location - before)
        start = max(base, start - start % 4096)
        if encoding == "utf-16le":
            start += (location - start) % 2
        wanted = wanted or (MAX_SPAN_BYTES if source else 1024 * 1024)
        amount = min(wanted, base + size - start)
        for (old_start, old_encoding), raw in self.windows.items():
            if old_encoding == encoding and old_start <= start and old_start + len(raw) >= start + amount:
                return old_start, raw
        raw = self.memory.read(start, amount)
        if len(self.windows) >= 3:
            del self.windows[next(iter(self.windows))]
        self.windows[(start, encoding)] = raw
        return start, raw

    def values(self, location, encoding, *, source, wanted=None):
        try:
            selected = self.window(location, encoding, source=source, wanted=wanted)
        except OSError:
            return
        if selected is None:
            return
        start, raw = selected
        for span in json_spans(raw, location - start, encoding, message_array=not source, check=self.budget.check):
            address = start + span.offset
            self.stats["json_spans"] = self.stats.get("json_spans", 0) + 1
            key = (address, len(span.payload), encoding, source)
            if key in self.seen_spans:
                continue
            self.seen_spans.add(key)
            try:
                stable = self.memory.read(address, len(span.payload)) == span.payload
            except OSError:
                continue
            if stable:
                self.stats["stable_spans"] = self.stats.get("stable_spans", 0) + 1
                yield span.value


def _source_hint(block, offset, needle, encoding):
    before = max(0, offset - 160)
    if encoding == "utf-16le":
        before += (offset - before) % 2
    fragment = block[before:offset + len(needle) + 8]
    if encoding == "utf-16le":
        fragment = fragment[:len(fragment) // 2 * 2]
    text = fragment.decode(encoding, "replace").replace('\\"', '"').replace('\\"', '"')
    return bool(re.search(r'"user_name"\s*:\s*"', text))


def _msg_hint(block, offset, encoding):
    fragment = block[offset:offset + 256]
    if encoding == "utf-16le":
        fragment = fragment[:len(fragment) // 2 * 2]
    return bool(re.match(r'^"Msg"\s*:\s*\[', fragment.decode(encoding, "replace")))


def _rank_directory(renderers, username, budget):
    budget.phase = "rank"
    for renderer in renderers:
        consumed = 0
        for _, address, amount in renderer.chunks():
            if consumed + amount > _RANK_RENDERER_SCAN:
                break
            try:
                block = renderer.memory.read(address, amount)
            except OSError:
                consumed += amount
                continue
            consumed += amount
            for encoding in ("utf-8", "utf-16le"):
                renderer.stats["target_gh_hits"] = renderer.stats.get("target_gh_hits", 0) + block.count(username.encode(encoding))
                route = ("SubscriptionProfile/profile.html?userName=" + username).encode(encoding)
                renderer.stats["target_profile_route_hits"] = renderer.stats.get("target_profile_route_hits", 0) + block.count(route)
        renderer.stats["rank_scan_bytes"] = consumed
    return sorted(renderers, key=lambda item: (item.stats.get("target_profile_route_hits", 0),
                                               item.stats.get("target_gh_hits", 0)), reverse=True)


def _scan(renderers, markers, handle, budget, *, source=False, scan_limit=None, per_renderer_limit=_PER_RENDERER_SCAN):
    active = deque((renderer, iter(renderer.chunks()), 0) for renderer in renderers)
    scanned = 0
    while active:
        budget.check()
        renderer, chunks, consumed = active.popleft()
        if consumed >= per_renderer_limit:
            budget.truncated = True
            continue
        try:
            _, address, amount = next(chunks)
        except StopIteration:
            continue
        if scan_limit is not None and scanned + amount > scan_limit:
            budget.truncated = True
            return
        try:
            block = renderer.memory.read(address, amount)
        except OSError:
            active.append((renderer, chunks, consumed + amount))
            scanned += amount
            continue
        consumed += amount
        scanned += amount
        phase = budget.stats["phases"].setdefault(budget.phase, {"read_bytes": 0, "scan_bytes": 0})
        phase["scan_bytes"] += amount
        renderer.stats[budget.phase + "_scan_bytes"] = consumed
        for marker in markers:
            for encoding in ("utf-8", "utf-16le"):
                needle = marker.encode(encoding)
                cursor = 0
                while True:
                    found = block.find(needle, cursor)
                    if found < 0:
                        break
                    cursor = found + len(needle)
                    key = (address + found, encoding, source)
                    if key in renderer.seen_markers:
                        continue
                    renderer.seen_markers.add(key)
                    if source and not _source_hint(block, found, needle, encoding):
                        continue
                    if not source:
                        renderer.stats["msg_markers"] = renderer.stats.get("msg_markers", 0) + 1
                        if not _msg_hint(block, found, encoding):
                            continue
                        renderer.stats["msg_field_markers"] = renderer.stats.get("msg_field_markers", 0) + 1
                    sizes = (64 * 1024, 256 * 1024, MAX_SPAN_BYTES if source else 1024 * 1024)
                    for wanted in sizes:
                        useful = False
                        for value in renderer.values(address + found, encoding, source=source, wanted=wanted):
                            stop, relevant = handle(value)
                            useful = useful or relevant
                            if stop:
                                return
                        if useful:
                            renderer.stats["useful_markers"] = renderer.stats.get("useful_markers", 0) + 1
                            break
        active.append((renderer, chunks, consumed))


def _verify(platform, pid, expected_account, target, main_memory, budget, context=None):
    budget.final = True
    budget.phase = "verify"
    budget.check()
    if platform.account(main_memory, target) != expected_account or not platform.unchanged(pid, target):
        raise OfficialAccountReadError(_CHANGED, 409)
    if context is not None and _select_processes(platform.processes(pid, budget), pid) != context:
        raise OfficialAccountReadError(_CHANGED, 409)
    budget.check()


def capabilities(pid, expected_account):
    """Only fingerprint/account guards; no contact or renderer enumeration."""
    _inputs(pid, expected_account)
    platform = _platform()
    budget = _Budget(platform.clock)
    try:
        target = platform.target(pid, budget)
        if target.profile.version != _SUPPORTED:
            raise OfficialAccountReadError(_UNSUPPORTED, 501)
        with ExitStack() as stack:
            memory = _BudgetMemory(platform.memory(pid), budget, pid)
            stack.callback(memory.close)
            if platform.account(memory, target) != expected_account:
                raise OfficialAccountReadError(_CHANGED, 409)
            _verify(platform, pid, expected_account, target, memory, budget)
        return {"supported": True, "version": _SUPPORTED, "reason": ""}
    except OfficialAccountReadError:
        raise
    except _Limit:
        raise OfficialAccountReadError(_LIMITED, 409) from None
    except (OSError, ValueError, struct.error, ImportError, AttributeError, subprocess.SubprocessError):
        raise OfficialAccountReadError("暂时无法核对所选微信的公众号读取能力", 409) from None
    finally:
        _save_diagnostics(budget)


def read_loaded_articles(pid, expected_account, publisher, *, url=None, timeout=_SECONDS):
    """Read only existing account-owned renderer responses, with no navigation.

    content_html is returned only for an explicitly requested, source-bound
    canonical article. It is intended for the storage/conversion layer and must
    never be rendered directly by an HTTP endpoint.
    """
    _inputs(pid, expected_account, publisher, url)
    if type(timeout) not in (int, float) or not 0.05 <= timeout <= _SECONDS:
        raise OfficialAccountReadError("文章缓存读取时限无效")
    if not isinstance(publisher, str) or not publisher.strip():
        raise OfficialAccountReadError("请输入公众号用户名、微信号或昵称")
    requested = canonical_url(url) if url is not None else None
    platform = _platform()
    budget = _Budget(platform.clock, timeout)
    biz, body = None, None
    articles = {}
    try:
        target = platform.target(pid, budget)
        if target.profile.version != _SUPPORTED:
            raise OfficialAccountReadError(_UNSUPPORTED, 501)
        with ExitStack() as stack:
            main_memory = _BudgetMemory(platform.memory(pid), budget, pid)
            stack.callback(main_memory.close)
            if platform.account(main_memory, target) != expected_account:
                raise OfficialAccountReadError(_CHANGED, 409)
            owner = platform.publisher(main_memory, target, expected_account, publisher.strip())
            if (not isinstance(owner, dict) or not isinstance(owner.get("username"), str) or
                    not re.fullmatch(r"gh_[A-Za-z0-9_.-]{1,124}", owner["username"])):
                raise OfficialAccountReadError("公众号联系人身份未核对，请重新选择公众号", 409)
            owner = {key: owner.get(key, "") for key in ("username", "nickname", "alias")}
            context = _select_processes(platform.processes(pid, budget), pid)
            budget.truncated = context.truncated
            renderers = []
            for process in context.renderers:
                memory = _BudgetMemory(platform.memory(process.pid), budget, process.pid)
                stack.callback(memory.close)
                renderers.append(_Renderer(process, memory, budget))

            def source_value(value):
                nonlocal biz, body
                relevant = False
                for proof in publisher_proofs(value, owner["username"], requested_url=requested, check=budget.check):
                    relevant = True
                    if biz is not None and proof.biz != biz:
                        raise OfficialAccountReadError("已加载公众号响应的来源不一致，请重新打开目标文章后重试", 409)
                    biz = proof.biz
                    if requested and url_biz(requested) != biz:
                        raise OfficialAccountReadError("文章链接不属于所选公众号", 409)
                    if proof.article:
                        articles[proof.article["url"]] = proof.article
                    if proof.content_html is not None and proof.article and proof.article["url"] == requested:
                        body = dict(proof.article, content_html=proof.content_html)
                return (body is not None if requested else biz is not None), relevant

            try:
                budget.phase = "source"
                source_renderers = [renderer for renderer in renderers if renderer.process.render_type == 6]
                _scan(source_renderers, [owner["username"]], source_value, budget, source=True, scan_limit=_SOURCE_SCAN_BYTES)
                if biz is not None and not requested:
                    def list_value(value):
                        rows = history_articles(value, biz, check=budget.check)
                        for row in rows:
                            articles[row["url"]] = row
                            if len(articles) >= _MAX_ARTICLES:
                                budget.truncated = True
                                return True, True
                        return False, bool(rows)

                    directory_renderers = [renderer for renderer in renderers if renderer.process.render_type == 7]
                    directory_renderers = _rank_directory(directory_renderers, owner["username"], budget)
                    budget.phase = "directory"
                    # Do not spend the whole allowance evenly on unrelated pages.
                    for renderer in directory_renderers:
                        _scan([renderer], ['"Msg"'], list_value, budget, per_renderer_limit=_DIRECTORY_RENDERER_SCAN)
                        if len(articles) >= _MAX_ARTICLES:
                            break
            except _Limit:
                budget.truncated = True
            _verify(platform, pid, expected_account, target, main_memory, budget, context)
            if biz is None:
                raise OfficialAccountReadError(_OPEN_ARTICLE, 409)
            public_owner = dict(owner, biz=biz)
            if requested:
                if body is None:
                    raise OfficialAccountReadError(_OPEN_BODY, 409)
                selected = {key: body[key] for key in ("title", "url", "published_at")}
                return {"publisher": public_owner, "articles": [selected], "complete_remote_history": False,
                        "truncated": budget.truncated, "article": dict(body, publisher=public_owner)}
            if not articles:
                raise OfficialAccountReadError("请先在电脑微信打开该公众号历史列表，待加载完成后重试", 409)
            ordered = sorted(articles.values(), key=lambda row: (row["published_at"] or "", row["url"]), reverse=True)
            return {"publisher": public_owner, "articles": ordered[:_MAX_ARTICLES],
                    "complete_remote_history": False, "truncated": budget.truncated or len(ordered) > _MAX_ARTICLES}
    except OfficialAccountReadError:
        raise
    except _Limit:
        raise OfficialAccountReadError(_LIMITED, 409) from None
    except (OSError, ValueError, struct.error, ImportError, AttributeError, subprocess.SubprocessError):
        raise OfficialAccountReadError("暂时无法读取所选公众号，请保持电脑微信页面打开后重试", 409) from None
    finally:
        _save_diagnostics(budget)
