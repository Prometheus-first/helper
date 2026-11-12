"""仅处理本机已验证的微信构建，备份更新器及指定更新缓存。"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

import pefile

VERSION = '4.2.0.12'
UPDATER_SHA256 = 'b4a411b766fdcda3be4e39772ff5ff1ee97ba0c51a985cb609b7135ecf4b02bc'
UPDATER = 'WeixinUpdate.exe'
BACKUP = 'WeixinUpdate.exe.helper-disabled'
STAGED_ENTRIES = {'update.data', 'download-4.2.0.13', 'patch-4.2.0.13'}
STAGED_NAME = re.compile(r'staged-(?:[0-9a-f]{32}|4\.2\.0\.13-[0-9]{8}-[0-9]{6})\Z')


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


def _backup_root():
    local = os.environ.get('LOCALAPPDATA')
    if not local or not Path(local).is_absolute():
        raise GuardError('无法定位当前用户的更新缓存备份')
    root = Path(local) / 'helper-wechat-update-backup'
    # 清理目标必须始终位于实际备份目录，不能通过联接点转到其他位置。
    for path in (root, *root.parents):
        if path.exists() and _reparse(path):
            raise GuardError('更新缓存备份路径不能包含链接')
    return root


def _check_backup_tree(path, root):
    if _backup_root() != root:
        raise GuardError('更新缓存备份目录已变化，未清理')
    if path.parent != root or not path.resolve().is_relative_to(root.resolve()):
        raise GuardError('更新缓存备份不在允许的清理目录内')
    pending = [path]
    while pending:
        entry = pending.pop()
        if _reparse(entry):
            raise GuardError('更新缓存备份包含链接，未清理')
        mode = entry.lstat().st_mode
        if stat.S_ISDIR(mode):
            pending.extend(entry.iterdir())
        elif not stat.S_ISREG(mode):
            raise GuardError('更新缓存备份包含未知文件类型，未清理')


def _prune_staged_updates():
    root = _backup_root()
    if not root.exists():
        return 0
    backups = []
    for path in root.iterdir():
        if not STAGED_NAME.fullmatch(path.name):
            continue
        if _reparse(path):
            raise GuardError('更新缓存备份目录不能是链接')
        if not path.is_dir():
            continue
        entries = {entry.name for entry in path.iterdir()}
        if not entries or not entries <= STAGED_ENTRIES:
            continue  # 非插件目录或未知文件保留，不能按名称猜测后删除。
        _check_backup_tree(path, root)
        backups.append((path.stat().st_mtime_ns, path.name, path))
    backups.sort(reverse=True)
    removed = 0
    for _, _, path in backups[1:]:
        # 真正删除前再次验证完整目标；最新备份、进行中的搬移均不在列表中。
        try:
            _check_backup_tree(path, root)
            shutil.rmtree(path)
        except FileNotFoundError:
            continue  # 多账号可能同时完成同一份旧缓存的清理。
        removed += 1
    return removed


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
    backup_root = _backup_root()
    identifier = uuid.uuid4().hex
    # 未搬完的目录不参与保留策略，避免另一个账号清理到进行中的备份。
    backup = backup_root / ('pending-' + identifier)
    backup.mkdir(parents=True, exist_ok=False)
    for source, name in present:
        source.rename(backup / name)
    backup.rename(backup_root / ('staged-' + identifier))
    return len(present)


def protect(root):
    try:
        folder = _installation(root)
        changed = _update_directory(folder)
        count = _backup_staged_updates()
        removed = _prune_staged_updates()
        return (f'更新器{"已停用" if changed else "已处于停用状态"}；已备份 {count} 项更新缓存'
                f'；已清理 {removed} 份旧缓存备份，仅保留最新一份')
    except GuardError:
        raise
    except OSError as error:
        raise GuardError(f'更新保护未完成（{type(error).__name__}）；请检查微信安装目录和缓存备份目录的写入权限') from None


def cleanup_backups():
    """清理本插件的旧缓存备份，始终保留最新一份及安装目录中的更新器。"""
    try:
        removed = _prune_staged_updates()
        return f'已清理 {removed} 份旧更新缓存备份，保留最新一份；更新器恢复备份不变'
    except OSError as error:
        raise GuardError(f'清理缓存备份失败（{type(error).__name__}），请检查备份目录权限') from None


def restore(root):
    try:
        folder = _installation(root)
        changed = _update_directory(folder, restore=True)
        return '更新器已恢复' if changed else '更新器原本已启用'
    except GuardError:
        raise
    except OSError as error:
        raise GuardError(f'恢复更新器失败（{type(error).__name__}），请检查目录权限') from None
