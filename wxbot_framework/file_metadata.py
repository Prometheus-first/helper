"""微信 type=6 文件 XML 和账号隔离存储；不连接 CDN 或执行文件。"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import tempfile
import xml.etree.ElementTree as ET

from .account import validate_account
from .files import FILE_CHUNK_BYTES, MAX_FILE_BYTES, _check_local_path, _path_value, safe_file_name

MAX_FILE_XML_BYTES = 16_384
_FORBIDDEN_DECLARATION = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_MD5 = re.compile(r"[0-9a-fA-F]{32}")
_DECIMAL = re.compile(r"[0-9]{1,19}")


class FileMetadataError(ValueError):
    """文件 XML 无效；错误消息不会包含原始 XML、CDN 地址或密钥。"""


@dataclass(frozen=True, slots=True)
class FileMetadata:
    title: str = ""
    size: int | None = None
    extension: str = ""
    md5: str = ""
    cdnattachurl: str = field(default="", repr=False)
    aeskey: str = field(default="", repr=False)
    attachid: str = field(default="", repr=False)
    filekey: str = field(default="", repr=False)
    encryver: int | None = None


def _text(parent, name, maximum=1024):
    children = parent.findall(name)
    if len(children) > 1:
        raise FileMetadataError("文件 XML 包含重复字段")
    if not children:
        return ""
    element = children[0]
    if len(element):
        raise FileMetadataError("文件 XML 字段不能包含子元素")
    value = element.text or ""
    if len(value.encode("utf-8")) > maximum or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise FileMetadataError("文件 XML 字段过长或包含控制字符")
    return value


def _number(parent, name, maximum):
    if parent.find(name) is None:
        return None
    value = _text(parent, name)
    if not _DECIMAL.fullmatch(value) or int(value) > maximum:
        raise FileMetadataError("文件 XML 数值不在支持范围内")
    return int(value)


def parse_file_metadata(content: str) -> FileMetadata | None:
    """只解析 msg/appmsg(type=6)/appattach；合法的其他 appmsg 返回 None。

    title 和 extension 仅为展示信息。下载目标由 FileStore 决定，不能把它们
    直接拼为目录。缺失 size/md5 保持未知，不把未知值当作完整性验证成功。
    """
    if not isinstance(content, str) or not content.strip():
        raise FileMetadataError("文件 XML 必须是非空字符串")
    if len(content) > MAX_FILE_XML_BYTES or "\0" in content:
        raise FileMetadataError("文件 XML 过长或包含 NUL")
    try:
        if len(content.encode("utf-8")) > MAX_FILE_XML_BYTES:
            raise FileMetadataError("文件 XML 超过 16384 字节上限")
    except UnicodeError:
        raise FileMetadataError("文件 XML 必须包含有效 UTF-8 字符") from None
    if _FORBIDDEN_DECLARATION.search(content):
        raise FileMetadataError("文件 XML 不允许 DTD 或实体声明")
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        raise FileMetadataError("文件 XML 格式不合法") from None
    pending, count = [(root, 1)], 0
    while pending:
        element, depth = pending.pop()
        count += 1
        if count > 256 or depth > 16 or len(element.attrib) > 64:
            raise FileMetadataError("文件 XML 结构过于复杂")
        pending.extend((child, depth + 1) for child in element)
    if root.tag != "msg":
        raise FileMetadataError("文件 XML 必须具有 msg/appmsg 结构")
    appmsgs = list(root.iter("appmsg"))
    if len(appmsgs) != 1 or appmsgs[0] not in list(root):
        raise FileMetadataError("文件 XML 必须具有唯一的直接 appmsg 子元素")
    appmsg = appmsgs[0]
    message_type = _text(appmsg, "type", 16)
    if not message_type or not _DECIMAL.fullmatch(message_type):
        raise FileMetadataError("文件 XML 缺少有效的 appmsg 类型")
    if int(message_type) != 6:
        return None
    attachments = list(appmsg.iter("appattach"))
    if len(attachments) != 1 or attachments[0] not in list(appmsg):
        raise FileMetadataError("文件 XML 必须具有唯一的直接 appattach 子元素")
    attach = attachments[0]
    size = _number(attach, "totallen", MAX_FILE_BYTES)
    if size == 0:
        raise FileMetadataError("当前文件接收不支持空文件")
    hashes = [_text(parent, "md5", 32) for parent in (appmsg, attach)]
    if any(value and not _MD5.fullmatch(value) for value in hashes):
        raise FileMetadataError("文件 md5 必须为 32 位十六进制字符串")
    if all(hashes) and hashes[0].lower() != hashes[1].lower():
        raise FileMetadataError("文件 XML 的 md5 字段不一致")
    title = _text(appmsg, "title", 2048)
    extension = _text(attach, "fileext", 128)
    return FileMetadata(
        title=title, size=size, extension=extension, md5=(hashes[0] or hashes[1]).lower(),
        cdnattachurl=_text(attach, "cdnattachurl", 8192),
        aeskey=_text(attach, "aeskey", 1024), attachid=_text(attach, "attachid", 8192),
        filekey=_text(attach, "filekey", 1024), encryver=_number(attach, "encryver", (1 << 31) - 1),
    )


class FileStore:
    """原子保存已下载文件；外部名称清洗后加随机前缀，按账号 SHA-256 隔离。"""

    def __init__(self, root: str | os.PathLike[str], account: str):
        validate_account(account)
        self.root = _path_value(root).expanduser().resolve()
        _check_local_path(self.root)
        self.account = account
        self.directory = self.root / ("account-" + hashlib.sha256(account.encode("ascii")).hexdigest())
        _check_local_path(self.directory)

    def _prepare_directory(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.directory.mkdir(exist_ok=True)
        if (self.root.resolve(strict=True) != self.root or
                self.directory.resolve(strict=True) != self.directory or not self.directory.is_dir()):
            raise ValueError("文件账号目录不能通过链接重定向")

    def _reserve_destination(self, name, sequence):
        prefix = "file-" if sequence is None else f"file-{sequence}-"
        for _ in range(32):
            destination = self.directory / (prefix + secrets.token_hex(16) + "-" + name)
            _check_local_path(destination)
            reservation = self.directory / ("." + destination.name + ".reserve")
            try:
                descriptor = os.open(reservation, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            os.close(descriptor)
            if destination.exists() or destination.is_symlink():
                reservation.unlink()
                continue
            return destination, reservation
        raise FileExistsError("无法生成不重复的文件名")

    def save(self, data: bytes, *, name: str = "file", sequence: int | None = None) -> Path:
        """保存非空字节，保留清洗后的原名称；不判断内容格式或打开文件。"""
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_FILE_BYTES:
            raise ValueError("文件数据必须为非空 bytes，且大小在系统支持范围内")
        if sequence is not None and (type(sequence) is not int or not 0 <= sequence <= (1 << 64) - 1):
            raise ValueError("文件消息序号必须为 0 到 2^64-1 的整数")
        name = safe_file_name(name)
        self._prepare_directory()
        temporary, reservation = None, None
        try:
            descriptor, temporary_name = tempfile.mkstemp(prefix=".file-", suffix=".tmp", dir=self.directory)
            temporary = Path(temporary_name)
            try:
                target = os.fdopen(descriptor, "wb")
            except BaseException:
                os.close(descriptor)
                raise
            with target:
                target.write(data)
                target.flush()
                os.fsync(target.fileno())
            destination, reservation = self._reserve_destination(name, sequence)
            os.replace(temporary, destination)
            temporary = None
            return destination
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if reservation is not None:
                reservation.unlink(missing_ok=True)

    def save_download(self, source_path, *, name, sequence, expected_size, expected_md5, is_current):
        """分块校验并原子保存下载结果，取消或校验失败时不留下附件。"""
        import stat
        if sequence is not None and (type(sequence) is not int or not 0 <= sequence <= (1 << 64) - 1):
            raise ValueError("文件消息序号无效")
        name = safe_file_name(name)
        source_path = Path(source_path)
        info = source_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or source_path.resolve(strict=True) != source_path or
                not 0 < info.st_size <= MAX_FILE_BYTES or
                (expected_size is not None and info.st_size != expected_size)):
            raise ValueError("下载结果不是有效的普通文件")
        self._prepare_directory()
        temporary, reservation = None, None
        try:
            descriptor, temporary_name = tempfile.mkstemp(prefix=".file-", suffix=".tmp", dir=self.directory)
            temporary = Path(temporary_name)
            try:
                target = os.fdopen(descriptor, "wb")
            except BaseException:
                os.close(descriptor)
                raise
            digest, size = hashlib.md5(), 0
            with target, source_path.open("rb") as source:
                opened = os.fstat(source.fileno())
                if (not stat.S_ISREG(opened.st_mode) or opened.st_size != info.st_size or
                        (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)):
                    raise ValueError("下载结果发生变化")
                while True:
                    if not is_current():
                        raise ValueError("微信连接已结束")
                    chunk = source.read(FILE_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > opened.st_size:
                        raise ValueError("下载结果发生变化")
                    digest.update(chunk)
                    target.write(chunk)
                current = os.fstat(source.fileno())
                if (size != opened.st_size or current.st_size != opened.st_size or
                        current.st_mtime_ns != opened.st_mtime_ns or
                        (expected_size is not None and size != expected_size) or
                        (expected_md5 and not hmac.compare_digest(digest.hexdigest(), expected_md5))):
                    raise ValueError("下载文件校验失败")
                target.flush()
                os.fsync(target.fileno())
            if not is_current():
                raise ValueError("微信连接已结束")
            destination, reservation = self._reserve_destination(name, sequence)
            os.replace(temporary, destination)
            temporary = None
            return destination, size
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if reservation is not None:
                reservation.unlink(missing_ok=True)
