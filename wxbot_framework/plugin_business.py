"""只读取、替换插件声明的业务函数；导入、生命周期和其余源码不参与编辑。"""
from __future__ import annotations

import ast
import json
from pathlib import PurePosixPath
import re
import textwrap


def _node(tree, qualified):
    node = tree
    for name in qualified.split('.'):
        node = next((child for child in node.body if isinstance(child, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                     and child.name == name), None)
        if node is None:
            return None
    return node if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else None


def _entries(files):
    try:
        manifest = json.loads(files.get('plugin.json', '{}'))
    except (TypeError, ValueError):
        raise ValueError('插件清单格式不正确，请先恢复默认插件') from None
    if not isinstance(manifest, dict):
        raise ValueError('插件清单应为 JSON 对象')
    declarations = manifest.get('business') if isinstance(manifest, dict) else None
    if declarations is None:
        declarations = [{'function':'Plugin.on_message','label':'处理消息'}]
    if not isinstance(declarations, list) or not 1 <= len(declarations) <= 8:
        raise ValueError('插件 business 应声明 1–8 个业务函数')
    result, seen = [], set()
    for item in declarations:
        if not isinstance(item, dict):
            raise ValueError('业务函数声明无效')
        file, qualified, label = item.get('file', '__init__.py'), item.get('function'), item.get('label', '处理消息')
        if (not isinstance(file, str) or file not in files or PurePosixPath(file).suffix != '.py' or
                '\\' in file or PurePosixPath(file).is_absolute() or '..' in PurePosixPath(file).parts or
                not isinstance(qualified, str) or not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?', qualified) or
                qualified.rsplit('.', 1)[-1] in ('__init__', 'on_load', 'on_unload') or
                not isinstance(label, str) or not 1 <= len(label) <= 32 or any(ord(char)<32 for char in label)):
            raise ValueError('业务函数的文件、名称或说明无效')
        identifier = file + ':' + qualified
        if identifier in seen:
            raise ValueError('业务函数不能重复声明')
        seen.add(identifier)
        try:
            tree = ast.parse(files[file])
        except (ValueError, SyntaxError):
            raise ValueError('插件源码存在语法错误，请先恢复默认插件') from None
        node = _node(tree, qualified)
        if node is None:
            # 旧插件没有声明时，仅提供配置；不会回退展示完整文件。
            if manifest.get('business') is None:
                continue
            raise ValueError(f'未找到业务函数：{qualified}')
        start = min([node.lineno] + [decorator.lineno for decorator in node.decorator_list]) - 1
        end = node.end_lineno
        lines = files[file].splitlines(keepends=True)
        # AST 不包含函数末尾的注释；仍属于函数体的注释也应随业务代码保存和显示。
        for index in range(end, len(lines)):
            line = lines[index]
            if not line.strip():
                continue
            indent = len(line) - len(line.lstrip())
            if line.lstrip().startswith('#') and indent > node.col_offset:
                end = index + 1
            else:
                break
        code = textwrap.dedent(''.join(lines[start:end]))
        indent = lines[start][:len(lines[start]) - len(lines[start].lstrip())]
        result.append({'id':identifier,'label':label,'file':file,'node':node,'start':start,'end':end,'indent':indent,'code':code})
    return result


def view(snapshot):
    entries = _entries(snapshot['files'])
    result = {key:value for key,value in snapshot.items() if key != 'files'}
    result['functions'] = {entry['id']:entry['code'] for entry in entries}
    result['function_labels'] = {entry['id']:entry['label'] for entry in entries}
    return result


def merge(files, functions):
    entries = _entries(files)
    if not isinstance(functions, dict) or set(functions) != {entry['id'] for entry in entries}:
        raise ValueError('业务函数已变化，请重新读取；只允许修改当前插件展示的业务函数')
    replacements = {}
    for entry in entries:
        code = functions[entry['id']]
        if not isinstance(code, str) or not code.strip() or len(code.encode('utf-8')) > 256 * 1024:
            raise ValueError('业务函数应为非空 Python 代码，最多 256 KiB')
        code = textwrap.dedent(code).replace('\r\n', '\n').replace('\r', '\n')
        try:
            tree = ast.parse(code)
            compile(code, entry['label'], 'exec')
        except (SyntaxError, ValueError) as error:
            raise ValueError(f"{entry['label']} 第 {getattr(error,'lineno',1) or 1} 行：{getattr(error,'msg','包含无效字符')}") from None
        original = entry['node']
        if (len(tree.body) != 1 or type(tree.body[0]) is not type(original)):
            raise ValueError('只修改当前业务函数内的代码，不要添加文件级代码或其他函数')
        node = tree.body[0]
        signature = lambda value: (value.name, ast.dump(value.args),
                                  [ast.dump(item) for item in value.decorator_list],
                                  [ast.dump(item) for item in getattr(value, 'type_params', ())],
                                  ast.dump(value.returns) if value.returns is not None else None)
        if signature(node) != signature(original):
            raise ValueError('请保留函数名称、参数和装饰器，只修改函数内的业务逻辑')
        replacement = textwrap.indent(code.rstrip('\n') + '\n', entry['indent'])
        replacements.setdefault(entry['file'], []).append((entry['start'], entry['end'], replacement))
    result = dict(files)
    for file, edits in replacements.items():
        lines = result[file].splitlines(keepends=True)
        previous = len(lines) + 1
        for start, end, replacement in sorted(edits, reverse=True):
            if end > previous:
                raise ValueError('业务函数范围重叠，请检查插件声明')
            lines[start:end] = [replacement]
            previous = start
        result[file] = ''.join(lines)
    return result
