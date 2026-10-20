"""发布目录检查：只报告文件与问题类型，不输出密钥或个人标记原文。"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOWED_DLLS = {'native/build_text/text_bridge.dll', 'native/build/selftest_bridge.dll'}
SECRET_PATTERNS = {
    'possible_api_key': re.compile(r'(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}'),
    'possible_github_token': re.compile(r'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})'),
    'private_key': re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----'),
    'absolute_user_path': re.compile(r'[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"\x00]+', re.IGNORECASE),
    'possible_phone_number': re.compile(r'(?<![A-Za-z0-9_])1[3-9][0-9]{9}(?![A-Za-z0-9_])'),
}


def inspect(root, private_markers=()):
    findings, count, total = [], 0, 0
    forbidden_dirs = set()
    for path in sorted(root.rglob('*')):
        relative = path.relative_to(root)
        if '.git' in relative.parts:
            continue
        if path.is_dir():
            if path.name in ('.runtime','logs','__pycache__','.venv','venv','.build') or path.name.startswith(('snapshot_','ghidra_')):
                forbidden_dirs.add(relative.as_posix())
            continue
        if not path.is_file():
            continue
        count += 1
        raw = path.read_bytes()
        total += len(raw)
        label = relative.as_posix()
        if path.suffix.lower() in ('.pyc','.obj','.pdb','.lib','.exp','.exe','.dmp','.db','.sqlite','.sqlite3','.bak','.zip',
                                   '.wxgf','.hevc','.gif','.png','.jpg','.jpeg','.webp','.bmp',
                                   '.wav','.pcm','.silk','.mp3','.opus','.ogg','.flac','.aac','.amr','.m4a'):
            findings.append({'file':label,'kind':'unnecessary_or_private_artifact'})
        if path.suffix.lower() == '.dll' and label not in ALLOWED_DLLS:
            findings.append({'file':label,'kind':'unexpected_binary'})
        for kind, pattern in SECRET_PATTERNS.items():
            # DLLs and other native files can also carry UTF-16 paths or credentials.
            variants = (raw.decode('utf-8',errors='ignore'),raw.decode('utf-16le',errors='ignore'),
                        raw[1:].decode('utf-16le',errors='ignore'))
            if any(pattern.search(text) for text in variants):
                findings.append({'file':label,'kind':kind})
        if any(marker and (marker.encode('utf-8') in raw or marker.encode('utf-16le') in raw)
               for marker in private_markers):
            findings.append({'file':label,'kind':'known_private_marker'})
    findings.extend({'file':name,'kind':'runtime_or_legacy_directory'} for name in sorted(forbidden_dirs))
    for name in ('bot_config','members_config','llm_config','tts_config'):
        template = root / (name + '.example.json')
        if not template.is_file():
            findings.append({'file':template.name,'kind':'missing_config_template'})
    for filename in ('bot_config.json','bot_config.example.json'):
        path = root / filename
        if path.exists():
            data=json.loads(path.read_text(encoding='utf-8'))
            if data.get('wechat_account') not in (None,'wxid_example_robot') or data.get('wechat_pid') is not None or data.get('chat_allowlist'):
                findings.append({'file':filename,'kind':'nonempty_account_or_chat_binding'})
            if data.get('wechat_data_directory'):
                findings.append({'file':filename,'kind':'nonempty_personal_data_directory'})
            if data.get('plugins',{}).get('idiom_chain',{}).get('member_names'):
                findings.append({'file':filename,'kind':'nonempty_member_names'})
    for filename in ('members_config.json','members_config.example.json'):
        path=root/filename
        if path.exists() and json.loads(path.read_text(encoding='utf-8')).get('groups'):
            findings.append({'file':filename,'kind':'nonempty_group_bindings'})
    for filename in ('llm_config.json','llm_config.example.json','tts_config.json','tts_config.example.json'):
        path=root/filename
        if path.exists() and json.loads(path.read_text(encoding='utf-8')).get('api_key'):
            findings.append({'file':filename,'kind':'nonempty_api_key'})
        if filename.startswith('tts_config') and path.exists() and json.loads(path.read_text(encoding='utf-8')).get('default_voice'):
            findings.append({'file':filename,'kind':'nonempty_personal_voice'})
    return {'passed':not findings,'files_scanned':count,'bytes_scanned':total,'findings':findings}


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--private-markers-file',type=Path,help='可选：发布目录外的本地标记清单，不会输出其内容')
    args=parser.parse_args()
    markers=[]
    if args.private_markers_file:
        data=json.loads(args.private_markers_file.read_text(encoding='utf-8'))
        markers=data.get('private_markers',[]) if isinstance(data,dict) else data
        if not isinstance(markers,list) or not all(isinstance(value,str) for value in markers):
            parser.error('标记清单应为字符串列表，或含 private_markers 列表的 JSON 对象')
    result=inspect(ROOT,markers)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['passed'] else 1)
