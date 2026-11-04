"""表情消息的有界元数据解析；下载地址不进入 repr 或消息监控。"""
from dataclasses import dataclass, field
import re
import urllib.parse
from xml.etree import ElementTree as ET

MAX_SOURCE_BYTES = 20 * 1024 * 1024
CDN_HOSTS = frozenset({'vweixinf.tc.qq.com', 'vweixin.tc.qq.com',
                       'wxapp.tc.qq.com', 'emoji.qpic.cn'})


class StickerError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Sticker:
    md5: str
    length: int | None = None
    url: str = field(default='', repr=False)


def sticker_url(value):
    if (not isinstance(value, str) or not value or len(value) > 4096 or '\\' in value or
            any(ord(ch) <= 32 or ord(ch) == 127 for ch in value)):
        raise StickerError('表情包下载地址无效')
    try:
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or parsed.hostname not in CDN_HOSTS or
                parsed.username or parsed.password or parsed.fragment or not parsed.path.startswith('/') or
                parsed.port not in (None, 80 if parsed.scheme == 'http' else 443)):
            raise ValueError()
    except ValueError:
        raise StickerError('表情包下载地址不受支持') from None
    # 某些微信旧 CDN 仅提供 HTTP；保留消息给出的协议，下载后必须校验 MD5。
    return value


def parse_sticker(content):
    if not isinstance(content, str) or not content or len(content) > 16384 or '\0' in content:
        raise StickerError('表情包元数据无效')
    try:
        if len(content.encode('utf-8')) > 16384:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise StickerError('表情包元数据过长或编码无效') from None
    if re.search(r'<!\s*(DOCTYPE|ENTITY)\b', content, re.I):
        raise StickerError('表情包元数据不允许实体声明')
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        raise StickerError('表情包元数据格式无效') from None
    pending, count = [(root, 1)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > 128 or depth > 16 or len(node.attrib) > 64:
            raise StickerError('表情包元数据结构过大')
        pending.extend((child, depth + 1) for child in node)
    nodes = list(root.iter('emoji'))
    if root.tag != 'msg' or len(nodes) != 1 or nodes[0] not in list(root):
        raise StickerError('表情包元数据缺少唯一 emoji 节点')
    attributes = nodes[0].attrib
    md5 = attributes.get('md5', '')
    if not re.fullmatch(r'[a-fA-F0-9]{32}', md5):
        raise StickerError('表情包缺少内容校验信息')
    length = attributes.get('len', '')
    if length and (not re.fullmatch(r'[0-9]{1,10}', length) or int(length) > MAX_SOURCE_BYTES):
        raise StickerError('表情包声明大小超过支持范围')
    length = (int(length) or None) if length else None
    return Sticker(md5.lower(), length, sticker_url(attributes.get('cdnurl', '')))
