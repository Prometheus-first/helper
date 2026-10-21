"""合并转发元数据。仅解析，不联网；转发中的作者名称不代表已验证的 wxid。"""
from dataclasses import dataclass, field
import re
import xml.etree.ElementTree as ET

from .image_metadata import ImageMetadata

MAX_FORWARD_XML_BYTES = 256 * 1024 - 1
MAX_FORWARD_ITEMS = 200
MAX_FORWARD_IMAGES = 16
MAX_FORWARD_IMAGE_BYTES = 64 * 1024 * 1024
_FORBIDDEN = re.compile(r'<!\s*(?:DOCTYPE|ENTITY)\b', re.I)
_MD5 = re.compile(r'[a-fA-F0-9]{32}')


class ForwardedError(ValueError):
    def __init__(self, message, *, forwarded=False):
        super().__init__(message)
        self.forwarded = forwarded


@dataclass(frozen=True)
class ForwardedItem:
    kind: str
    sender_name: str
    source_time: str
    text: str = ''
    image: ImageMetadata | None = field(default=None, repr=False)
    image_size: int = 0
    image_error: str = ''

    def public(self):
        result = {'kind': self.kind, 'sender_name': self.sender_name,
                  'source_time': self.source_time, 'text': self.text}
        if self.kind == 'image':
            result.update(image_status='pending' if self.image else 'unavailable', image_path='',
                          image_error=self.image_error)
        return result


@dataclass(frozen=True)
class ForwardedMessage:
    title: str
    items: tuple[ForwardedItem, ...]
    truncated: bool = False

    def public(self):
        return {'app_type': 19, 'forwarded_title': self.title,
                'forwarded_records': [x.public() for x in self.items],
                'forwarded_truncated': self.truncated}


def _xml(content):
    if not isinstance(content, str) or not content.strip() or '\0' in content:
        raise ForwardedError('转发记录内容无效')
    try:
        if len(content) > MAX_FORWARD_XML_BYTES or len(content.encode('utf-8')) > MAX_FORWARD_XML_BYTES:
            raise ForwardedError('转发记录超过 256 KiB 上限')
    except UnicodeError:
        raise ForwardedError('转发记录编码无效') from None
    if _FORBIDDEN.search(content):
        raise ForwardedError('转发记录不允许实体声明')
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        raise ForwardedError('转发记录 XML 无效') from None
    pending, count = [(root, 1)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > 20000 or depth > 32:
            raise ForwardedError('转发记录结构超过上限')
        pending.extend((child, depth + 1) for child in node)
    return root


def _text(node, name, limit):
    value = node.findtext(name) or ''
    return value[:limit]


def _image(node):
    md5 = _text(node, 'fullmd5', 33)
    size = _text(node, 'datasize', 16)
    url, key = node.findtext('cdndataurl') or '', node.findtext('cdndatakey') or ''
    if not size.isascii() or not size.isdecimal() or not 1 <= int(size) <= 20 * 1024 * 1024:
        return None, 0, '图片声明大小无效'
    if not _MD5.fullmatch(md5):
        return None, int(size), '图片校验信息缺失'
    if not 1 <= len(url.encode('utf-8')) <= 1023 or any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        return None, int(size), '图片下载信息缺失'
    if len(key) not in (32, 64) or not all(ch in '0123456789abcdefABCDEF' for ch in key):
        return None, int(size), '图片下载信息缺失'
    # 实测使用图片原图任务，转发的 datafmt 与真实文件格式可能不同，保存时以文件头为准。
    return ImageMetadata(md5=md5.lower(), length=int(size), cdnbigimgurl=url, aeskey=key), int(size), ''


def parse_forwarded(content: str) -> ForwardedMessage | None:
    outer = _xml(content)
    if outer.tag != 'msg':
        return None
    apps = outer.findall('appmsg')
    if len(apps) != 1 or apps[0].findtext('type') != '19':
        return None
    app = apps[0]
    raw = app.findtext('recorditem')
    if not raw:
        raise ForwardedError('转发记录未携带可展开内容', forwarded=True)
    try:
        inner = _xml(raw)
    except ForwardedError as error:
        raise ForwardedError(str(error), forwarded=True) from None
    if inner.tag != 'recordinfo' or len(inner.findall('datalist')) != 1:
        raise ForwardedError('转发记录列表无效', forwarded=True)
    records = inner.find('datalist').findall('dataitem')
    items, images, total = [], 0, 0
    kinds = {'1':'text', '2':'image', '3':'voice', '4':'video', '5':'link', '8':'file', '17':'forwarded'}
    for node in records[:MAX_FORWARD_ITEMS]:
        kind = kinds.get(node.get('datatype'), 'unknown')
        image, size, error = None, 0, ''
        if kind == 'image':
            image, size, error = _image(node)
            if image:
                images += 1
                total += size
                if images > MAX_FORWARD_IMAGES or total > MAX_FORWARD_IMAGE_BYTES:
                    image, error = None, '本条转发图片下载额度已用完'
        items.append(ForwardedItem(kind, _text(node, 'sourcename', 128),
                                  _text(node, 'sourcetime', 64),
                                  _text(node, 'datadesc', 32768) if kind == 'text' else '',
                                  image, size, error))
    return ForwardedMessage(_text(app, 'title', 256), tuple(items), len(records) > MAX_FORWARD_ITEMS)
