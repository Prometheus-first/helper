"""好友、群聊和文件传输助手的原生文本、图片与语音服务。"""
from __future__ import annotations

import logging
import time
import ctypes as c
from collections import deque
from pathlib import Path

from filehelper_service import NativeFileHelperService, ReceivedMediaMessage
from wxbot_framework.mentions import parse_at_members
from run_selftest import TEXT_DLL, K, check, modules, wechat_candidates, run
from wxbot_framework.text import TextRouter, validate_target, validate_mentions
from wxbot_framework.account import read_current_account, read_current_profile, select_account_process, validate_account
from wxbot_framework.contacts import read_contact_name

logger = logging.getLogger("NativeText")


def run_text(mode, **options):
    return run(mode, bridge_path=TEXT_DLL, **options)


def _inspect_identity(pid, reader):
    core = modules(pid).get("weixin.dll")
    if not core:
        return None
    process = check(K.OpenProcess(0x410, False, pid), "只读核对微信账号")
    try:
        def read(address, size):
            buffer = c.create_string_buffer(size)
            count = c.c_size_t()
            check(K.ReadProcessMemory(process, address, buffer, size, c.byref(count)), "只读核对账号字段")
            return buffer.raw[:count.value]
        return reader(read, core[0])
    finally:
        K.CloseHandle(process)


def inspect_account(pid):
    """只读核对候选进程的当前账号，不向候选进程注入 DLL。"""
    return _inspect_identity(pid, read_current_account)


def inspect_profile(pid):
    """首次绑定只读取 wxid 和微信名，名称暂不可读时仍可使用 wxid。"""
    def read_profile(read, base):
        profile = read_current_profile(read, base)
        if profile:
            return profile
        account = read_current_account(read, base)
        return {"wxid": account, "name": ""} if account else None
    return _inspect_identity(pid, read_profile)


def find_account_process(account=None):
    candidates = []
    for pid in wechat_candidates():
        try:
            candidates.append((pid, inspect_account(pid)))
        except OSError:
            # 枚举后退出的进程不参与选择。
            pass
    return select_account_process(candidates, account)


