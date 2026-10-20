"""微信机器人框架的统一入口：python main.py。"""
from __future__ import annotations

import argparse
import json
import logging
import math
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys

from wxbot_framework.backend import CAPABILITIES, TEXT_CAPABILITIES
from wxbot_framework.config import ConfigStore, EXAMPLE_ACCOUNT, account_is_unbound, initialize_configs
from wxbot_framework.locking import InstanceLock
from wxbot_framework.members import MemberStore
from wxbot_framework.plugins import PluginManager
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import ServiceFacade
from wxbot_framework.startup import ensure_account_binding
from wxbot_framework.shutdown import ShutdownHandlers

ROOT = Path(__file__).resolve().parent


def configure_logging(config):
    directory = ROOT / "logs"
    directory.mkdir(exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file = RotatingFileHandler(directory / "bot.log", maxBytes=2 * 1024 * 1024,
                               backupCount=3, encoding="utf-8")
    file.setFormatter(formatter)
    logging.basicConfig(level=config.get("log_level", "INFO").upper(), handlers=[console, file], force=True)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "bot_config.json")
    parser.add_argument("--check", action="store_true", help="检查配置、清单及启用插件语法，不注入微信")
    parser.add_argument("--list-plugins", action="store_true", help="列出可用插件，不注入微信")
    parser.add_argument("--run-seconds", type=float, default=0, help="有限时长检查；默认 0 持续运行")
    args = parser.parse_args()
    if not math.isfinite(args.run_seconds) or args.run_seconds < 0:
        parser.error("运行时长必须是非负有限数值")
    try:
        store = ConfigStore(args.config)
        if store.path == (ROOT / "bot_config.json").resolve():
            initialize_configs(ROOT)
        config = store.load()
        if args.check or args.list_plugins:
            # 检查模板时不需要先绑定账号，也不探测微信；仅核对成员配置的结构。
            validation = dict(config)
            if account_is_unbound(validation.get("wechat_account")):
                validation["wechat_account"] = EXAMPLE_ACCOUNT
            members = MemberStore.from_config(validation, store.path.parent)
            capabilities = TEXT_CAPABILITIES if config.get("backend") == "native_text" else CAPABILITIES
            plugins = PluginManager(ROOT / "plugins", store, ServiceFacade(), capabilities, member_store=members)
            specs = plugins.discover()
            if plugins.load_errors:
                raise ValueError(str(plugins.load_errors))
            for spec in specs.values():
                enabled = config.get("plugins", {}).get(spec.name, {}).get("enabled", spec.default_enabled)
                if args.check and enabled:
                    for source in spec.path.parent.rglob("*.py"):
                        compile(source.read_bytes(), str(source), "exec")
                    if spec.name in ("idiom_chain", "chat_summary", "mention_chat"):
                        from wxbot_framework.llm import LLMClient
                        LLMClient.from_file(store.path.parent / config.get("llm_config_file", "llm_config.json"))
                    if spec.name == "voice_reply":
                        from wxbot_framework.tts import FishTTS
                        FishTTS.from_file(store.path.parent / config.get("tts_config_file", "tts_config.json"))
                print(json.dumps({"name": spec.name, "enabled": enabled, "api": spec.api,
                                  "description": spec.description,
                                  "missing_capabilities": sorted(spec.requires - plugins.capabilities)}, ensure_ascii=False))
            if args.check:
                print("配置与插件语法检查通过")
            return 0
        configure_logging(config)
        with InstanceLock(ROOT / ".runtime/bot.lock"):
            ensure_account_binding(ROOT, store, timeout=args.run_seconds)
            app = BotApplication(ROOT, store)
            with ShutdownHandlers(app):
                app.run(seconds=args.run_seconds)
        return 0
    except KeyboardInterrupt:
        logging.getLogger("Main").info("账号识别已取消")
        return 0
    except Exception:
        logging.getLogger("Main").exception("启动或运行失败")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
