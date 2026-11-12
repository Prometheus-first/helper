"""启动微信前保护当前已验证构建；也可恢复更新程序。"""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins.wechat_update_guard.guard import GuardError, cleanup_backups, protect, restore


def main():
    parser = argparse.ArgumentParser(description='微信 4.2.0.12 更新保护')
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument('--protect', action='store_true', help='封锁已验证的更新器及更新缓存路径，清除升级包')
    choice.add_argument('--restore', action='store_true', help='解除本工具的路径占位并恢复更新器，不放回升级包')
    choice.add_argument('--cleanup-backups', action='store_true', help='清理旧更新缓存备份，保留最新一份和更新器恢复备份')
    args = parser.parse_args()
    try:
        print(cleanup_backups() if args.cleanup_backups else (protect if args.protect else restore)(ROOT))
        return 0
    except GuardError as error:
        print(f'操作未完成：{error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
