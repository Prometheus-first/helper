"""运行离线回归测试；不启动管理台，也不连接真实微信。"""
import argparse
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pattern', default='test_*.py', help='测试文件匹配模式')
    parser.add_argument('--verbose', action='store_true', help='显示每项测试结果')
    args = parser.parse_args()
    sys.path[:0] = [str(ROOT), str(ROOT / 'native'), str(ROOT / 'tests')]
    suite = unittest.defaultTestLoader.discover(str(ROOT / 'tests'), pattern=args.pattern)
    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
