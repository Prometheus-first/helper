"""插件发现、独立消息队列和生命周期管理。"""
from __future__ import annotations

import copy
import importlib.util
import json
import logging
import queue
import sys
import threading
import types
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import validate_name
from .sdk import PluginContext, LegacyService
from .speech import SpeechController

logger = logging.getLogger("Plugins")


@dataclass(frozen=True)
class PluginSpec:
    name: str
    path: Path
    api: str
    description: str
    requires: frozenset
    message_types: frozenset = frozenset({1})
    default_enabled: bool = True


class PluginRecord:
    def __init__(self, spec, instance, context, module_name, queue_size):
        self.spec, self.instance, self.context = spec, instance, context
        self.module_name = module_name
        self.queue = queue.Queue(maxsize=queue_size)
        self.stop_event = threading.Event()
        self.callback_lock = threading.RLock()
        self.active = True
        self.handled = self.errors = self.dropped = 0
        self.last_error = ""
        self.thread = threading.Thread(target=self._run, name=f"Plugin.{spec.name}", daemon=True)

    def enqueue(self, message):
        if not self.active:
            return
        try:
            self.queue.put_nowait(message)
        except queue.Full:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                self.context.logger.warning("插件队列已满，累计丢弃 %d 条", self.dropped)

    def _run(self):
        try:
            while not self.stop_event.is_set():
                try:
                    message = self.queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    with self.callback_lock:
                        if not self.active:
                            continue
                        try:
                            if self.spec.api == "legacy":
                                service = LegacyService(self.context._service, message.session_id)
                                self.instance.on_message(message.protocol, copy.deepcopy(dict(message.data)), service)
                            else:
                                self.instance.on_message(message, self.context)
                            self.handled += 1
                        except BaseException as error:
                            self.errors += 1
                            self.last_error = str(error)
                            self.context.logger.exception("插件处理消息失败；继续处理后续消息")
                finally:
                    self.queue.task_done()
        finally:
            self.unload()

    def unload(self):
        try:
            if self.spec.api == "v1" and callable(getattr(self.instance, "on_unload", None)):
                self.instance.on_unload(self.context)
        except (Exception, SystemExit):
            self.context.logger.exception("插件资源释放失败")
        finally:
            for name in list(sys.modules):
                if name == self.module_name or name.startswith(self.module_name + "."):
                    sys.modules.pop(name, None)

    def deactivate(self, *, force=False):
        # 热重载遇到正在执行的长回调时保留原插件，避免两份实例同时回复。
        acquired = self.callback_lock.acquire(timeout=0 if force else 2)
        if not acquired and not force:
            raise RuntimeError(f"插件 {self.spec.name} 正在处理消息，请稍后重试")
        try:
            self.active = False
            self.stop_event.set()
        finally:
            if acquired:
                self.callback_lock.release()

    def join(self):
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            self.context.logger.warning("插件退出仍在等待回调或 on_unload；主程序继续清理连接")


