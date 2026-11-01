"""账号隔离的本地只读文章库与受限公开图片服务，不保存原始 HTML 或会话凭据。

文章 ID 是规范化公开文章 URL 的完整 SHA256（64 位小写十六进制）。
这里提供导入、阅读和 Markdown 导出；没有修改公众号原文或本地草稿接口。
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime
from functools import wraps
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MAX_ARTICLES = 500
MAX_ACCOUNT_ARTICLES = 5000
MAX_LIST_BYTES = 3 * 1024 * 1024
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_MARKDOWN_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_IMAGES = 100
MAX_ACCOUNT_BYTES = 128 * 1024 * 1024
MAX_RECORD_BYTES = 2 * 1024 * 1024
MAX_CACHED_IMAGES = 512
_ID = re.compile(r"[a-f0-9]{64}\Z")
_IMAGE_HOSTS = {"mmbiz.qpic.cn", "mmbiz.qlogo.cn"}
_PUBLIC_PARAMETERS = ("__biz", "mid", "idx", "sn")
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class ArticlesError(RuntimeError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _unescape(value):
    for _ in range(2):
        decoded = unescape(value)
        if decoded == value:
            break
        value = decoded
    return value


def normalize_article_url(value):
    """纯函数：公开 long link 四参数规范化；非法或缺参数时返回 None。"""
    if not isinstance(value, str) or len(value) > 8192:
        return None
    value = _unescape(value).strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    if value.startswith("//"):
        value = "https:" + value
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or parts.hostname != "mp.weixin.qq.com" or
                parts.username is not None or parts.password is not None or parts.path != "/s" or
                parts.port not in (None, 80 if parts.scheme == "http" else 443)):
            return None
        identity = {}
        for key, item in parse_qsl(parts.query, keep_blank_values=True, max_num_fields=128):
            if key not in _PUBLIC_PARAMETERS:
                continue
            if key in identity and identity[key] != item:
                return None
            identity[key] = item
        if set(identity) != set(_PUBLIC_PARAMETERS):
            return None
        if (not re.fullmatch(r"[A-Za-z0-9+/=_-]{1,256}", identity["__biz"]) or
                not re.fullmatch(r"[0-9]{1,24}", identity["mid"]) or int(identity["mid"]) <= 0 or
                not re.fullmatch(r"[0-9]{1,5}", identity["idx"]) or int(identity["idx"]) <= 0 or
                not re.fullmatch(r"[a-fA-F0-9]{32}", identity["sn"])):
            return None
        identity["mid"] = str(int(identity["mid"]))
        identity["idx"] = str(int(identity["idx"]))
        identity["sn"] = identity["sn"].lower()
        return "https://mp.weixin.qq.com/s?" + urlencode([(key, identity[key]) for key in _PUBLIC_PARAMETERS])
    except (ValueError, OverflowError):
        return None


def _image_url(value):
    if not isinstance(value, str) or len(value) > 8192:
        return None
    value = _unescape(value).strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    if value.startswith("//"):
        value = "https:" + value
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or parts.hostname not in _IMAGE_HOSTS or
                parts.username is not None or parts.password is not None or
                parts.port not in (None, 80 if parts.scheme == "http" else 443) or
                not re.fullmatch(r"/[A-Za-z0-9_/%.-]{1,2048}", parts.path)):
            return None
        # 编码后仍拒绝目录跳转、反斜线和控制字符；CDN 路径无需这些字符。
        from urllib.parse import unquote
        decoded = unquote(parts.path)
        if "\\" in decoded or any(char in {".", ".."} for char in decoded.split("/")) or any(ord(char) < 32 for char in decoded):
            return None
        formats = []
        for key, item in parse_qsl(parts.query, keep_blank_values=True, max_num_fields=64):
            if key == "wx_fmt":
                formats.append(item)
        if len(set(formats)) > 1:
            return None
        fmt = formats[0] if formats else None
        if fmt is not None and fmt not in {"png", "jpeg", "jpg", "gif", "webp"}:
            return None
        return urlunsplit(("https", parts.hostname, parts.path, urlencode({"wx_fmt": fmt}) if fmt else "", ""))
    except ValueError:
        return None


def _link_url(value):
    if not isinstance(value, str) or len(value) > 8192:
        return None
    value = _unescape(value).strip()
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    article = normalize_article_url(value)
    if article:
        return article
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname or parts.username is not None or
                parts.password is not None or parts.port not in (None, 80 if parts.scheme == "http" else 443)):
            return None
        # 非公众号链接只保留公开主机/路径，不传播查询参数或 fragment。
        if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", parts.hostname) or any(char in parts.path for char in "()<>\\"):
            return None
        return urlunsplit((parts.scheme, parts.hostname, parts.path or "/", "", ""))
    except ValueError:
        return None


def _text(value, maximum, label, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or "\0" in value:
        raise ArticlesError(label + "无效")
    try:
        if len(value.encode("utf-8")) > maximum:
            raise ArticlesError(label + "超过大小限制", 413)
    except UnicodeError:
        raise ArticlesError(label + "编码无效") from None
    if any(ord(char) < 32 and char not in "\r\n\t" for char in value):
        raise ArticlesError(label + "包含控制字符")
    return value


def _md_text(value):
    value = value.replace("\\", "\\\\").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\[\]()*_`#!])", r"\\\1", value)


class _Markdown(HTMLParser):
    _hidden = {"script", "style", "noscript", "iframe", "object", "svg", "math", "template", "head"}
    _blocks = {"p", "div", "section", "article", "blockquote", "ul", "ol", "table", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.images = []
        self.hidden = []
        self.links = []
        self.events = 0

    def tick(self):
        self.events += 1
        if self.events > 50000:
            raise ArticlesError("文章 HTML 结构超过处理上限", 413)

    def handle_starttag(self, tag, attrs):
        self.tick()
        if tag in self._hidden:
            self.hidden.append(tag)
            return
        if self.hidden:
            return
        attrs = dict(attrs)
        if tag in self._blocks:
            self.parts.append("\n\n")
        elif re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag == "br":
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in {"td", "th"}:
            self.parts.append(" ")
        elif tag == "img":
            url = _image_url(attrs.get("data-src")) or _image_url(attrs.get("src"))
            if url:
                if len(self.images) >= MAX_IMAGES:
                    raise ArticlesError("文章图片数量超过上限", 413)
                index = len(self.images) + 1
                self.images.append({"index": index, "url": url, "alt": "图片 " + str(index)})
                self.parts.append(f"\n\n![图片 {index}](article-image:{index})\n\n")
        elif tag == "a":
            url = _link_url(attrs.get("href"))
            self.links.append(url)
            if url:
                self.parts.append("[")

    def handle_startendtag(self, tag, attrs):
        if tag in self._hidden:
            self.tick()
            return
        self.handle_starttag(tag, attrs)
        if tag not in {"img", "br", "hr", "input", "meta", "link"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        self.tick()
        if self.hidden:
            if tag in self.hidden:
                index = len(self.hidden) - 1 - self.hidden[::-1].index(tag)
                del self.hidden[index:]
            return
        if tag in self._blocks or re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n\n")
        elif tag == "a" and self.links:
            url = self.links.pop()
            if url:
                self.parts.append("](" + url + ")")

    def handle_data(self, value):
        self.tick()
        if not self.hidden:
            self.parts.append(_md_text(value))


def _markdown(html):
    _text(html, MAX_HTML_BYTES, "文章正文")
    parser = _Markdown()
    try:
        parser.feed(html)
        parser.close()
    except ArticlesError:
        raise
    except Exception:
        raise ArticlesError("文章 HTML 无法安全转换", 422) from None
    while parser.links:
        if parser.links.pop():
            parser.parts.append("]")
    result = re.sub(r"\n[ \t]+", "\n", "".join(parser.parts).replace("\r\n", "\n").replace("\r", "\n"))
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    if not result:
        raise ArticlesError("文章正文尚未加载或没有可读取内容", 422)
    _text(result, MAX_MARKDOWN_BYTES, "文章 Markdown")
    return result, parser.images


def _publisher(value):
    if not isinstance(value, dict):
        raise ArticlesError("公众号信息无效")
    username = value.get("username", "")
    if not isinstance(username, str) or not re.fullmatch(r"gh_[A-Za-z0-9_]{1,124}", username):
        raise ArticlesError("公众号标识无效")
    result = {"username": username}
    for name, maximum in (("nickname", 512), ("alias", 256), ("biz", 256)):
        result[name] = _text(value.get(name, ""), maximum, "公众号字段", empty=True).strip()
    if not re.fullmatch(r"[A-Za-z0-9+/=_-]{1,256}", result["biz"]):
        raise ArticlesError("公众号公开标识无效")
    return result


def _metadata(value, publisher):
    if not isinstance(value, dict):
        raise ArticlesError("文章元数据无效")
    url = normalize_article_url(value.get("url"))
    if not url or dict(parse_qsl(urlsplit(url).query))["__biz"] != publisher["biz"]:
        raise ArticlesError("文章链接与公众号不一致")
    title = _text(value.get("title"), 2048, "文章标题").strip()
    published = value.get("published_at")
    if published is not None:
        _text(published, 64, "文章日期")
        try:
            moment = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if moment.utcoffset() is None:
                raise ValueError()
        except ValueError:
            raise ArticlesError("文章日期必须含时区") from None
    return {"id": hashlib.sha256(url.encode("utf-8")).hexdigest(), "title": title, "url": url,
            "published_at": published, "publisher": publisher}


def _absolute(value):
    try:
        value = os.fspath(value)
    except TypeError:
        raise ArticlesError("文章库工作目录无效") from None
    if not isinstance(value, str) or not value.strip() or "\0" in value or ".." in Path(value).parts:
        raise ArticlesError("文章库工作目录无效")
    return Path(os.path.abspath(value))


def _safe(path, *, directory=None):
    information = None
    for component in (*reversed(path.parents), path):
        information = component.lstat()
        if stat.S_ISLNK(information.st_mode) or getattr(information, "st_file_attributes", 0) & _REPARSE:
            raise ArticlesError("文章库路径包含链接或重解析点", 409)
        if component != path and not stat.S_ISDIR(information.st_mode):
            raise ArticlesError("文章库目录无效", 409)
    if directory is True and not stat.S_ISDIR(information.st_mode):
        raise ArticlesError("文章库目录无效", 409)
    if directory is False and (not stat.S_ISREG(information.st_mode) or information.st_nlink != 1):
        raise ArticlesError("文章库文件不是独立普通文件", 409)
    return information


def _mkdir(path):
    for component in (*reversed(path.parents), path):
        try:
            _safe(component, directory=True)
        except FileNotFoundError:
            try:
                component.mkdir()
            except FileExistsError:
                pass
        _safe(component, directory=True)
    if path.resolve(strict=True) != path:
        raise ArticlesError("文章库目录发生重定向", 409)


def _read(path, maximum):
    before = _safe(path, directory=False)
    if not 0 < before.st_size <= maximum:
        raise ArticlesError("文章缓存大小无效，请重新读取", 409)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino) or opened.st_nlink != 1:
            raise ArticlesError("文章缓存已改变，请重试", 409)
        data = source.read(maximum + 1)
        after = os.fstat(source.fileno())
    final = _safe(path, directory=False)
    if (len(data) != opened.st_size or len(data) > maximum or
            (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns) or
            (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns) !=
            (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)):
        raise ArticlesError("文章缓存已改变，请重试", 409)
    return data


def _api(function):
    @wraps(function)
    def call(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ArticlesError:
            raise
        except (OSError, ValueError, UnicodeError, RuntimeError):
            raise ArticlesError("文章库暂时无法读写，请重试", 500) from None
    return call


@contextmanager
def _file_lock(directory):
    path = directory / ".lock"
    if path.exists() or path.is_symlink():
        _safe(path, directory=False)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as stream:
        _safe(path, directory=False)
        if not os.fstat(stream.fileno()).st_size:
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + 5
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise ArticlesError("文章库正在处理其他请求，请稍后重试", 409) from None
                time.sleep(0.01)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class Articles:
    def __init__(self, workspace):
        self.workspace = _absolute(workspace)
        try:
            _safe(self.workspace, directory=True)
        except OSError:
            raise ArticlesError("文章库工作目录不存在", 500) from None
        self.root = self.workspace / ".runtime" / "articles"

    @contextmanager
    def _locked(self, account):
        account = _text(account, 512, "文章所属账号")
        namespace = hashlib.sha256(account.encode("utf-8")).hexdigest()
        directory = self.root / namespace
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault(str(directory), threading.RLock())
        with lock:
            _mkdir(directory)
            with _file_lock(directory):
                _safe(directory, directory=True)
                yield directory

    def _identifier(self, identifier):
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier):
            raise ArticlesError("文章 ID 无效")
        return identifier

    def _load(self, directory, identifier):
        path = directory / (self._identifier(identifier) + ".json")
        try:
            data = json.loads(_read(path, MAX_RECORD_BYTES))
        except FileNotFoundError:
            raise ArticlesError("当前账号没有这篇文章", 404) from None
        except (ValueError, UnicodeError):
            raise ArticlesError("文章缓存已失效，请重新读取", 409) from None
        if not isinstance(data, dict) or data.get("version") != 1:
            raise ArticlesError("文章缓存版本无效，请重新读取", 409)
        meta = _metadata(data, _publisher(data.get("publisher")))
        if meta["id"] != identifier:
            raise ArticlesError("文章缓存标识不匹配", 409)
        markdown = data.get("markdown")
        if markdown is not None:
            _text(markdown, MAX_MARKDOWN_BYTES, "文章 Markdown", empty=True)
        images = data.get("images", [])
        if not isinstance(images, list) or len(images) > MAX_IMAGES:
            raise ArticlesError("文章图片缓存无效", 409)
        sanitized = []
        for index, image in enumerate(images, 1):
            if not isinstance(image, dict) or image.get("index") != index or not _image_url(image.get("url")):
                raise ArticlesError("文章图片缓存无效", 409)
            sanitized.append({"index": index, "url": _image_url(image["url"]), "alt": "图片 " + str(index)})
        return {**meta, "markdown": markdown, "images": sanitized}

    def _brief(self, row):
        return {key: row[key] for key in ("id", "title", "url", "published_at", "publisher")} | {"has_content": row["markdown"] is not None}

    def _records(self, directory):
        paths = []
        for entry in directory.iterdir():
            _safe(entry)
            if re.fullmatch(r"[a-f0-9]{64}\.json", entry.name):
                paths.append(entry)
                if len(paths) > MAX_ACCOUNT_ARTICLES:
                    raise ArticlesError("当前账号文章数量超过上限", 507)
        return paths

    def _usage(self, directory):
        total, count = 0, 0
        for entry in directory.iterdir():
            information = _safe(entry)
            children = entry.iterdir() if stat.S_ISDIR(information.st_mode) and entry.name == "media" else (entry,)
            for child in children:
                info = _safe(child, directory=False)
                total += info.st_size
                count += 1
                if count > MAX_ACCOUNT_ARTICLES + MAX_CACHED_IMAGES + 32:
                    raise ArticlesError("文章库文件数量超过上限", 507)
        return total

    def _write(self, directory, destination, data):
        _safe(destination.parent, directory=True)
        if destination.parent not in (directory, directory / "media"):
            raise ArticlesError("文章缓存写入位置无效", 409)
        existing = _safe(destination, directory=False).st_size if destination.exists() or destination.is_symlink() else 0
        if self._usage(directory) - existing + len(data) > MAX_ACCOUNT_BYTES:
            raise ArticlesError("当前账号文章缓存空间已满", 507)
        descriptor, name = tempfile.mkstemp(prefix=".part-", dir=destination.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _safe(directory, directory=True)
            _safe(destination.parent, directory=True)
            if destination.parent.resolve(strict=True) != destination.parent:
                raise ArticlesError("文章缓存目录发生重定向", 409)
            if destination.exists() or destination.is_symlink():
                _safe(destination, directory=False)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)

    def _save(self, directory, row):
        value = {"version": 1, **row}
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_RECORD_BYTES:
            raise ArticlesError("文章缓存超过单篇大小上限", 413)
        self._write(directory, directory / (row["id"] + ".json"), encoded)

    @_api
    def list(self, account, publisher=None):
        if publisher is not None and (not isinstance(publisher, str) or
                not re.fullmatch(r"gh_[A-Za-z0-9_]{1,124}", publisher)):
            raise ArticlesError("请选择有效的公众号标识")
        with self._locked(account) as directory:
            rows = []
            for path in self._records(directory):
                row = self._load(directory, path.stem)
                if publisher is None or row['publisher']['username'] == publisher:
                    rows.append(self._brief(row))
        rows.sort(key=lambda row: (row["published_at"] or "", row["url"]), reverse=True)
        listing = {"articles": rows, "complete_remote_history": False}
        if len(json.dumps(listing, ensure_ascii=False).encode('utf-8')) > MAX_LIST_BYTES:
            raise ArticlesError("文章目录超过响应上限，请按公众号读取", 413)
        return listing

    @_api
    def import_list(self, account, result, *, allow_partial=False):
        if not isinstance(result, dict) or not isinstance(result.get("articles"), list):
            raise ArticlesError("文章列表结果无效")
        publisher = _publisher(result.get("publisher"))
        if len(result["articles"]) > MAX_ARTICLES:
            raise ArticlesError("本次文章条数超过上限", 413)
        items = {_metadata(value, publisher)["id"]: _metadata(value, publisher) for value in result["articles"]}
        with self._locked(account) as directory:
            existing = {path.stem for path in self._records(directory)}
            if not allow_partial and len(existing | set(items)) > MAX_ACCOUNT_ARTICLES:
                raise ArticlesError("当前账号文章数量达到上限", 507)
            previous_rows = {identifier: self._load(directory, identifier) for identifier in items if identifier in existing}
            if any(row['publisher']['username'] != publisher['username'] for row in previous_rows.values()):
                raise ArticlesError("文章链接已属于其他公众号，已保留原内容", 409)
            rows = []
            remaining = MAX_ACCOUNT_ARTICLES - len(existing)
            capacity_limited = False
            for identifier, meta in items.items():
                if identifier not in existing:
                    if remaining <= 0:
                        capacity_limited = True
                        continue
                    remaining -= 1
                previous = previous_rows.get(identifier, {"markdown": None, "images": []})
                row = {**meta, "markdown": previous["markdown"], "images": previous["images"]}
                self._save(directory, row)
                rows.append(self._brief(row))
        imported = {"publisher": publisher, "articles": rows, "complete_remote_history": False,
                    "truncated": result.get("truncated") is True}
        if allow_partial:
            imported['capacity_limited'] = capacity_limited
        return imported

    @_api
    def import_article(self, account, result):
        if not isinstance(result, dict) or not isinstance(result.get("article"), dict):
            raise ArticlesError("文章正文结果无效")
        article = result["article"]
        publisher = _publisher(article.get("publisher", result.get("publisher")))
        meta = _metadata(article, publisher)
        markdown, images = _markdown(article.get("content_html"))
        row = {**meta, "markdown": markdown, "images": images}
        with self._locked(account) as directory:
            existing = {path.stem for path in self._records(directory)}
            if row["id"] not in existing and len(existing) >= MAX_ACCOUNT_ARTICLES:
                raise ArticlesError("当前账号文章数量达到上限", 507)
            if row["id"] in existing and self._load(directory, row["id"])['publisher']['username'] != publisher['username']:
                raise ArticlesError("文章链接已属于其他公众号，已保留原内容", 409)
            self._save(directory, row)
        return {**row, "has_content": True}

    @_api
    def get(self, account, identifier):
        self._identifier(identifier)
        with self._locked(account) as directory:
            row = self._load(directory, identifier)
            return {**row, "markdown": row["markdown"] or "", "has_content": row["markdown"] is not None}

    @_api
    def export(self, account, identifier):
        row = self.get(account, identifier)
        if not row["has_content"]:
            raise ArticlesError("请先读取这篇文章的正文", 409)
        images = {image["index"]: image["url"] for image in row["images"]}
        markdown = re.sub(r"!\[图片 ([0-9]+)\]\(article-image:([0-9]+)\)",
                          lambda found: f"![图片 {found[1]}]({images[int(found[2])]})" if int(found[2]) in images else "", row["markdown"])
        publisher = row["publisher"]["nickname"] or row["publisher"]["alias"] or row["publisher"]["username"]
        heading = ("# " + _md_text(row["title"]) + "\n\n公众号：" + _md_text(publisher) +
                   ("\n\n时间：" + _md_text(row["published_at"]) if row["published_at"] else "") +
                   "\n\n[原文链接](" + row["url"] + ")\n\n")
        filename = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", row["title"]).strip(" .")[:80] or "article-" + identifier[:12]
        return {"filename": filename + ".md", "markdown": heading + markdown}

    @_api
    def image(self, account, identifier, index):
        self._identifier(identifier)
        if type(index) is not int or not 1 <= index <= MAX_IMAGES:
            raise ArticlesError("文章图片序号无效")
        with self._locked(account) as directory:
            row = self._load(directory, identifier)
            if index > len(row["images"]):
                raise ArticlesError("这篇文章没有该图片", 404)
            url = row["images"][index - 1]["url"]
            media = directory / "media"
            _mkdir(media)
            path = media / (hashlib.sha256(url.encode()).hexdigest() + ".img")
            if path.exists() or path.is_symlink():
                data = _read(path, MAX_IMAGE_BYTES)
                mime = _image_info(data)
            else:
                if sum(1 for _ in media.iterdir()) >= MAX_CACHED_IMAGES:
                    raise ArticlesError("文章图片缓存数量达到上限", 507)
                data, declared = _download_image(url)
                mime = _image_info(data, declared)
                self._write(directory, path, data)
        return {"mime": mime, "data_base64": base64.b64encode(data).decode("ascii")}


def _image_info(data, declared=None):
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
        raise ArticlesError("文章图片超过 2 MiB 或文件为空", 413)
    from io import BytesIO
    import warnings
    from PIL import Image
    types = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}
    if declared is not None and declared not in set(types.values()) | {"image/jpg"}:
        raise ArticlesError("图片 CDN 没有返回支持的图片类型", 502)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as probe:
                kind = probe.format
                if kind not in types or not 0 < probe.width <= 8192 or not 0 < probe.height <= 8192:
                    raise ValueError()
                if probe.width * probe.height > 20_000_000:
                    raise ValueError()
                probe.verify()
            total = 0
            with Image.open(BytesIO(data)) as image:
                for frame in range(101):
                    try:
                        image.seek(frame)
                    except EOFError:
                        break
                    total += image.width * image.height
                    if frame == 100 or total > 40_000_000:
                        raise ValueError()
                    image.load()
    except Exception:
        raise ArticlesError("图片无效或超过像素、动画帧处理上限", 422) from None
    mime = types[kind]
    if declared is not None and declared.replace("image/jpg", "image/jpeg") != mime:
        raise ArticlesError("图片内容与 CDN 声明类型不一致", 502)
    return mime


def _download_image(url):
    # 延迟导入：native reader 只复用规范化函数时不加载网络栈/Pillow。
    from urllib.parse import urljoin
    from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
    deadline = time.monotonic() + 10

    class Redirects(HTTPRedirectHandler):
        def __init__(self):
            self.count = 0

        def redirect_request(self, request, fp, code, message, headers, newurl):
            self.count += 1
            if self.count > 3 or time.monotonic() >= deadline:
                raise ArticlesError("图片跳转次数或读取时间超过限制", 504)
            target = urljoin(request.full_url, newurl)
            canonical = _image_url(target)
            if urlsplit(target).scheme != "https" or not canonical:
                raise ArticlesError("图片跳转超出允许的公开 CDN", 502)
            request.timeout = min(5, max(0.1, deadline - time.monotonic()))
            return super().redirect_request(request, fp, code, message, headers, canonical)

    try:
        canonical = _image_url(url)
        if not canonical or urlsplit(url).scheme != "https":
            raise ArticlesError("文章图片地址无效", 400)
        opener = build_opener(ProxyHandler({}), Redirects())
        request = Request(canonical, headers={"User-Agent": "HelperArticleReader/1", "Accept": "image/png,image/jpeg,image/gif,image/webp", "Accept-Encoding": "identity"})
        with opener.open(request, timeout=5) as response:
            final = response.geturl()
            if response.status != 200 or urlsplit(final).scheme != "https" or not _image_url(final):
                raise ArticlesError("图片 CDN 返回了无效响应", 502)
            if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                raise ArticlesError("图片 CDN 返回了不支持的内容编码", 502)
            declared = response.headers.get_content_type().lower()
            length = response.headers.get("Content-Length")
            if length is not None and (not re.fullmatch(r"[0-9]{1,12}", length) or not 0 < int(length) <= MAX_IMAGE_BYTES):
                raise ArticlesError("文章图片超过 2 MiB 或大小声明无效", 413)
            chunks, total = [], 0
            read = getattr(response, "read1", response.read)
            while True:
                if time.monotonic() >= deadline:
                    raise ArticlesError("文章图片读取超时", 504)
                chunk = read(min(65536, MAX_IMAGE_BYTES + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > MAX_IMAGE_BYTES:
                    raise ArticlesError("文章图片超过 2 MiB", 413)
            if length is not None and total != int(length):
                raise ArticlesError("图片 CDN 返回了不完整数据", 502)
            return b"".join(chunks), declared
    except ArticlesError:
        raise
    except TimeoutError:
        raise ArticlesError("文章图片读取超时", 504) from None
    except Exception:
        raise ArticlesError("暂时无法读取文章公开图片", 502) from None
