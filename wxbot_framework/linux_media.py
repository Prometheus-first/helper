"""Linux 媒体元数据及已经落盘的附件读取；不把缓存命中当成主动下载成功。"""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
import xml.etree.ElementTree as ET


def parse_test_media(event, *, expected_filename):
    kind = event.get("type")
    if kind not in (3, 49):
        return None
    raw = event.get("text", "")
    if not isinstance(raw, str) or "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
        raise ValueError("拒绝包含 DTD 或实体定义的媒体 XML")
    start = raw.find("<msg")
    if start < 0:
        raise ValueError("没有媒体消息根节点")
    root = ET.fromstring(raw[start:])
    if root.tag != "msg":
        raise ValueError("媒体消息根节点错误")
    if kind == 3:
        image = root.find("img")
        if image is None:
            raise ValueError("图片消息没有 img 节点")
        return {"type": 3, "xml": ET.tostring(root, encoding="unicode"), "attributes": dict(image.attrib)}
    app = root.find("appmsg")
    if app is None or app.findtext("type") not in ("6", "74") or app.findtext("title") != expected_filename:
        return None
    attach = app.find("appattach")
    if attach is None:
        raise ValueError("文件消息没有附件节点")
    return {"type": 49, "app_type": int(app.findtext("type")), "xml": ET.tostring(root, encoding="unicode"),
            "title": expected_filename, "attributes": {child.tag: child.text or "" for child in attach},
            "md5": app.findtext("md5", "").strip().lower()}


def find_cached_file(capture, account, *, storage_root=None):
    """只返回当前账号下大小与消息 MD5 同时匹配的已下载附件。"""
    if not isinstance(account, str) or re.fullmatch(r"wxid_[A-Za-z0-9_-]+", account) is None:
        raise ValueError("当前 Linux 微信账号无效")
    title = capture.get("title", "")
    if not isinstance(title, str) or not title or title in (".", "..") or any(x in title for x in ("/", "\\", "\0")):
        raise ValueError("附件文件名无效")
    md5 = capture.get("md5", "")
    length = capture.get("attributes", {}).get("totallen", "")
    if not isinstance(md5, str) or re.fullmatch(r"[0-9a-fA-F]{32}", md5) is None:
        raise ValueError("附件没有有效的 MD5，不能确认缓存归属")
    if not isinstance(length, str) or not length.isascii() or not length.isdecimal() or int(length) <= 0:
        raise ValueError("附件大小无效")
    root = Path(storage_root or Path.home() / "xwechat_files").expanduser().resolve()
    if not root.is_dir():
        return None
    for profile in root.iterdir():
        if profile.is_symlink() or not profile.is_dir() or not (profile.name == account or re.fullmatch(re.escape(account) + r"_[0-9a-f]{4,16}", profile.name)):
            continue
        directory = profile / "msg" / "file"
        if not directory.is_dir():
            continue
        # 日期目录枚举不读取其他会话正文；仅打开本条消息指定的文件名。
        for month in sorted(directory.iterdir(), reverse=True):
            if month.is_symlink() or not month.is_dir() or re.fullmatch(r"[0-9]{4}-[0-9]{2}", month.name) is None:
                continue
            candidate = month / title
            if candidate.is_symlink():
                continue
            try:
                path = candidate.resolve(strict=True)
                if not path.is_relative_to(profile.resolve()) or not path.is_file() or path.stat().st_size != int(length):
                    continue
                with path.open("rb") as source:
                    checksum = hashlib.file_digest(source, "md5").hexdigest()
                if checksum == md5.lower():
                    return path
            except (OSError, RuntimeError):
                continue
    return None


def _read_cache_key(pid):
    """读取匹配版本微信的当前缓存密钥，只在本次调用内使用。"""
    import ctypes
    import struct
    from .linux_backend import LINUX_WECHAT_SHA256
    if type(pid) is not int or pid <= 0:
        raise ValueError("Linux 微信进程无效")
    proc = Path("/proc") / str(pid)
    executable = (proc / "exe").resolve(strict=True)
    with executable.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != LINUX_WECHAT_SHA256:
            raise RuntimeError("Linux 微信版本指纹不匹配")
    maps = [line.split() for line in (proc / "maps").read_text().splitlines()]
    mappings = [fields for fields in maps if len(fields) >= 6 and fields[2] == "00000000" and fields[-1] == str(executable)]
    if len(mappings) != 1:
        raise RuntimeError("Linux 微信主模块映射无效")
    base = int(mappings[0][0].split("-")[0], 16)
    class IOV(ctypes.Structure):
        _fields_ = [("base", ctypes.c_void_p), ("length", ctypes.c_size_t)]
    libc = ctypes.CDLL(None, use_errno=True)
    reader = libc.process_vm_readv
    reader.argtypes = [ctypes.c_int, ctypes.POINTER(IOV), ctypes.c_ulong, ctypes.POINTER(IOV), ctypes.c_ulong, ctypes.c_ulong]
    reader.restype = ctypes.c_ssize_t
    def read(address, length):
        if not address or not 0 < length <= 64:
            raise RuntimeError("微信缓存字段无效")
        buffer = ctypes.create_string_buffer(length)
        local, remote = IOV(ctypes.addressof(buffer), length), IOV(address, length)
        if reader(pid, ctypes.byref(local), 1, ctypes.byref(remote), 1, 0) != length:
            raise RuntimeError("无法读取当前微信的缓存字段")
        return buffer.raw
    def pointer(address):
        return struct.unpack("<Q", read(address, 8))[0]
    app = pointer(base + 0xacf6710)
    table = pointer(app)
    if pointer(table + 0x68) != base + 0x6041b30:
        raise RuntimeError("微信当前服务入口布局不匹配")
    hub = pointer(app + 0x68)
    native = read(hub + 0x228, 24)
    if native[0] & 1:
        length, address = struct.unpack_from("<QQ", native, 8)
        if length != 32:
            raise RuntimeError("当前微信缓存密钥尚未就绪")
        encoded = read(address, length)
    else:
        raise RuntimeError("当前微信缓存密钥尚未就绪")
    # 该掩码是此版本 getter 的常量，并非用户密钥；不记录解开的账号密钥。
    mask = struct.pack("<4Q", 0x42173edadaef52a4, 0xbb1f97421344a33a, 0xef1a6fe2eaf0557f, 0x9fdaa6742ac652b4)
    value = bytes(a ^ b for a, b in zip(encoded, mask))
    if any(byte not in b"0123456789abcdefABCDEF" for byte in value):
        raise RuntimeError("微信缓存密钥格式无效")
    # 原生 AES 包装器使用返回字符串的前 16 字节，不对十六进制文本再次解码。
    return value[:16]


