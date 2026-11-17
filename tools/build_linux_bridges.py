"""编译 Linux 管理台桥接；原子替换文件，不覆盖已映射的共享库。"""
from pathlib import Path
import os
import subprocess
import tempfile
from wxbot_framework.locking import InstanceLock

ROOT = Path(__file__).resolve().parents[1]


def build(root=ROOT):
    root = Path(root)
    target = root / 'native/linux/build/panel'
    target.mkdir(parents=True, exist_ok=True)
    with InstanceLock(root / '.runtime/linux-build.lock'):
        for name in ('text_bridge', 'send_bridge'):
            source = root / 'native/linux' / (name + '.c')
            output = target / (name + '.so')
            dependencies = (source, root / 'native/linux/personal_account.h')
            if output.exists() and output.stat().st_mtime_ns >= max(p.stat().st_mtime_ns for p in dependencies):
                continue
            with tempfile.TemporaryDirectory(prefix='.build-', dir=target) as temporary:
                candidate = Path(temporary) / output.name
                subprocess.run(['gcc', '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                    '-fPIC', '-shared', '-pthread', str(source), '-ldl', '-o', str(candidate)], check=True)
                os.replace(candidate, output)
    return target


if __name__ == '__main__':
    print('Linux 桥接已准备好：' + str(build()))