class PluginManager:
    def __init__(self, directory, store, service, capabilities, queue_size=128, *, member_store=None):
        self.directory = Path(directory).resolve()
        self.store, self.service = store, service
        self.capabilities = frozenset(capabilities)
        self.queue_size = queue_size
        self._lock = threading.RLock()
        self.records = {}
        self.load_errors = {}
        self.config = store.load()
        self.member_store = member_store
        self.speech = SpeechController(
            self.store.path.parent / self.config.get("tts_config_file", "tts_config.json"),
            self.store.path.parent / ".runtime/tts", member_store, lambda: self.is_active("voice_reply"))

    def is_active(self, name):
        with self._lock:
            record = self.records.get(name)
            return bool(record and record.active)

    def discover(self):
        specs = {}
        for directory in sorted(self.directory.iterdir()):
            if not directory.is_dir() or directory.name.startswith("_"):
                continue
            path = directory / "__init__.py"
            if not path.is_file():
                continue
            try:
                name = validate_name(directory.name)
                if not path.resolve().is_relative_to(self.directory):
                    raise ValueError("插件源文件必须位于插件目录内")
                manifest_path = directory / "plugin.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig")) if manifest_path.exists() else {}
                api = manifest.get("api", "v1")
                requires = manifest.get("requires", ["text.receive"])
                message_types = manifest.get("message_types", [1])
                default_enabled = manifest.get("default_enabled", True)
                if type(default_enabled) is not bool:
                    raise ValueError("plugin.json 的 default_enabled 必须是布尔值")
                if api not in ("v1", "legacy") or not isinstance(requires, list) or not all(isinstance(x, str) for x in requires):
                    raise ValueError("plugin.json 的 api / requires 不合法")
                if (not isinstance(message_types, list) or not message_types or
                        any(type(value) is not int or value <= 0 for value in message_types)):
                    raise ValueError("plugin.json 的 message_types 必须是非空正整数列表")
                specs[name] = PluginSpec(name, path, api, str(manifest.get("description", "")),
                                         frozenset(requires), frozenset(message_types), default_enabled)
            except Exception as error:
                self.load_errors[directory.name] = str(error)
                logger.error("插件 %s 无法发现：%s", directory.name, error)
        return specs

    def _prepare(self, spec):
        missing = spec.requires - self.capabilities
        if missing:
            raise RuntimeError(f"缺少后端能力：{', '.join(sorted(missing))}")
        options = self.config.get("plugins", {}).get(spec.name, {})
        context = PluginContext(spec.name, options, self.service, self.capabilities,
                                llm_config_path=self.store.path.parent / self.config.get("llm_config_file", "llm_config.json"),
                                member_store=self.member_store, speech=self.speech)
        # 每次使用独立模块名并重新编译源文件，热重载不复用旧入口字节码。
        if "_wxbot_plugins" not in sys.modules:
            package = types.ModuleType("_wxbot_plugins")
            package.__path__ = [str(self.directory)]
            sys.modules[package.__name__] = package
        module_name = f"_wxbot_plugins.{spec.name}_{uuid.uuid4().hex}"
        module_spec = importlib.util.spec_from_file_location(module_name, spec.path,
                                                            submodule_search_locations=[str(spec.path.parent)])
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_name] = module
        record = None
        try:
            exec(compile(spec.path.read_bytes(), str(spec.path), "exec"), module.__dict__)
            instance = module.Plugin()
            if not callable(getattr(instance, "on_message", None)):
                raise TypeError("Plugin 必须实现 on_message")
            record = PluginRecord(spec, instance, context, module_name, self.queue_size)
            if spec.api == "v1" and callable(getattr(instance, "on_load", None)):
                instance.on_load(context)
            return record
        except BaseException:
            if record is not None:
                record.unload()
            else:
                for name in list(sys.modules):
                    if name == module_name or name.startswith(module_name + "."):
                        sys.modules.pop(name, None)
            raise

    def load_one(self, name):
        validate_name(name)
        spec = self.discover().get(name)
        if spec is None:
            raise ValueError(f"未找到插件：{name}")
        candidate = self._prepare(spec)
        previous = self.records.get(name)
        try:
            with self._lock:
                if previous is not None:
                    previous.deactivate()
                self.records[name] = candidate
                candidate.thread.start()
            if previous is not None:
                previous.join()
            self.load_errors.pop(name, None)
            logger.info("已加载插件：%s (%s)", name, spec.api)
        except Exception:
            candidate.unload()
            raise

    def disable(self, name, *, persist=True):
        validate_name(name)
        with self._lock:
            record = self.records.get(name)
            if record is not None:
                record.deactivate()
            if persist:
                try:
                    self.config = self.store.set_enabled(name, False)
                except Exception:
                    # 回调已停止，明确报告配置保存错误；不会重新启动半关闭实例。
                    self.records.pop(name, None)
                    raise
            self.records.pop(name, None)
        if record is not None:
            record.join()
        logger.info("已停用插件：%s", name)

    def enable(self, name):
        if name not in self.discover():
            raise ValueError(f"未找到插件：{name}")
        self.config = self.store.set_enabled(name, True)
        self.load_one(name)

    def reload(self, name=None):
        self.config = self.store.load()
        self.speech.path = self.store.path.parent / self.config.get("tts_config_file", "tts_config.json")
        specs = self.discover()
        if name is not None:
            validate_name(name)
            if name not in specs and name not in self.records:
                raise ValueError(f"未找到插件：{name}")
        targets = [name] if name else sorted(set(specs) | set(self.records))
        for target in targets:
            try:
                if target not in specs or not self.config.get("plugins", {}).get(target, {}).get("enabled", specs[target].default_enabled):
                    self.disable(target, persist=False)
                else:
                    self.load_one(target)
            except (Exception, SystemExit) as error:
                self.load_errors[target] = str(error)
                logger.exception("插件 %s 加载失败，其他插件继续运行", target)

    def dispatch(self, message):
        with self._lock:
            records = tuple(self.records.values())
        for record in records:
            if message.type in record.spec.message_types:
                record.enqueue(message)

    def status(self):
        specs = self.discover()
        with self._lock:
            rows = []
            for name in sorted(set(specs) | set(self.records)):
                record = self.records.get(name)
                spec = specs.get(name)
                missing = spec.requires - self.capabilities if spec else frozenset()
                rows.append({"name": name, "active": bool(record and record.active),
                             "enabled": self.config.get("plugins", {}).get(name, {}).get("enabled", spec.default_enabled if spec else True),
                             "api": spec.api if spec else "unknown", "missing_capabilities": sorted(missing),
                             "handled": record.handled if record else 0, "errors": record.errors if record else 0,
                             "dropped": record.dropped if record else 0,
                             "error": self.load_errors.get(name) or (record.last_error if record else "")})
            return rows

    def shutdown(self):
        with self._lock:
            records = tuple(self.records.values())
            self.records.clear()
            for record in records:
                record.deactivate(force=True)
        self.speech.close()
        for record in records:
            record.join()
