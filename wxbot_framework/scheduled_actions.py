"""插件定时动作的声明和参数校验，不从网页接收任意代码或函数名。"""
import json
import re
from pathlib import Path

from .config import validate_name
from .text import validate_target

IDENT = re.compile(r'^[a-z][a-z0-9_]{0,63}$')
METHOD = re.compile(r'^[a-z][a-zA-Z0-9_]{0,63}$')
TYPES = {'text', 'textarea', 'chat', 'integer', 'boolean'}
BUILTIN = {'id': 'builtin.send_text', 'label': '发送文字', 'fields': [
    {'name': 'chat_id', 'label': '会话 ID', 'type': 'chat', 'required': True},
    {'name': 'content', 'label': '消息内容', 'type': 'textarea', 'required': True}]}


def actions_from_manifest(name, manifest):
    validate_name(name)
    definitions = manifest.get('scheduled_actions', [])
    if not isinstance(definitions, list) or len(definitions) > 16:
        raise ValueError('scheduled_actions 应为最多 16 项的列表')
    result, seen = [], set()
    for definition in definitions:
        if not isinstance(definition, dict):
            raise ValueError('定时动作声明无效')
        ident, method, label = (definition.get(key) for key in ('id', 'method', 'label'))
        if not isinstance(ident, str) or not IDENT.fullmatch(ident) or ident in seen:
            raise ValueError('定时动作 ID 无效或重复')
        if not isinstance(method, str) or not METHOD.fullmatch(method):
            raise ValueError('定时动作方法名无效')
        if not isinstance(label, str) or not label.strip() or len(label) > 80:
            raise ValueError('定时动作名称无效')
        fields = definition.get('fields', [])
        if not isinstance(fields, list) or len(fields) > 16:
            raise ValueError('定时动作字段无效')
        normalized, field_names = [], set()
        for field in fields:
            if not isinstance(field, dict):
                raise ValueError('定时动作字段无效')
            key, title, kind = (field.get(k) for k in ('name', 'label', 'type'))
            if not isinstance(key, str) or not IDENT.fullmatch(key) or key in field_names:
                raise ValueError('定时动作字段名无效或重复')
            if not isinstance(title, str) or not title.strip() or len(title) > 80 or kind not in TYPES:
                raise ValueError('定时动作字段类型或名称无效')
            required = field.get('required', False)
            if type(required) is not bool:
                raise ValueError('定时动作字段 required 必须是布尔值')
            normalized.append({'name': key, 'label': title, 'type': kind, 'required': required})
            field_names.add(key)
        result.append({'id': f'plugin.{name}.{ident}', 'label': label.strip(), 'plugin': name,
                       'method': method, 'fields': normalized})
        seen.add(ident)
    return result


def discover_actions(root, sources):
    result = [BUILTIN]
    for directory in sorted((Path(root) / 'plugins').iterdir()):
        if not directory.is_dir() or directory.name.startswith('_') or not (directory / '__init__.py').is_file():
            continue
        try:
            path = sources.path(validate_name(directory.name)) / 'plugin.json'
            if path.exists():
                manifest = json.loads(path.read_text(encoding='utf-8-sig'))
                result.extend(actions_from_manifest(directory.name, manifest))
        except (ValueError, OSError, TypeError, AttributeError):
            # 一个插件声明错误不妨碍其他任务；插件状态页负责展示加载错误。
            continue
    return result


def validate_params(action, values):
    if not isinstance(values, dict) or set(values) - {field['name'] for field in action['fields']}:
        raise ValueError('定时动作参数无效')
    output = {}
    for field in action['fields']:
        key, kind = field['name'], field['type']
        value = values.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            if field['required']:
                raise ValueError(f'请填写{field["label"]}')
            continue
        if kind == 'boolean':
            if type(value) is not bool: raise ValueError(f'{field["label"]}必须是布尔值')
        elif kind == 'integer':
            if type(value) is not int or not -(2**31) <= value < 2**31: raise ValueError(f'{field["label"]}必须是整数')
        else:
            if not isinstance(value, str) or '\0' in value or (len(value) > 32768 and action['id'] != 'builtin.send_text'):
                raise ValueError(f'{field["label"]}无效')
            value.encode('utf-8')
            if kind == 'chat': validate_target(value)
        output[key] = value
    return output
