"""微信引用回复：只取本次正文与引用摘要，不执行内嵌消息或下载媒体。"""
from dataclasses import dataclass
import re
from xml.etree import ElementTree as ET

MAX_XML_BYTES = 256 * 1024 - 1
# The complete parsed XML is already bounded by MAX_XML_BYTES.  Do not impose
# a smaller, silent character limit on the question or the cited text.
MAX_TEXT = MAX_XML_BYTES
_DECLARATION = re.compile(r'<!\s*(?:DOCTYPE|ENTITY)\b', re.I)
_KINDS = {'text', 'image', 'voice', 'video', 'sticker', 'file', 'link', 'quote', 'unknown'}
_LABELS = {3: ('image', '[图片]'), 34: ('voice', '[语音]'), 43: ('video', '[视频]'),
           47: ('sticker', '[表情]'), 62: ('video', '[视频]')}


class QuoteError(ValueError):
    pass


def validate_quote_id(value):
    """Return a nonzero 64-bit WeChat server message ID."""
    if isinstance(value, str) and 1 <= len(value) <= 20 and value.isascii() and value.isdecimal():
        value = int(value)
    if type(value) is not int or not 0 < value < 2**64:
        raise QuoteError('原消息缺少有效的微信服务器消息编号')
    return value


def _clean(value, limit, *, label=False):
    if not isinstance(value, str):
        return ''
    return ''.join(char for char in value if ord(char) >= 32 and ord(char) != 127 or
                   not label and char in '\n\r\t')[:limit].strip()


def _xml(value):
    if not isinstance(value, str) or not value.strip() or '\0' in value:
        raise QuoteError('引用消息内容无效')
    try:
        if len(value) > MAX_XML_BYTES or len(value.encode('utf-8')) > MAX_XML_BYTES:
            raise QuoteError('引用消息超过大小限制')
    except UnicodeError:
        raise QuoteError('引用消息编码无效') from None
    declarations = re.sub(r'<!\[CDATA\[.*?\]\]>|<!--.*?-->', '', value, flags=re.S)
    if _DECLARATION.search(declarations):
        raise QuoteError('引用消息不允许实体声明')
    try:
        root = ET.fromstring(value)
    except (ET.ParseError, ValueError):
        raise QuoteError('引用消息 XML 无效') from None
    stack, count = [(root, 1)], 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if depth > 24 or count > 2048:
            raise QuoteError('引用消息结构超过限制')
        stack.extend((child, depth + 1) for child in node)
    return root


def _field(node, name):
    children = node.findall(name)
    if len(children) > 1:
        raise QuoteError('引用消息包含重复字段')
    return children[0].text or '' if children else ''


@dataclass(frozen=True)
class QuotedContent:
    author: str = ''
    kind: str = 'unknown'
    text: str = '[原消息未提供]'
    available: bool = False
    truncated: bool = False

    def public(self):
        # 不带原始 XML、CDN 地址、凭据、路径或引用作者的内部 ID。
        return {'author': self.author, 'kind': self.kind, 'text': self.text,
                'available': self.available, 'truncated': self.truncated}


@dataclass(frozen=True)
class QuotedMessage:
    text: str
    reference: QuotedContent

    def public(self):
        return {'app_type': 57, 'quoted_text': self.text, 'quoted_message': self.reference.public()}


def _reference(node, depth):
    author = _clean(_field(node, 'displayname'), 128, label=True)
    kind = _field(node, 'type').strip()
    raw = _field(node, 'content')
    if kind == '1':
        text = _clean(raw, MAX_TEXT)
        return QuotedContent(author, 'text', text or '[原消息未提供]', bool(text), len(raw) > MAX_TEXT)
    if kind.isascii() and kind.isdecimal() and len(kind) < 8 and int(kind) in _LABELS:
        media, label = _LABELS[int(kind)]
        return QuotedContent(author, media, label, True)
    if kind == '49' and raw.strip() and depth < 2:
        try:
            inner = _xml(raw)
            apps = inner.findall('appmsg') if inner.tag == 'msg' else []
            if len(apps) == 1:
                app = apps[0]
                subtype = _field(app, 'type').strip()
                if subtype == '57':
                    nested = _parse(inner, depth + 1)
                    if nested is not None:
                        text = nested.text + '\n引用 ' + (nested.reference.author or '未知作者') + '：' + nested.reference.text
                        return QuotedContent(author, 'quote', _clean(text, MAX_TEXT), True,
                                             len(text) > MAX_TEXT or nested.reference.truncated)
                if subtype in ('5', '6'):
                    title = _clean(_field(app, 'title'), 256, label=True)
                    return QuotedContent(author, 'link' if subtype == '5' else 'file',
                                         ('[链接]' if subtype == '5' else '[文件]') + (' ' + title if title else ''), True)
        except QuoteError:
            pass
    return QuotedContent(author, text='[无法读取的引用内容]')


def _parse(root, depth=0):
    if root.tag != 'msg':
        return None
    apps = root.findall('appmsg')
    if len(apps) != 1 or _field(apps[0], 'type').strip() != '57':
        return None
    app = apps[0]
    text = _clean(_field(app, 'title'), MAX_TEXT)
    refs = app.findall('refermsg')
    if len(refs) > 1:
        raise QuoteError('引用消息包含多个原消息')
    quoted = _reference(refs[0], depth) if refs else QuotedContent()
    return QuotedMessage(text, quoted)


def parse_quote(content):
    """普通分享返回 None；格式损坏抛 QuoteError，错误中不包含消息正文。"""
    return _parse(_xml(content))


def reference(message):
    """读取框架已提取的引用摘要；不根据可见文字或其他分享猜测引用。"""
    if message.type != 49 or message.data.get('app_type') != 57:
        return None
    value = message.data.get('quoted_message')
    if not isinstance(value, dict) or value.get('kind') not in _KINDS:
        return None
    if (not isinstance(value.get('text'), str) or not isinstance(value.get('author'), str) or
            type(value.get('available')) is not bool or type(value.get('truncated')) is not bool):
        return None
    return QuotedContent(_clean(value['author'], 128, label=True), value['kind'],
                         _clean(value['text'], MAX_TEXT), value['available'], value['truncated'])


def display_quote(message):
    quoted = reference(message)
    if quoted is None:
        return ''
    return message.text + '\n引用 ' + (quoted.author or '未知作者') + '：' + quoted.text
