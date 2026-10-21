"""helper 本地网页管理台：python web.py。"""
import argparse
import logging
from pathlib import Path
import sys
import json
import threading
import webbrowser

from main import configure_logging
from wxbot_framework.config import ConfigStore, initialize_configs
from wxbot_framework.locking import InstanceLock
from wxbot_framework.shutdown import ShutdownHandlers
from wxbot_framework.web_panel import PanelController, PanelServer

ROOT = Path(__file__).resolve().parent


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765, help='本机端口，默认 8765')
    parser.add_argument('--no-browser', action='store_true', help='不自动打开浏览器')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--workspace', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        return worker_main(args.workspace)
    if not 1 <= args.port <= 65535:
        parser.error('端口应为 1–65535')
    controller = server = None
    try:
        initialize_configs(ROOT)
        configure_logging(ConfigStore(ROOT / 'bot_config.json').load(), directory=ROOT / 'logs/panel')
        with InstanceLock(ROOT / '.runtime/web.lock'):
            from wxbot_framework.robot_hub import RobotHub
            controller = RobotHub(ROOT)
            server = PanelServer(controller, args.port)
            url = f'http://127.0.0.1:{server.server_address[1]}'
            print(f'管理台已启动：{url}\n在网页点击启动机器人；退出管理服务请按 Ctrl+C。', flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            with ShutdownHandlers(controller):
                while not controller.stop_event.is_set():
                    server.handle_request()
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        logging.getLogger('WebPanel').error('管理台启动失败：%s', error)
        return 1
    finally:
        if server is not None:
            server.server_close()
        if controller is not None:
            controller.close()


def worker_main(workspace):
    if workspace is None:
        raise ValueError('缺少机器人工作目录')
    workspace = workspace.resolve()
    if workspace != ROOT and not workspace.is_relative_to((ROOT / '.runtime/robots').resolve()):
        raise ValueError('机器人工作目录不在项目允许范围')
    controller = PanelController(ROOT, workspace=workspace)
    configure_logging(controller.store.load(), directory=workspace / 'logs', console_enabled=False)
    # configure_logging(force=True) 会替换根处理器，重新接上本机器人的日志缓冲。
    logging.getLogger().addHandler(controller.logs)
    server = PanelServer(controller, 0)
    def parent_lifetime():
        try:
            sys.stdin.buffer.read(1)
        finally:
            controller.stop_event.set()
    threading.Thread(target=parent_lifetime, name='PanelOwner', daemon=True).start()
    print(json.dumps({'port': server.server_address[1], 'token': server.token}), flush=True)
    try:
        with ShutdownHandlers(controller):
            while not controller.stop_event.is_set():
                server.handle_request()
    finally:
        server.server_close()
        controller.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