def decode_cached_image(data, key, expected_md5):
    """还原一张 V2 缓存图片，校验消息 MD5 后才返回内容；密钥不写入磁盘。"""
    import struct
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    if not isinstance(data, bytes) or not isinstance(expected_md5, str) or re.fullmatch(r"[0-9a-fA-F]{32}", expected_md5) is None:
        raise ValueError("图片缓存或消息校验值无效")
    if hashlib.md5(data).hexdigest() == expected_md5.lower():
        return data
    if not data.startswith(b"\x07\x08V2\x08\x07") or len(data) < 31:
        return None
    if not isinstance(key, bytes) or len(key) != 16:
        raise ValueError("图片缓存解密参数无效")
    aes_size, xor_size = struct.unpack_from("<II", data, 6)
    encrypted_size = (aes_size // 16 + 1) * 16
    if encrypted_size > len(data) - 15 or xor_size > len(data) - 15 - encrypted_size:
        return None
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    prefix = decryptor.update(data[15:15 + encrypted_size]) + decryptor.finalize()
    pad = prefix[-1]
    if not 1 <= pad <= 16 or prefix[-pad:] != bytes([pad]) * pad:
        return None
    prefix = prefix[:-pad]
    if len(prefix) != aes_size:
        return None
    end = len(data) - xor_size
    middle, tail = data[15 + encrypted_size:end], data[end:]
    # 尾部异或字节不单独导出；以格式尾标缩小候选，再用完整消息 MD5 判定。
    if prefix.startswith(b"\xff\xd8\xff") and len(tail) >= 2:
        candidate = tail[-1] ^ 0xd9
        candidates = [candidate] if tail[-2] ^ candidate == 0xff else []
    elif prefix.startswith(b"\x89PNG") and len(tail) >= 12:
        ending = b"\x00\x00\x00\x00IEND\xaeB`\x82"
        candidate = tail[-1] ^ ending[-1]
        candidates = [candidate] if bytes(byte ^ candidate for byte in tail[-12:]) == ending else []
    else:
        candidates = range(256) if tail else (0,)
    for candidate in candidates:
        result = prefix + middle + tail.translate(bytes(byte ^ candidate for byte in range(256)))
        if hashlib.md5(result).hexdigest() == expected_md5.lower():
            return result
    return None


def read_cached_image(capture, account, pid, *, storage_root=None):
    """读取当前账号已经下载的图片；没有缓存则返回 None，不代替主动下载。"""
    if not isinstance(account, str) or re.fullmatch(r"wxid_[A-Za-z0-9_-]+", account) is None:
        raise ValueError("当前 Linux 微信账号无效")
    attrs = capture.get("attributes", {})
    md5 = attrs.get("md5", "")
    if not isinstance(md5, str) or re.fullmatch(r"[0-9a-fA-F]{32}", md5) is None:
        raise ValueError("图片没有有效的消息 MD5")
    lengths = {int(value) for name in ("length", "hdlength") if isinstance(value := attrs.get(name), str) and value.isascii() and value.isdecimal() and int(value) > 0}
    if not lengths:
        raise ValueError("图片没有有效的消息长度")
    root = Path(storage_root or Path.home() / "xwechat_files").expanduser().resolve()
    if not root.is_dir():
        return None
    import os
    key = None
    for profile in root.iterdir():
        if profile.is_symlink() or not profile.is_dir() or not (profile.name == account or re.fullmatch(re.escape(account) + r"_[0-9a-f]{4,16}", profile.name)):
            continue
        for directory, dirs, files in os.walk(profile / "msg", followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(directory) / name).is_symlink()]
            for name in files:
                path = Path(directory) / name
                try:
                    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(profile.resolve()):
                        continue
                    if path.stat().st_size not in lengths | {length + 31 for length in lengths}:
                        continue
                    data = path.read_bytes()
                    if hashlib.md5(data).hexdigest() == md5.lower():
                        return data
                    if not data.startswith(b"\x07\x08V2\x08\x07"):
                        continue
                    if key is None:
                        key = _read_cache_key(pid)
                    result = decode_cached_image(data, key, md5)
                    if result is not None:
                        return result
                except OSError:
                    continue
    return None
