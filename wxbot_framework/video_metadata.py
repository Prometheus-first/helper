"""解析普通视频消息元数据；下载凭据只保留在受控进程内。"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
import xml.etree.ElementTree as ET

MAX_VIDEO_XML_BYTES = 16_384
_HASH = re.compile(r"[0-9a-fA-F]{32}")
_HEX_KEY = re.compile(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{64})")
_NUMBER = re.compile(r"[0-9]{1,19}")
_FORBIDDEN = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


class VideoMetadataError(ValueError):
    """视频元数据无效；异常信息不包含原始 XML 或下载凭据。"""


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    length: int | None = None
    raw_length: int | None = None
    duration: int | None = None
    md5: str = ""
    raw_md5: str = ""
    cdn_url: str = field(default="", repr=False)
    aes_key: str = field(default="", repr=False)
    raw_cdn_url: str = field(default="", repr=False)
    raw_aes_key: str = field(default="", repr=False)


def select_video_download(metadata: VideoMetadata):
    """优先选择消息中的原始视频引用；不将凭据输出到公开事件。"""
    if metadata.raw_cdn_url and metadata.raw_aes_key and metadata.raw_length and metadata.raw_md5:
        return metadata.raw_cdn_url, metadata.raw_aes_key, metadata.raw_length, metadata.raw_md5, "original"
    if metadata.cdn_url and metadata.aes_key and metadata.length and metadata.md5:
        return metadata.cdn_url, metadata.aes_key, metadata.length, metadata.md5, "standard"
    return "", "", 0, "", "unknown"


def parse_video_metadata(content: str) -> VideoMetadata:
    """解析单个 msg/videomsg；不下载、记录或对外暴露 CDN 参数。"""
    if not isinstance(content, str) or not content.strip() or "\0" in content:
        raise VideoMetadataError("视频 XML 无效")
    try:
        if len(content.encode("utf-8")) > MAX_VIDEO_XML_BYTES:
            raise VideoMetadataError("视频 XML 超过长度限制")
    except UnicodeEncodeError:
        raise VideoMetadataError("视频 XML 不是有效的 UTF-8") from None
    if _FORBIDDEN.search(content):
        raise VideoMetadataError("视频 XML 不允许 DTD 或实体声明")
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        raise VideoMetadataError("视频 XML 格式无效") from None
    pending, count = [(root, 1)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > 256 or depth > 16 or len(node.attrib) > 64:
            raise VideoMetadataError("视频 XML 结构过于复杂")
        pending.extend((child, depth + 1) for child in node)
    videos = list(root.iter("videomsg"))
    if root.tag != "msg" or len(videos) != 1 or videos[0] not in list(root):
        raise VideoMetadataError("视频 XML 必须包含唯一的直接 videomsg 子元素")
    attrs = videos[0].attrib

    def number(name):
        value = attrs.get(name, "")
        if not value:
            return None
        if not _NUMBER.fullmatch(value) or int(value) > (1 << 63) - 1:
            raise VideoMetadataError("视频数值字段无效")
        return int(value)

    def digest(name):
        value = attrs.get(name, "")
        if value and not _HASH.fullmatch(value):
            raise VideoMetadataError("视频校验值无效")
        return value.lower()

    def key(name):
        value = attrs.get(name, "")
        if value and not _HEX_KEY.fullmatch(value):
            raise VideoMetadataError("视频密钥格式无效")
        return value

    def url(name):
        value = attrs.get(name, "")
        if len(value.encode("utf-8")) > 1023 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise VideoMetadataError("视频下载引用无效")
        return value

    return VideoMetadata(length=number("length"), raw_length=number("rawlength"),
        duration=number("playlength"), md5=digest("md5"), raw_md5=digest("rawmd5"),
        cdn_url=url("cdnvideourl"), aes_key=key("aeskey"),
        raw_cdn_url=url("cdnrawvideourl"), raw_aes_key=key("cdnrawvideoaeskey"))
