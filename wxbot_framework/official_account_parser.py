"""已核对的公众号响应格式与精确 JSON 跨度解析；不访问进程或网络。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
import json
import math
import re
from urllib.parse import parse_qs

MAX_SPAN_BYTES = 2 * 1024 * 1024
MAX_HTML_BYTES = 4 * 1024 * 1024
_WRAPPERS = ("AppMsgContextList", "Content", "content", "data", "result", "resp_json", "jsapi_resp", "response")
_TITLES = ("Title", "title")
_URLS = ("ContentUrl", "link", "Url", "url")
_DATES = ("ori_create_time", "send_time", "CreateTime", "create_time", "pub_time", "publish_time")


@dataclass(frozen=True)
class JsonSpan:
    offset: int
    payload: bytes
    value: object


@dataclass(frozen=True)
class PublisherProof:
    biz: str
    article: dict | None = None
    content_html: str | None = None


def canonical_url(value):
    # Shared with the storage layer; one public URL has one local identity.
    from .articles import normalize_article_url
    return normalize_article_url(value)


def url_biz(value):
    canonical = canonical_url(value)
    if not canonical:
        return None
    return parse_qs(canonical.split("?", 1)[1]).get("__biz", [None])[0]


def _first(value, names):
    for key in names:
        item = value.get(key)
        if type(item) in (str, int, float) and item != "":
            return item
    return None


def _title(value):
    if not isinstance(value, str):
        return None
    value = unescape(unescape(value))
    if not 1 <= len(value.strip()) <= 512 or any(ord(character) < 32 and character not in "\r\n\t" for character in value):
        return None
    return " ".join(value.split())


def _date(value):
    try:
        if type(value) in (int, float) or isinstance(value, str) and value.isdecimal():
            number = float(value)
            if not math.isfinite(number):
                return None
            if 1_000_000_000_000 <= number < 1_000_000_000_000_000:
                number /= 1000
            if not 946684800 <= number <= 4102444800:
                return None
            parsed = datetime.fromtimestamp(number, timezone.utc)
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                return None
        else:
            return None
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def _date_fields(value):
    if not isinstance(value, dict):
        return None
    return next((result for key in _DATES if (result := _date(value.get(key)))), None)


def _metadata(value, *, fallback=None, inherited_date=None, allow_base_url=True):
    if not isinstance(value, dict):
        return None
    base = fallback if isinstance(fallback, dict) else {}
    own_title = _first(value, _TITLES)
    base_title = _first(base, _TITLES)
    title = _title(own_title or base_title)
    link = _first(value, _URLS)
    # A common base URL cannot be assigned to several different child titles.
    if not link and (allow_base_url or not own_title or own_title == base_title):
        link = _first(base, _URLS)
    url = canonical_url(link)
    if not title or not url:
        return None
    return {"title": title, "url": url,
            "published_at": _date_fields(value) or _date_fields(base) or inherited_date}


def _walk(value, check, depth=0):
    check()
    if depth > 6:
        return
    if isinstance(value, dict):
        yield value
        for key in _WRAPPERS:
            if key in value:
                yield from _walk(value[key], check, depth + 1)
    elif isinstance(value, list):
        for item in value[:512]:
            yield from _walk(item, check, depth + 1)
    elif isinstance(value, str) and len(value) <= MAX_SPAN_BYTES and value.lstrip().startswith(("{", "[")):
        try:
            decoded = json.loads(value)
        except (ValueError, RecursionError):
            return
        yield from _walk(decoded, check, depth + 1)


def publisher_proofs(value, username, *, requested_url=None, check=lambda: None):
    """Only explicit publisher bindings can establish a gh -> public biz map."""
    for node in _walk(value, check):
        response = node.get("base_resp")
        if (node.get("user_name") == username and isinstance(response, dict) and
                type(response.get("ret")) is int and response["ret"] == 0):
            meta = _metadata(node)
            if meta:
                biz = url_biz(meta["url"])
                body = node.get("content_noencode")
                if (requested_url != meta["url"] or not isinstance(body, str) or
                        not body.strip() or "\0" in body or len(body.encode("utf-8")) > MAX_HTML_BYTES):
                    body = None
                yield PublisherProof(biz, meta, body)


def history_articles(value, biz, *, check=lambda: None):
    """Project only Msg/AppMsg rows whose own canonical URL matches the biz."""
    articles = {}
    for node in _walk(value, check):
        messages = node.get("Msg")
        if not isinstance(messages, list):
            continue
        for message in messages[:128]:
            check()
            if not isinstance(message, dict) or not isinstance(message.get("AppMsg"), dict):
                continue
            appmsg = message["AppMsg"]
            details = appmsg.get("DetailInfo")
            if not isinstance(details, list):
                continue
            inherited = _date_fields(message.get("BaseInfo")) or _date_fields(message)
            base = appmsg.get("BaseInfo")
            for detail in details[:64]:
                check()
                meta = _metadata(detail, fallback=base, inherited_date=inherited, allow_base_url=len(details) == 1)
                if meta and url_biz(meta["url"]) == biz:
                    articles[meta["url"]] = meta
    return list(articles.values())


def json_spans(raw, marker_offset, encoding, *, message_array=False, check=lambda: None):
    """Yield exactly round-trippable spans around one observed byte marker.

    A Msg value array is parsed directly after the verified field literal, so a
    large preceding FeaturedList cannot hide the directory root. Other spans
    must actually contain the supplied marker, not an adjacent JSON object.
    """
    check()
    if not isinstance(raw, bytes) or not 0 <= marker_offset < len(raw) or len(raw) > MAX_SPAN_BYTES:
        return
    if encoding not in ("utf-8", "utf-16le"):
        return
    errors = "surrogateescape" if encoding == "utf-8" else "surrogatepass"
    if encoding == "utf-16le":
        if marker_offset % 2:
            return
        raw = raw[:len(raw) // 2 * 2]
    try:
        text = raw.decode(encoding, errors)
        offset = len(raw[:marker_offset].decode(encoding, errors))
    except UnicodeError:
        return
    decoder = json.JSONDecoder()
    seen = set()

    def span(start, end, value):
        beginning = len(text[:start].encode(encoding, errors))
        payload = text[start:end].encode(encoding, errors)
        if len(payload) > MAX_SPAN_BYTES or raw[beginning:beginning + len(payload)] != payload:
            return None
        key = (beginning, len(payload))
        if key in seen:
            return None
        seen.add(key)
        return JsonSpan(beginning, payload, value)

    if message_array:
        match = re.match(r'"Msg"\s*:\s*(\[)', text[offset:])
        if match:
            start = offset + match.start(1)
            try:
                array, end = decoder.raw_decode(text[start:])
            except (ValueError, RecursionError):
                array = None
            if isinstance(array, list):
                selected = span(start, start + end, {"Msg": array})
                if selected:
                    yield selected
    starts = [match.start() for match in re.finditer(r'[\{\[]\s*(?=["\{\[\]\}])', text[:offset])]
    anchors = [match.start() for match in re.finditer(
        r'\{\s*"(?:base_resp|user_name|Content|resp_json|jsapi_resp|AppMsgContextList)"\s*:', text[:offset])]
    candidates = list(dict.fromkeys(anchors[-32:] + starts[:8] + starts[-128:]))
    for start in reversed(candidates):
        check()
        try:
            value, end = decoder.raw_decode(text[start:])
        except (ValueError, RecursionError):
            continue
        selected = span(start, start + end, value)
        if (selected and selected.offset <= marker_offset < selected.offset + len(selected.payload)):
            yield selected
