"""仅处理本机已验证的微信构建，备份更新器及指定更新缓存。"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import uuid

import pefile

VERSION = '4.2.0.12'
UPDATER_SHA256 = 'b4a411b766fdcda3be4e39772ff5ff1ee97ba0c51a985cb609b7135ecf4b02bc'
UPDATER = 'WeixinUpdate.exe'
BACKUP = 'WeixinUpdate.exe.helper-disabled'


class GuardError(RuntimeError):
    pass


def _reparse(path):
    return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT) if os.name == 'nt' else path.is_symlink()


def _version(path):
    pe = pefile.PE(str(path), fast_load=True)
    try:
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY['IMAGE_DIRECTORY_ENTRY_RESOURCE']])
        fixed = pe.VS_FIXEDFILEINFO[0]
        return (fixed.FileVersionMS >> 16, fixed.FileVersionMS & 0xffff,
                fixed.FileVersionLS >> 16, fixed.FileVersionLS & 0xffff)
    finally:
        pe.close()


def _installation(root):
    if os.name != 'nt':
        raise GuardError('仅支持 Windows 版微信')
    try:
        from wxbot_framework.wechat_login import _install_path
        from native.moments_transport import _core_profile
        exe = _install_path(Path(root))
        folder = exe.parent
        dll = folder / VERSION / 'Weixin.dll'
        profile = _core_profile(dll)
        if _version(exe) != (4, 2, 0, 12) or profile is None or profile.version != VERSION:
            raise GuardError('仅支持已验证构建的微信 4.2.0.12')
        return folder
    except GuardError:
        raise
    except Exception:
        raise GuardError('未找到唯一的受支持微信安装位置') from None


def _digest(path):
    if _reparse(path) or not path.is_file():
        raise GuardError('更新器路径无效或为链接')
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _update_directory(folder, restore=False):
    directory = folder / VERSION
    if _reparse(directory):
        raise GuardError('微信版本目录不能是链接')
    source = directory / (BACKUP if restore else UPDATER)
    destination = directory / (UPDATER if restore else BACKUP)
    if source.exists() and destination.exists():
        raise GuardError('更新器和备份同时存在，请手动检查，不覆盖任何文件')
    if not source.exists():
        if destination.exists() and _digest(destination) == UPDATER_SHA256:
            return False
        raise GuardError('未找到已验证的更新程序')
    if _digest(source) != UPDATER_SHA256:
        raise GuardError('更新程序与已验证版本不一致，未修改')
    source.rename(destination)
    return True


def _backup_staged_updates():
    roaming = os.environ.get('APPDATA')
    local = os.environ.get('LOCALAPPDATA')
    if not roaming or not local:
        raise GuardError('无法定位当前用户的更新缓存')
    root = Path(roaming) / 'Tencent' / 'xwechat' / 'update'
    if not root.exists():
        return 0
    if _reparse(root):
        raise GuardError('更新缓存目录不能是链接')
    entries = [('update.data', 'update.data'), ('download/4.2.0.13', 'download-4.2.0.13'),
               ('patch/4.2.0.13', 'patch-4.2.0.13')]
    present = []
    for relative, name in entries:
        source = root / relative
        if source.exists():
            if _reparse(source) or any(_reparse(parent) for parent in source.parents if parent != root and root in parent.parents):
                raise GuardError('更新缓存路径包含链接，未转移')
            present.append((source, name))
    if not present:
        return 0
    backup = Path(local) / 'helper-wechat-update-backup' / ('staged-' + uuid.uuid4().hex)
    backup.mkdir(parents=True, exist_ok=False)
    for source, name in present:
        source.rename(backup / name)
    return len(present)


def protect(root):
    try:
        folder = _installation(root)
        changed = _update_directory(folder)
        count = _backup_staged_updates()
        return f'更新器{"已停用" if changed else "已处于停用状态"}；已备份 {count} 项更新缓存'
    except GuardError:
        raise
    except OSError as error:
        raise GuardError(f'更新保护未完成（{type(error).__name__}）；请检查微信安装目录的写入权限') from None


def restore(root):
    try:
        folder = _installation(root)
        changed = _update_directory(folder, restore=True)
        return '更新器已恢复' if changed else '更新器原本已启用'
    except GuardError:
        raise
    except OSError as error:
        raise GuardError(f'恢复更新器失败（{type(error).__name__}），请检查目录权限') from None