class NativeTextService(NativeFileHelperService):
    _dll = TEXT_DLL
    _poll_export = b"WxPollMediaMessageV3"
    _message_type = ReceivedMediaMessage
    _run = staticmethod(run_text)

    def __init__(self, *, account=None, media_directory=None, image_allowlist=(),
                 wechat_data_directory=None, emoticon_directory=None, voice_directory=None, **options):
        super().__init__(**options)
        self.preferred_account = validate_account(account) if account is not None else None
        self.router = TextRouter(include_self_groups=True)
        self.chats = {}
        self._next_account_check = 0
        self._sent_texts = deque(maxlen=64)
        self.mention_messages = 0
        self.media_directory = Path(media_directory or Path(__file__).resolve().parent.parent / ".runtime/images")
        self.image_allowlist = frozenset(image_allowlist)
        self._image_receiver = None
        self.wechat_data_directory = Path(wechat_data_directory) if wechat_data_directory is not None else None
        self.emoticon_directory = Path(emoticon_directory or
                                       Path(__file__).resolve().parent.parent / ".runtime/emoticons")
        self.voice_directory = Path(voice_directory or
                                    Path(__file__).resolve().parent.parent / ".runtime/voices")
        self.last_send_result = None

    @property
    def self_wxid(self):
        return self.router.self_wxid

    def start(self):
        self.router = TextRouter(include_self_groups=True)
        self.chats = {}
        self._sent_texts.clear()
        if self.pid is None:
            self.pid = find_account_process(self.preferred_account)
        elif self.preferred_account and inspect_account(self.pid) != self.preferred_account:
            # 手动指定 PID 时也先只读核对，避免向其他账号加载 DLL。
            raise RuntimeError("目标进程登录的不是绑定的机器人账号，等待正确账号登录")
        result = super().start()
        logger.info("当前登录微信ID=%s；好友和群聊文本、图片已就绪", self.self_wxid)
        return result

    def _read_account(self):
        return read_current_account(self._channel.read_memory, self._channel.core_base)

    def _prepare_channel(self):
        account = self._read_account()
        if not account:
            raise RuntimeError("微信尚未完成登录，自动等待当前账号就绪")
        if self.preferred_account and account != self.preferred_account:
            raise RuntimeError("目标进程登录的不是绑定的机器人账号，等待正确账号登录")
        self.router.set_account(account)
        self._next_account_check = time.monotonic() + 1
        from wxbot_framework.image_metadata import ImageStore
        from wxbot_framework.image_receiver import ImageReceiver
        self._image_receiver = ImageReceiver(ImageStore(self.media_directory, account),
            self._download_image, self._deliver_image,
            lambda: not self._stop.is_set() and self._owns_bridge and self.self_wxid == account)

    def _download_image(self, metadata, destination):
        filekey = metadata.cdnmidimgurl or metadata.filekey or metadata.cdnbigimgurl or metadata.cdnthumburl
        aeskey = metadata.aeskey if (metadata.cdnmidimgurl or metadata.filekey or metadata.cdnbigimgurl) else (metadata.cdnthumbaeskey or metadata.aeskey)
        image_type = (2 if metadata.cdnmidimgurl or metadata.filekey else
                      1 if metadata.cdnbigimgurl else 3)
        if not filekey or not aeskey:
            return False
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                return False
            self._check_account(force=True)
            result = self._run("--download-image", pid=self.pid, keep_loaded=True,
                preserve_report=self.preserve_reports,
                download_image={"destination": str(destination), "filekey": filekey,
                                "aeskey": aeskey, "image_type": image_type})
            self._check_account(force=True)
            return result.get("success") is True

    def _deliver_image(self, data):
        if self._stop.is_set():
            return
        logger.info("图片接收 | 会话ID=%s | 状态=%s | 文件=%s", data["chat_id"],
                    data.get("image_status"), data.get("image_path", ""))
        for handler in list(self.msg_handlers):
            try:
                handler(self.pid, 11046, data, self)
            except Exception:
                logger.exception("图片消息处理器异常")

    def stop(self):
        self._stop.set()
        if self._image_receiver is not None:
            self._image_receiver.close(timeout=30)
            if self._image_receiver.is_alive:
                raise RuntimeError("图片原生下载尚未结束，保留桥接等待清理重试")
            self._image_receiver = None
        return super().stop()

    def _check_account(self, *, force=False):
        if not force and time.monotonic() < self._next_account_check:
            return
        account = self._read_account()
        if not account or account != self.self_wxid:
            error = RuntimeError("微信已退出登录或当前账号发生变化，重新连接并自动识别")
            self.last_error = error
            self._stop.set()
            raise error
        self._next_account_check = time.monotonic() + 1

    def _before_poll(self):
        self._check_account()

    def _sender_wechat_name(self, wxid):
        if self._channel is None:
            return None
        name = read_contact_name(self._channel.read_memory, self._channel.core_base, wxid,
                                 expected_account=self.self_wxid)
        if name is None and wxid == self.self_wxid:
            profile = read_current_profile(self._channel.read_memory, self._channel.core_base)
            name = profile["name"] if profile and profile["wxid"] == wxid else None
        return name

    def _map_message(self, message):
        if message.type not in (1, 3):
            return None
        previous = self.self_wxid
        previous_unparsed = self.router.unparsed_groups
        body = (getattr(message, "image_xml", b"") if message.type == 3 else message.text).decode("utf-8")
        data = self.router.convert(message.sender.decode("utf-8"), message.receiver.decode("utf-8"),
                                   body, sequence=message.sequence, msg_type=message.type,
                                   thread_id=message.thread_id)
        if self.self_wxid and not previous:
            logger.info("当前账号已识别；好友和群聊文本已就绪")
        if data:
            name = self._sender_wechat_name(data["sender_id"]) if data["sender_id"] else None
            if name:
                data["sender_wechat_name"] = name
            source = getattr(message, "mention_source", b"").decode("utf-8", errors="replace")
            data["at_members"] = parse_at_members(source)
            data["is_at_self"] = bool(data["room_wxid"] and self.self_wxid in data["at_members"])
            if data["at_members"]:
                self.mention_messages += 1
            if data["is_self"] and any(chat == data["chat_id"] and text == data["msg"] and
                                       time.monotonic() - sent_at < 120
                                       for chat, text, sent_at in tuple(self._sent_texts)):
                self.router.ignored_self += 1
                data["bot_generated"] = True
                data["dispatch_to_plugins"] = False
            elif data["is_at_self"]:
                data["dispatch_to_plugins"] = True
            self.chats[data["chat_id"]] = {"chat_id": data["chat_id"],
                                           "is_group": bool(data["room_wxid"])}
            if message.type == 3 and self._image_receiver is not None:
                if not self.image_allowlist or data["chat_id"] in self.image_allowlist:
                    self._image_receiver.submit(data)
                return None
        elif self.router.unparsed_groups > previous_unparsed:
            # 第一条失败时提示；后续通过 status 查看累计数。
            if self.router.unparsed_groups == 1:
                logger.warning("群文本作者格式未匹配，已跳过；需要用测试群消息核对实际布局")
        return data

    def send_text(self, to_wxid, content):
        return self._send_text(to_wxid, content)

    def send_at_text(self, room_wxid, content, at_list):
        members = validate_mentions(room_wxid, at_list)
        return self._send_text(room_wxid, content, mentions=members)

    def send_image(self, to_wxid, path):
        from wxbot_framework.images import validate_image_path
        validate_target(to_wxid)
        path = validate_image_path(path)
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                raise RuntimeError("原生图片服务没有运行")
            self._check_account(force=True)
            self.last_result = self._run("--dispatch", pid=self.pid, image_path=path,
                                        target=to_wxid, keep_loaded=True,
                                        preserve_report=self.preserve_reports)
            return self.last_result["success"]

    def send_emoticon(self, to_wxid, path):
        from wxbot_framework.emoticons import validate_emoticon_path
        validate_target(to_wxid)
        path = validate_emoticon_path(path)
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                raise RuntimeError("原生表情服务没有运行")
            self._check_account(force=True)
            self.last_result = self._run("--dispatch", pid=self.pid, emoticon_path=path,
                                        account=self.self_wxid, target=to_wxid, keep_loaded=True,
                                        preserve_report=self.preserve_reports)
            self._check_account(force=True)
            return self.last_result["success"]

    def send_voice(self, to_wxid, path):
        from wxbot_framework.voices import prepare_voice
        validate_target(to_wxid)
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                raise RuntimeError("原生语音服务没有运行")
            self._check_account(force=True)
            account = validate_account(self.self_wxid)
            voice_root = self.voice_directory.resolve()
            account_directory = (voice_root / account).resolve()
            if account_directory.parent != voice_root:
                raise ValueError("语音账号目录必须位于当前项目的语音目录内")
            # 转码和快照归当前账号所有；原生调用结束前保持快照文件有效。
            with prepare_voice(path, account_directory) as voice:
                if not self._owns_bridge or self._stop.is_set():
                    raise RuntimeError("微信连接已结束，取消待发送语音")
                self._check_account(force=True)
                if self.self_wxid != account:
                    raise RuntimeError("微信账号已发生变化，取消待发送语音")
                self.last_result = self.last_send_result = self._run(
                    "--dispatch", pid=self.pid, keep_loaded=True,
                    preserve_report=self.preserve_reports, target=to_wxid,
                    voice_path=str(voice.path), voice_duration_ms=voice.duration_ms,
                    account=account)
                self._check_account(force=True)
                return self.last_send_result.get("success", False)

    def export_emoticons(self, *, convert=True):
        from wxbot_framework.emoticon_account import read_emoticon_cache_key
        from wxbot_framework.emoticons import export_local_emoticons
        if type(convert) is not bool:
            raise ValueError("convert 必须是布尔值")
        if self.wechat_data_directory is None:
            raise ValueError("请先在本地配置 wechat_data_directory，指向当前账号的数据目录")
        with self._calls:
            if not self._owns_bridge or self._stop.is_set() or self._channel is None:
                raise RuntimeError("原生表情服务没有运行")
            self._check_account(force=True)
            account = self.self_wxid
            key = read_emoticon_cache_key(self._channel.read_memory, self._channel.core_base,
                                          expected_account=account)

            def is_current():
                return (self._owns_bridge and not self._stop.is_set() and
                        self.self_wxid == account and self._read_account() == account)

            try:
                result = export_local_emoticons(self.wechat_data_directory, self.emoticon_directory,
                                               key, expected_account=account, convert=convert,
                                               is_current=is_current)
                self._check_account(force=True)
                return result
            finally:
                # 不持久化密钥；引用只活到这次显式导出结束。
                del key

    def _send_text(self, to_wxid, content, *, mentions=None):
        validate_target(to_wxid)
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                raise RuntimeError("原生文本服务没有运行")
            self._check_account(force=True)
            self._sent_texts.append((to_wxid, content, time.monotonic()))
            self.last_result = self._run("--dispatch", pid=self.pid, text=content, target=to_wxid,
                                        keep_loaded=True, preserve_report=self.preserve_reports, mentions=mentions)
            if content.startswith("WXBOT_"):
                logger.info("测试文本发送完成：chat_id=%s success=%s native_thread=%s",
                            to_wxid, self.last_result["success"],
                            self.last_result.get("report", {}).get("thread_id"))
            return self.last_result["success"]
