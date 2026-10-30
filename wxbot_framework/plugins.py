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
from .plugin_sources import PluginSources
from .scheduled_actions import actions_from_manifest, validate_params

logger = logging.getLogger("Plugins")


class ScheduledService:
    """将定时回调绑定到触发时的微信连接和收发白名单。"""
    METHODS = {'send_text', 'send_image', 'send_file', 'send_voice', 'send_emoticon',
               'send_at_text', 'export_emoticons', 'group_members', 'group_member_name',
               'group_member_role', 'contacts', 'accept_friend'}
    SENDS = {'send_text', 'send_image', 'send_file', 'send_voice', 'send_emoticon', 'send_at_text'}

    def __init__(self, service, session, allowlist, valid):
        self._service, self._session, self._allowlist = service, session, frozenset(allowlist)
        self._valid = valid

    def __getattr__(self, name):
        if name not in self.METHODS:
            raise AttributeError(name)
        method = getattr(self._service, name)
        def call(*args, **kwargs):
            if not self._valid():
                raise RuntimeError('定时任务已取消或停止')
            requested = kwargs.get('expected_session')
            if requested is not None and requested != self._session:
                raise RuntimeError('定时任务所属的微信连接已变化')
            if name in self.SENDS and self._allowlist and (not args or args[0] not in self._allowlist):
                raise RuntimeError('目标不在收发白名单中')
            kwargs['expected_session'] = self._session
            return method(*args, **kwargs)
        return call


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
        self.sources = PluginSources(self.directory, store.path.parent)
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
                directory = self.sources.path(name)
                path = directory / "__init__.py"
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
                                member_store=self.member_store, speech=self.speech, plugin_active=self.is_active)
        context.project_root = self.directory.parent
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

    def dispatch(self, message, *, only=None):
        with self._lock:
            records = tuple(self.records.values())
        for record in records:
            if (only is None or record.spec.name == only) and message.type in record.spec.message_types:
                record.enqueue(message)

    def run_scheduled(self, action_id, params, session, allowlist, valid=lambda: True):
        parts = action_id.split('.') if isinstance(action_id, str) else []
        if len(parts) != 3 or parts[0] != 'plugin':
            raise ValueError('定时动作不存在')
        name = validate_name(parts[1])
        with self._lock:
            record = self.records.get(name)
        if record is None:
            raise RuntimeError('对应插件未启用或未加载')
        # 和普通回调串行；重载/停用时不能同时执行两个实例。
        if not record.callback_lock.acquire(timeout=2):
            raise RuntimeError('插件正忙，跳过定时动作')
        try:
            if not record.active or not self.service.session_is_current(session) or not valid():
                raise RuntimeError('插件或微信连接已停止')
            manifest_path = record.spec.path.parent / 'plugin.json'
            manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
            action = next((item for item in actions_from_manifest(name, manifest) if item['id'] == action_id), None)
            if action is None:
                raise ValueError('插件已不提供这个定时动作')
            values = validate_params(action, params)
            callback = getattr(record.instance, action['method'], None)
            if not callable(callback):
                raise ValueError('插件定时动作没有对应方法')
            context = copy.copy(record.context)
            context._service = ScheduledService(self.service, session, allowlist, valid)
            context.session_id = session
            result = callback(values, context)
            if result is False:
                raise RuntimeError('插件报告任务未完成')
            return result
        finally:
            record.callback_lock.release()

    def status(self):
        specs = self.discover()
        with self._lock:
            rows = []
            for name in sorted(set(specs) | set(self.records)):
                record = self.records.get(name)
                spec = specs.get(name)
                missing = spec.requires - self.capabilities if spec else frozenset()
                rows.append({"name": name, "active": bool(record and record.active),
                             "description": spec.description if spec else record.spec.description,
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
