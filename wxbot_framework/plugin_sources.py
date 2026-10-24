"""按机器人隔离的插件源码副本、版本校验与默认版本恢复。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import uuid

from .config import validate_name

MAX_FILE_BYTES = 256 * 1024
MAX_SOURCE_BYTES = 1024 * 1024
EDITABLE = {'.py', '.json', '.md', '.txt'}


def _safe(path, boundary):
    path, boundary = Path(path), Path(boundary).resolve()
    if not path.resolve().is_relative_to(boundary):
        raise ValueError('插件路径超出允许范围')
    current = path
    while current != boundary:
        if current.is_symlink() or (current.exists() and getattr(current.lstat(), 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError('插件路径不能使用链接')
        current = current.parent
    return path


def _atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8', newline='\n') as output:
            output.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
            output.flush()
            import os
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class PluginSources:
    def __init__(self, directory, workspace):
        self.directory = Path(directory).resolve()
        self.workspace = Path(workspace).resolve()
        self.overrides = self.workspace / '.runtime/plugin_code'

    def default_path(self, name):
        validate_name(name)
        path = _safe(self.directory / name, self.directory)
        if name.startswith('_') or not (path / '__init__.py').is_file():
            raise ValueError('未找到该插件')
        _safe(path / '__init__.py', path)
        return path

    def _folder(self, name):
        self.default_path(name)
        return _safe(self.overrides / name, self.workspace)

    def pointer(self, name):
        path = _safe(self._folder(name) / 'active.json', self.workspace)
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding='utf-8'))
        revision = value.get('revision') if isinstance(value, dict) else False
        if revision is not None and (not isinstance(revision, str) or len(revision) != 32 or
                                     any(char not in '0123456789abcdef' for char in revision)):
            raise ValueError('插件版本记录无效，请重置该插件')
        return revision

    def path(self, name):
        revision = self.pointer(name)
        if revision is None:
            return self.default_path(name)
        path = _safe(self._folder(name) / 'versions' / revision, self.workspace)
        if not (path / '__init__.py').is_file():
            raise ValueError('插件代码副本不完整，请重置该插件')
        _safe(path / '__init__.py', path)
        return path

    def files(self, name, *, defaults=False):
        folder = self.default_path(name) if defaults else self.path(name)
        result, total = {}, 0
        for path in sorted(folder.rglob('*')):
            relative = path.relative_to(folder)
            if any(part.startswith('.') or part == '__pycache__' for part in relative.parts):
                continue
            _safe(path, folder)
            if not path.is_file():
                continue
            data = path.read_bytes()
            total += len(data)
            if len(data) > MAX_FILE_BYTES or total > MAX_SOURCE_BYTES or len(result) >= 64:
                raise ValueError('插件源码超过编辑限制（单文件 256 KiB、合计 1 MiB、最多 64 个文件）')
            result[relative.as_posix()] = data
        return result

    def snapshot(self, name, options):
        files = self.files(name)
        digest = hashlib.sha256()
        for relative, data in sorted(files.items()):
            digest.update(relative.encode('utf-8') + b'\0' + len(data).to_bytes(8, 'big') + data)
        digest.update(json.dumps(options, sort_keys=True, ensure_ascii=False).encode('utf-8'))
        texts = {}
        for relative, data in files.items():
            if Path(relative).suffix in EDITABLE:
                try:
                    texts[relative] = data.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n')
                except UnicodeDecodeError:
                    continue
        return {'name': name, 'revision': digest.hexdigest(), 'files': texts,
                'options': options, 'customized': self.pointer(name) is not None}

    def validate(self, name, files):
        existing = self.snapshot(name, {})['files']
        if not isinstance(files, dict) or set(files) != set(existing):
            raise ValueError('源码文件列表已变化，请重新打开编辑器；仅允许编辑当前插件已有文件')
        for relative, text in files.items():
            path = PurePosixPath(relative)
            if (not isinstance(text, str) or '\\' in relative or path.is_absolute() or
                    any(part in ('..', '.') for part in path.parts) or path.suffix not in EDITABLE):
                raise ValueError('插件源码文件无效')
            if len(text.encode('utf-8')) > MAX_FILE_BYTES:
                raise ValueError(f'{relative} 超过 256 KiB')
            if path.suffix == '.py':
                try:
                    compile(text, relative, 'exec')
                except (SyntaxError, ValueError) as error:
                    line = getattr(error, 'lineno', None)
                    detail = getattr(error, 'msg', '包含无效字符')
                    raise ValueError(f'{relative} 第 {line or 1} 行：{detail}') from None
            elif path.suffix == '.json':
                try:
                    value = json.loads(text)
                except (ValueError, RecursionError):
                    raise ValueError(f'{relative} 的 JSON 格式不正确') from None
                if relative == 'plugin.json':
                    self._manifest(value)
        if sum(len(text.encode('utf-8')) for text in files.values()) > MAX_SOURCE_BYTES:
            raise ValueError('插件源码合计超过 1 MiB')

    @staticmethod
    def _manifest(value):
        if not isinstance(value, dict) or value.get('api', 'v1') not in ('v1', 'legacy'):
            raise ValueError('plugin.json 的 api 无效')
        requires, types = value.get('requires', ['text.receive']), value.get('message_types', [1])
        if not isinstance(requires, list) or not all(isinstance(item, str) for item in requires):
            raise ValueError('plugin.json 的 requires 应为字符串列表')
        if not isinstance(types, list) or not types or any(type(item) is not int or item <= 0 for item in types):
            raise ValueError('plugin.json 的 message_types 应为正整数列表')
        if type(value.get('default_enabled', True)) is not bool:
            raise ValueError('plugin.json 的 default_enabled 应为布尔值')

    def prepare(self, name, texts):
        self.validate(name, texts)
        original = self.files(name)
        revision = uuid.uuid4().hex
        folder = _safe(self._folder(name) / 'versions' / revision, self.workspace)
        folder.mkdir(parents=True, exist_ok=False)
        for relative, data in original.items():
            target = _safe(folder / relative, folder)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(texts[relative].encode('utf-8') if relative in texts else data)
        return revision

    def activate(self, name, revision):
        if revision is not None:
            if not isinstance(revision, str) or len(revision) != 32 or any(char not in '0123456789abcdef' for char in revision):
                raise ValueError('插件版本无效')
            _safe(self._folder(name) / 'versions' / revision, self.workspace)
        _atomic(_safe(self._folder(name) / 'active.json', self.workspace), {'revision': revision})

    def defaults(self, name):
        manifest = self.default_path(name) / 'plugin.json'
        enabled = json.loads(manifest.read_text('utf-8-sig')).get('default_enabled', True) if manifest.exists() else True
        example = self.directory.parent / 'bot_config.example.json'
        value = json.loads(example.read_text('utf-8-sig')).get('plugins', {}).get(name, {'enabled': enabled})
        return value
