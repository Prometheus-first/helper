"""当前受管 Linux 微信的本地表情缓存导出；密钥只保留在内存。"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import stat
import struct
import tempfile

from .emoticons import EmoticonSessionExpired, export_local_emoticons
from .linux_contacts import VerifiedLinuxProcess
from .linux_contact_database import _contact_db


def _cache_key(process):
    """4.1.13.26 getter 0x61a00e0：账号上下文 +0x240 的表情专用字段。"""
    def read(address, size):
        value = process.read(address, size)
        if len(value) != size:
            raise ValueError('表情缓存字段不完整')
        return value
    def pointer(address):
        value = struct.unpack('<Q', read(address, 8))[0]
        if not 0x10000 <= value < 0x800000000000:
            raise ValueError('表情缓存指针不可用')
        return value
    process.verify()
    app = pointer(process.base + 0xacf6710)
    table = pointer(app)
    if pointer(table + 0x68) != process.base + 0x6041b30:
        raise ValueError('表情缓存服务布局未验证')
    hub = pointer(app + 0x68)
    header = read(hub + 0x240, 24)
    if header[0] & 1:
        length, address = struct.unpack_from('<QQ', header, 8)
    else:
        length, address = header[0] >> 1, hub + 0x241
    if length != 16 or not 0x10000 <= address < 0x800000000000:
        raise ValueError('当前表情缓存尚未就绪')
    encoded = read(address, length)
    if (read(hub + 0x240, 24) != header or read(address, length) != encoded or
            pointer(process.base + 0xacf6710) != app or pointer(app + 0x68) != hub):
        raise EmoticonSessionExpired('emoticon_session_expired')
    mask = struct.pack('<4Q', 0x24e867175f37c146, 0x6462eb5e9fa90471,
                       0xb3d17f98dc8f123b, 0x2959d0276c89b57c)
    plain = bytes(a ^ b for a, b in zip(encoded, mask))
    process.verify()
    return plain[:16]


def _private_directory(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise ValueError('表情导出目录不能包含链接')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or
            stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError('表情导出目录必须是当前用户的私有目录')
    return path


def export_linux_emoticons(pid, *, expected_account, expected_sha256, instance_home,
                           output_directory, convert=True, is_current=lambda: True):
    """只导出已确认 Persist/Thumb 布局，不把缓存集合当成完整收藏。"""
    if type(convert) is not bool or not callable(is_current):
        raise ValueError('表情导出选项无效')
    run = None
    try:
        with VerifiedLinuxProcess(pid, expected_account, expected_sha256) as process:
            def current():
                if not is_current():
                    return False
                process.verify()
                return True
            if not current():
                raise EmoticonSessionExpired('emoticon_session_expired')
            home = Path(instance_home).absolute()
            if home.resolve(strict=True) != home:
                raise ValueError('微信会话目录不能包含链接')
            database = _contact_db(process, home)
            profile = database.parents[2]
            if (profile.parent != home / 'xwechat_files' or
                    profile.stat().st_uid != os.geteuid()):
                raise ValueError('表情缓存不属于当前微信会话')
            output = Path(output_directory).absolute()
            if output == home or home in output.parents or profile == output or profile in output.parents:
                raise ValueError('导出目录不能位于微信数据目录内')
            directory = _private_directory(output)
            run = Path(tempfile.mkdtemp(prefix='export-', dir=directory))
            key = _cache_key(process)
            try:
                rows = export_local_emoticons(profile, run, key, expected_account,
                    convert=convert, is_current=current)
            finally:
                del key
            if not current():
                raise EmoticonSessionExpired('emoticon_session_expired')
            # shared exporter creates its namespace beneath our exclusive run.
            for child in run.iterdir():
                if child.is_dir() and not child.is_symlink():
                    child.chmod(0o700)
            return rows
    except BaseException:
        if run is not None and run.parent == Path(output_directory).absolute() and not run.is_symlink():
            shutil.rmtree(run)
        raise
