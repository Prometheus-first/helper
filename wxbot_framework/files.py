"""本地文件校验与发送快照；不连接微信或网络，不执行或自动打开文件。"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
from typing import Iterator

# Windows file sizes use a signed 64-bit value; no smaller framework cap.
MAX_FILE_BYTES = (1 << 63) - 1
FILE_CHUNK_BYTES = 1024 * 1024
MAX_FILE_PATH_BYTES = 1023
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                   *(f"LPT{i}" for i in range(1, 10))}


@dataclass(frozen=True, slots=True)
class FileInfo:
    path: Path
    size: int
    name: str


def safe_file_name(value: str, *, fallback="file") -> str:
    """把外部名称变为单一文件名；原始名称不能决定任何输出目录。"""
    if not isinstance(value, str):
        raise ValueError("文件名必须是字符串")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("文件名必须可以编码为 UTF-8") from None
    # 同时处理 Windows 和 POSIX 分隔符，避免在其他平台运行测试时漏掉一种。
    name = re.split(r"[/\\]", value)[-1]
    name = "".join("_" if ord(ch) < 32 or ord(ch) == 127 or ch in '<>:"|?*' else ch
                   for ch in name).strip().rstrip(". ")
    if not name or name in (".", ".."):
        name = fallback
    if name.split(".", 1)[0].upper() in _RESERVED_NAMES:
        name = "_" + name
    # 留出随机前缀及 Windows 文件名长度空间；按 UTF-8 字节截断，不拆开字符。
    if len(name.encode("utf-8")) > 160:
        suffix = Path(name).suffix
        if len(suffix.encode("utf-8")) > 24:
            suffix = ""
        stem = name[:-len(suffix)] if suffix else name
        limit = 160 - len(suffix.encode("utf-8"))
        name = stem.encode("utf-8")[:limit].decode("utf-8", errors="ignore").rstrip(". ") + suffix
    return name


def _path_value(path) -> Path:
    try:
        value = os.fspath(path)
    except TypeError:
        raise ValueError("文件路径必须是本地文件路径") from None
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError("文件路径必须非空，且不能包含 NUL")
    if value.startswith(("\\\\", "//")) or "://" in value:
        raise ValueError("文件路径必须是本地路径，不支持网络路径或 URL")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("文件路径必须可以编码为 UTF-8") from None
    result = Path(value)
    # 禁止 NTFS 备用数据流；冒号只能用在 Windows 盘符中。
    remaining = value[len(result.drive):] if os.name == "nt" else value
    if ":" in remaining:
        raise ValueError("文件路径不能包含备用数据流或无效冒号")
    return result


def _check_local_path(path: Path) -> None:
    try:
        length = len(str(path).encode("utf-8"))
    except UnicodeError:
        raise ValueError("文件路径必须可以编码为 UTF-8") from None
    if not 1 <= length <= MAX_FILE_PATH_BYTES:
        raise ValueError("文件路径必须为 1 到 1023 个 UTF-8 字节")
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("文件路径必须是本地路径")
    if os.name == "nt" and path.anchor:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        kernel.GetDriveTypeW.restype = ctypes.c_uint
        if kernel.GetDriveTypeW(path.anchor) == 4:
            raise ValueError("文件路径不能位于网络驱动器")


def _read_file(path, output=None) -> tuple[Path, int]:
    try:
        resolved = _path_value(path).resolve(strict=True)
        _check_local_path(resolved)
        information = resolved.stat()
        if not stat.S_ISREG(information.st_mode):
            raise ValueError("文件路径必须指向普通文件")
        if not 0 < information.st_size <= MAX_FILE_BYTES:
            raise ValueError("文件必须非空，且大小在系统支持范围内")
        with resolved.open("rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode) or not 0 < opened.st_size <= MAX_FILE_BYTES:
                raise ValueError("文件必须为非空普通文件，且大小在系统支持范围内")
            size = 0
            if output is not None:
                while True:
                    chunk = source.read(FILE_CHUNK_BYTES)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > opened.st_size:
                        raise ValueError("文件在读取期间发生变化，请重试")
                    output.write(chunk)
            else:
                size = opened.st_size
            current = os.fstat(source.fileno())
        if (size != opened.st_size or
                current.st_size != opened.st_size or current.st_mtime_ns != opened.st_mtime_ns):
            raise ValueError("文件在读取期间发生变化，请重试")
        return resolved, size
    except (OSError, RuntimeError) as error:
        raise ValueError("文件不存在或无法读取") from error


def inspect_file(path) -> FileInfo:
    """校验非空普通文件；不按扩展名或固定大小限制文件。"""
    resolved, size = _read_file(path)
    return FileInfo(resolved, size, resolved.name)


@contextmanager
def prepare_file(path, directory) -> Iterator[FileInfo]:
    """生成保留安全文件名的受控快照，退出时仅清理本次临时目录。"""
    resolved, _ = _read_file(path)
    # 已存在的本地普通文件拥有合法 basename；发送端保留完整原文件名。
    # safe_file_name 仅用于接收 XML 中无法信任的外部名称。
    name = resolved.name
    try:
        root = _path_value(directory).resolve()
        _check_local_path(root)
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir() or root.resolve(strict=True) != root:
            raise ValueError("文件临时目录不能通过链接重定向")
        owned = Path(tempfile.mkdtemp(prefix="file-", dir=root))
    except (OSError, RuntimeError) as error:
        raise ValueError("无法创建文件临时目录") from error
    try:
        if owned.parent != root or owned.resolve(strict=True) != owned:
            raise ValueError("文件临时目录超出预期范围")
        snapshot = owned / name
        _check_local_path(snapshot)
        with snapshot.open("xb") as output:
            _, size = _read_file(resolved, output)
        yield FileInfo(snapshot, size, name)
    finally:
        # 删除前再次确认绝对目录边界；不递归跟随被替换的目录链接。
        if (owned.parent == root and owned.is_absolute() and owned.exists() and
                owned.resolve(strict=True) == owned):
            shutil.rmtree(owned)
