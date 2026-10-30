"""好友、群聊和文件传输助手的原生文本、图片、文件及语音服务。"""
from __future__ import annotations

import logging
import time
import uuid
import ctypes as c
import hashlib
from functools import lru_cache
from collections import deque
from datetime import datetime
from pathlib import Path

from filehelper_service import NativeFileHelperService, ReceivedMediaMessageV6
from wxbot_framework.mentions import parse_at_members
from run_selftest import TEXT_DLL, K, check, modules, wechat_candidates, run
from wxbot_framework.text import TextRouter, validate_target, validate_mentions
from wxbot_framework.account import read_current_account, read_current_profile, select_account_process, validate_account
from wxbot_framework.native_profiles import ACCOUNT_DISCOVERY_PROFILES, select_memory_profile
from wxbot_framework.contacts import read_contact_name

logger = logging.getLogger("NativeText")


def run_text(mode, **options):
    return run(mode, bridge_path=TEXT_DLL, **options)


def _inspect_identity(pid, reader, *, expected_core=None):
    core = modules(pid).get("weixin.dll")
    if not core or (expected_core is not None and core != expected_core):
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


@lru_cache(maxsize=8)
def _discovery_profile_for_file(path, size, modified_ns):
    candidates = [profile for profile in ACCOUNT_DISCOVERY_PROFILES if profile.size == size]
    if not candidates:
        return None
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    stat = Path(path).stat()
    if stat.st_size != size or stat.st_mtime_ns != modified_ns:
        return None
    return next((profile for profile in candidates if profile.sha256 == digest.hexdigest()), None)


def inspect_profile(pid):
    """首次绑定只读取 wxid 和微信名，名称暂不可读时仍可使用 wxid。"""
    core = modules(pid).get('weixin.dll')
    if not core:
        return None
    path = Path(core[1])
    stat = path.stat()
    partial = _discovery_profile_for_file(str(path), stat.st_size, stat.st_mtime_ns)
    def read_profile(read, base):
        profile = read_current_profile(read, base)
        if profile:
            return profile
        account = read_current_account(read, base)
        if account:
            return {"wxid": account, "name": ""}
        if partial:
            found = read_current_profile(read, base, profiles=(partial,))
            if not found:
                account = read_current_account(read, base, profiles=(partial,))
                found = {"wxid": account, "name": ""} if account else None
            if found:
                return {**found, 'bridge_supported': False, 'wechat_version': partial.version}
        return None
    return _inspect_identity(pid, read_profile, expected_core=core)


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
    _poll_export = b"WxPollMediaMessageV7"
    _message_type = ReceivedMediaMessageV6
    _run = staticmethod(run_text)

    def __init__(self, *, account=None, media_directory=None, image_allowlist=(),
                 wechat_data_directory=None, emoticon_directory=None, voice_directory=None,
                 file_directory=None, **options):
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
        self._file_receiver = None
        self._voice_receiver = None
        self._forwarded_receiver = None
        self.file_directory = Path(file_directory or
                                   Path(__file__).resolve().parent.parent / ".runtime/files")
        self.wechat_data_directory = Path(wechat_data_directory) if wechat_data_directory is not None else None
        self.emoticon_directory = Path(emoticon_directory or
                                       Path(__file__).resolve().parent.parent / ".runtime/emoticons")
        self.voice_directory = Path(voice_directory or
                                    Path(__file__).resolve().parent.parent / ".runtime/voices")
        self.last_send_result = None
        self._friend_requests = None
        self._friend_session_id = ""
        self.wechat_version = None

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
        if self.wechat_version == '4.2.0.11':
            logger.info("当前登录微信ID=%s；4.2.0.11 文本、语音、图片及文件收发已就绪", self.self_wxid)
        else:
            logger.info("当前登录微信ID=%s；好友和群聊文本、图片、文件已就绪", self.self_wxid)
        return result

    def _read_account(self):
        return read_current_account(self._channel.read_memory, self._channel.core_base)

    def _prepare_channel(self):
        account = self._read_account()
        if not account:
            raise RuntimeError("微信尚未完成登录，自动等待当前账号就绪")
        if self.preferred_account and account != self.preferred_account:
            raise RuntimeError("目标进程登录的不是绑定的机器人账号，等待正确账号登录")
        if self._channel is not None:
            profile = select_memory_profile(self._channel.read_memory, self._channel.core_base)
            if profile is None:
                raise RuntimeError('微信版本布局尚未确认')
            self.wechat_version = profile.version
        self.router.set_account(account)
        from wxbot_framework.friend_requests import PendingFriendRequests
        self._friend_session_id = uuid.uuid4().hex
        self._friend_requests = PendingFriendRequests(account, self._friend_session_id)
        self._next_account_check = time.monotonic() + 1
        from wxbot_framework.image_metadata import ImageStore
        from wxbot_framework.image_receiver import ImageReceiver
        self._image_receiver = ImageReceiver(ImageStore(self.media_directory, account),
            self._download_image, self._deliver_image,
            lambda: not self._stop.is_set() and self._owns_bridge and self.self_wxid == account)
        from wxbot_framework.file_metadata import FileStore
        from wxbot_framework.file_receiver import FileReceiver
        self._file_receiver = FileReceiver(FileStore(self.file_directory, account),
            self._download_file, self._deliver_file,
            lambda: not self._stop.is_set() and self._owns_bridge and self.self_wxid == account)
        from wxbot_framework.forwarded_receiver import ForwardedReceiver
        self._forwarded_receiver = ForwardedReceiver(ImageStore(self.media_directory, account),
            self._download_image, self._deliver_forwarded,
            lambda: not self._stop.is_set() and self._owns_bridge and self.self_wxid == account,
            queue_size=8)
        from wxbot_framework.voice_receiver import VoiceReceiver
        self._voice_receiver = VoiceReceiver(self.voice_directory / 'received', account,
            self._deliver_voice,
            lambda: not self._stop.is_set() and self._owns_bridge and self.self_wxid == account)

    def _deliver_voice(self, data):
        if self._stop.is_set():
            return
        if data.get('voice_status') == 'failed':
            logger.warning('语音未完整缓存，撤回后无法补发原音频')
        for handler in list(self.msg_handlers):
            try:
                handler(self.pid, 11046, data, self)
            except Exception:
                logger.warning('语音接收事件处理暂不可用')

    @staticmethod
    def _voice_payload(message):
        if not isinstance(message, ReceivedMediaMessageV6):
            return b''
        offset, size = message.voice_offset, message.voice_size
        if (not size or size > 262143 or offset < len(message.image_xml) + 1 or
                offset + size > 262144):
            return b''
        # image_xml is a NUL-terminated XML prefix followed by a binary slice.
        # Never use the c_char array getter for the SILK payload: it truncates NULs.
        address = c.addressof(message) + ReceivedMediaMessageV6.image_xml.offset + offset
        return c.string_at(address, size)

    def _deliver_forwarded(self, data):
        if self._stop.is_set():
            return
        for handler in list(self.msg_handlers):
            try:
                handler(self.pid, 11046, data, self)
            except Exception:
                logger.warning('合并转发消息处理器暂不可用')

    def _download_file(self, metadata, destination):
        from wxbot_framework.files import MAX_FILE_BYTES
        # attachid 是微信应用附件标识，不能未经验证当作 CDN 地址。
        if (not metadata.cdnattachurl or not metadata.aeskey or type(metadata.size) is not int or
                not 1 <= metadata.size <= MAX_FILE_BYTES):
            return False
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                return False
            self._check_account(force=True)
            result = self._run("--download-file", pid=self.pid, keep_loaded=True,
                preserve_report=self.preserve_reports, account=self.self_wxid,
                download_file={"destination": str(destination), "filekey": metadata.cdnattachurl,
                               "aeskey": metadata.aeskey, "expected_size": metadata.size})
            self._check_account(force=True)
            return result.get("success") is True

    def _deliver_file(self, data):
        if self._stop.is_set():
            return
        # 群消息日志仍只显示群 ID；不输出附件 XML、CDN 地址或密钥。
        if data.get("room_wxid"):
            logger.info("群文件接收 | 群ID=%s", data["chat_id"])
        for handler in list(self.msg_handlers):
            try:
                handler(self.pid, 11046, data, self)
            except Exception:
                logger.exception("文件消息处理器异常")

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
        if self._voice_receiver is not None:
            self._voice_receiver.close(timeout=30)
            if self._voice_receiver.is_alive:
                raise RuntimeError('语音接收缓存尚未结束，保留桥接等待清理重试')
            self._voice_receiver = None
        if self._friend_requests is not None:
            self._friend_requests.close()
            self._friend_requests = None
        if self._forwarded_receiver is not None:
            self._forwarded_receiver.close(timeout=30)
            if self._forwarded_receiver.is_alive:
                raise RuntimeError('合并转发图片下载尚未结束，保留桥接等待清理重试')
            self._forwarded_receiver = None
        if self._file_receiver is not None:
            self._file_receiver.close(timeout=30)
            if self._file_receiver.is_alive:
                raise RuntimeError("文件原生下载尚未结束，保留桥接等待清理重试")
            self._file_receiver = None
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

    def group_members(self, room):
        from wxbot_framework.group_management import validate_group_id
        from wxbot_framework.group_members import read_group_members
        room = validate_group_id(room)
        with self._calls:
            if not self._owns_bridge or self._stop.is_set() or self._channel is None:
                raise RuntimeError("群成员查询需要已连接的微信账号")
            self._check_account(force=True)
            rows = read_group_members(self._channel.read_memory, self._channel.core_base, room,
                                      expected_account=self.self_wxid)
            self._check_account(force=True)
            return rows

    def contacts(self):
        """只读枚举当前账号缓存；不联网拉取、不保存联系人资料。"""
        from wxbot_framework.contacts import read_contact_directory
        with self._calls:
            if not self._owns_bridge or self._stop.is_set() or self._channel is None:
                raise RuntimeError("联系人查询需要已连接的微信账号")
            self._check_account(force=True)
            account, session = self.self_wxid, self._friend_session_id
            snapshot = read_contact_directory(self._channel.read_memory, self._channel.core_base,
                                              expected_account=account)
            self._check_account(force=True)
            if account != self.self_wxid or session != self._friend_session_id:
                raise RuntimeError("联系人所属的微信连接已结束")
            if snapshot is None:
                raise RuntimeError("微信联系人缓存尚未就绪，请稍后重试")
            return dict(snapshot, account=account, session_id=session,
                        scanned_at=datetime.now().astimezone().isoformat(timespec="seconds"))

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
        if message.type == 37:
            return self._map_friend_request(message)
        if message.type in (10000, 10002):
            return self._map_group_system(message)
        if message.type not in (1, 3, 34, 43, 47, 49, 62):
            return None
        previous = self.self_wxid
        previous_unparsed = self.router.unparsed_groups
        flags = getattr(message, 'reserved', 0)
        if message.type == 1 and flags & 8:
            logger.warning('文本超过接收上限或不可读取，已跳过；会话 ID=%s', message.sender.decode('utf-8', errors='replace'))
            return None
        body = (getattr(message, "image_xml", b"") if message.type != 1 or flags & 4 else message.text).decode("utf-8", errors="replace")
        data = self.router.convert(message.sender.decode("utf-8"), message.receiver.decode("utf-8"),
                                   body, sequence=message.sequence, msg_type=message.type,
                                   thread_id=message.thread_id)
        if self.self_wxid and not previous:
            logger.info("当前账号已识别；好友和群聊文本已就绪")
        if data:
            data['native_message_id'] = str(getattr(message, 'native_message_id', 0) or '')
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
            if message.type == 34:
                data['monitor_only'] = True
                data['raw_content'] = data.get('content') or body
                enabled = not self.image_allowlist or data['chat_id'] in self.image_allowlist
                receiving = enabled and self._voice_receiver is not None
                status = 'pending' if receiving else 'unavailable' if enabled else 'skipped'
                sanitized = dict(data, voice_status=status, voice_path='')
                for field in ('raw_content', 'content', 'msg'):
                    sanitized[field] = ''
                if receiving:
                    # Validation failure can synchronously deliver failed; always
                    # enqueue pending first, and do not emit it again on return.
                    self._deliver_voice(sanitized)
                    self._voice_receiver.submit(data, self._voice_payload(message))
                    return None
                return sanitized
            if message.type in (43, 47, 62):
                data['monitor_only'] = True
                return data
            forwarded = None
            if message.type == 49:
                from wxbot_framework.forwarded import ForwardedError, parse_forwarded
                try:
                    forwarded = parse_forwarded(body)
                except ForwardedError as error:
                    if error.forwarded or '<recorditem' in body:
                        data.update(app_type=19, forwarded_status='failed')
                if forwarded is not None:
                    data.update(forwarded.public())
                    data['forwarded_status'] = 'pending'
                    if self.image_allowlist and data['chat_id'] not in self.image_allowlist:
                        data['forwarded_status'] = 'metadata'
                        for item in data['forwarded_records']:
                            if item.get('image_status') == 'pending':
                                item['image_status'] = 'skipped'
                if getattr(message, 'reserved', 0) & 1:
                    data['metadata_unavailable'] = True
            if message.type in (3, 49):
                monitor = getattr(self, 'monitor_handler', None)
                if monitor is not None:
                    monitor(dict(data, monitor_only=True))
            if message.type == 3 and self._image_receiver is not None:
                if not self.image_allowlist or data["chat_id"] in self.image_allowlist:
                    self._image_receiver.submit(data)
                return None
            if message.type == 49:
                if forwarded is not None:
                    if self._forwarded_receiver is not None and (
                            not self.image_allowlist or data['chat_id'] in self.image_allowlist):
                        self._forwarded_receiver.submit(data, forwarded)
                    return None
                if data.get('app_type') == 19 or data.get('metadata_unavailable'):
                    return None
                if self._file_receiver is not None and (
                        not self.image_allowlist or data["chat_id"] in self.image_allowlist):
                    self._file_receiver.submit(data)
                return None
        elif self.router.unparsed_groups > previous_unparsed:
            # 第一条失败时提示；后续通过 status 查看累计数。
            if self.router.unparsed_groups == 1:
                logger.warning("群文本作者格式未匹配，已跳过；需要用测试群消息核对实际布局")
        return data

    def _map_group_system(self, message):
        # 系统事件不作为聊天正文，不进入模型或普通文本插件。
        sender = message.sender.decode("utf-8")
        receiver = message.receiver.decode("utf-8")
        if getattr(message, 'reserved', 0) & 2:
            # 独立系统解析入口发生在已核对的当前微信进程；这里没有 AddMsg 收发双方。
            from wxbot_framework.recall import parse_recall
            recall = parse_recall(message.image_xml.decode('utf-8', errors='replace'))
            if recall is None or sender != 'weixin' or receiver != 'weixin' or not self.self_wxid:
                return None
            chat = recall['chat_id']
            return {'msg':'', 'content':'', 'wx_type':10002, 'chat_id':chat,
                    'room_wxid':chat if chat.endswith('@chatroom') else '', 'sender_id':'',
                    'native_receiver':self.self_wxid, 'self_wxid':self.self_wxid,
                    'is_self':False, 'sequence':message.sequence, 'recall':recall, 'monitor_only':True}
        if receiver != self.self_wxid and sender != self.self_wxid:
            return None
        chat = receiver if sender == self.self_wxid else sender
        validate_target(chat)
        content = message.image_xml.decode("utf-8", errors="replace")
        from wxbot_framework.recall import parse_recall
        recall = parse_recall(content, chat_id=chat)
        group = chat.endswith('@chatroom')
        if not group and recall is None:
            return None
        self.chats[chat] = {"chat_id": chat, "is_group": group}
        data = {"msg": "", "content": "", "system_content": content, "wx_type": message.type,
                "chat_id": chat, "room_wxid": chat if group else "", "sender_id": "",
                "native_sender": sender, "native_receiver": receiver,
                "self_wxid": self.self_wxid, "is_self": False, "sequence": message.sequence,
                "native_message_id": str(getattr(message, 'native_message_id', 0) or '')}
        if recall is not None:
            data.update(recall=recall, monitor_only=True)
        return data

    def _map_friend_request(self, message):
        from wxbot_framework.friend_requests import FriendRequestError, parse_friend_request
        if self._stop.is_set() or self._friend_requests is None:
            return None
        try:
            receiver = message.receiver.decode("utf-8")
            sender = message.sender.decode("utf-8")
            if receiver != self.self_wxid or sender == self.self_wxid:
                return None
            request = parse_friend_request(message.image_xml.decode("utf-8"))
            # type37 也包含验证结果等通知；仅处理明确的入站申请。
            if request.opcode != 2:
                return None
            public = self._friend_requests.add(request)
        except (UnicodeError, FriendRequestError):
            logger.warning("好友申请元数据无效或不是当前支持的格式，已跳过")
            return None
        return {"msg": "", "content": "", "wx_type": 37, "chat_id": "fmessage",
                "from_wxid": request.sender_id, "sender_id": request.sender_id,
                "native_receiver": receiver, "self_wxid": self.self_wxid,
                "room_wxid": "", "is_self": False, "sequence": message.sequence,
                "friend_request": public, "dispatch_to_plugins": public["status"] == "pending"}

    def friend_requests(self):
        with self._calls:
            if not self._owns_bridge or self._stop.is_set() or self._friend_requests is None:
                raise RuntimeError("好友申请接收服务没有运行")
            self._check_account(force=True)
            return self._friend_requests.list()

    def accept_friend(self, request_id):
        with self._calls:
            if not self._owns_bridge or self._stop.is_set() or self._friend_requests is None:
                raise RuntimeError("好友申请接收服务没有运行")
            self._check_account(force=True)
            catalog = self._friend_requests
            owner = {"account": self.self_wxid, "session_id": self._friend_session_id}
            record = catalog.resolve(request_id, **owner)
            if record.status == "accepted":
                return True
            if record.status != "pending" or record.request.opcode != 2:
                raise ValueError("好友申请已处理或不可通过；请让对方重新发送申请")
            try:
                self.last_result = self._run("--accept-friend", pid=self.pid, keep_loaded=True,
                    preserve_report=self.preserve_reports, account=self.self_wxid,
                    friend_request=record.request)
                self._check_account(force=True)
            except Exception:
                catalog.mark_failed(request_id, **owner)
                raise RuntimeError("通过好友申请失败，请检查连接及申请状态") from None
            accepted = self.last_result.get("success") is True
            if accepted:
                catalog.mark_accepted(request_id, **owner)
            else:
                catalog.mark_failed(request_id, **owner)
            return accepted

    def invite_group_members(self, room, members):
        from wxbot_framework.group_management import validate_group_id, validate_group_members
        room = validate_group_id(room)
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                raise RuntimeError("原生群管理服务没有运行")
            self._check_account(force=True)
            members = validate_group_members(members, self_wxid=self.self_wxid)
            self.last_result = self._run("--manage-group", pid=self.pid, keep_loaded=True,
                preserve_report=self.preserve_reports, account=self.self_wxid,
                target=room, group_operation="invite", group_members=members)
            self._check_account(force=True)
            return self.last_result.get("success") is True

    def create_group(self, members):
        from wxbot_framework.group_management import validate_group_id, validate_group_members
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                raise RuntimeError("原生群管理服务没有运行")
            self._check_account(force=True)
            members = validate_group_members(members, creating=True, self_wxid=self.self_wxid)
            self.last_result = self._run("--manage-group", pid=self.pid, keep_loaded=True,
                preserve_report=self.preserve_reports, account=self.self_wxid,
                group_operation="create", group_members=members)
            self._check_account(force=True)
            if self.last_result.get("success") is not True:
                raise RuntimeError("微信未确认创建群聊，请检查返回状态")
            room = validate_group_id(self.last_result.get("report", {}).get("room_id"))
            self.chats[room] = {"chat_id": room, "is_group": True}
            return room

    def send_text(self, to_wxid, content):
        return self._send_text(to_wxid, content)

    def moments_capabilities(self):
        from moments_transport import moments_capabilities
        from moments_interaction_transport import interaction_capability
        with self._calls:
            if not self._owns_bridge or self._stop.is_set() or self._channel is None:
                return {'supported': False, 'reason': '微信尚未连接'}
            self._check_account(force=True)
            result = moments_capabilities(self.pid, bridge_path=self._dll)
            result['interaction_supported'] = (self.wechat_version == '4.1.15.56' and
                                               result.get('inspect_supported') is True and
                                               interaction_capability(self._dll))
            return result

    def interact_moments(self, payload):
        from moments_interaction_transport import interact_moments
        with self._calls:
            if not self._owns_bridge or self._stop.is_set() or self._channel is None:
                return {'status': 'failed', 'phase': 'preflight'}
            self._check_account(force=True)
            account, session = self.self_wxid, self._friend_session_id
            result = interact_moments(self.pid, account, payload, bridge_path=self._dll)
            try:
                self._check_account(force=True)
                current = account == self.self_wxid and session == self._friend_session_id
            except (OSError, RuntimeError):
                current = False
            return result if current else {'status': 'unknown', 'unload_blocked': result.get('unload_blocked') is True}

    def read_moments(self, limit=20, *, cancelled=lambda: False):
        from wxbot_framework.moments_reader import read_loaded_moments
        with self._calls:
            if not self._owns_bridge or self._stop.is_set() or self._channel is None:
                raise RuntimeError('微信尚未连接')
            self._check_account(force=True)
            return read_loaded_moments(self.pid, self.self_wxid, limit=limit,
                                       cancelled=lambda: self._stop.is_set() or cancelled())

    def publish_moments(self, payload):
        from moments_transport import publish_moments
        with self._calls:
            if not self._owns_bridge or self._stop.is_set() or self._channel is None:
                return {'status': 'failed', 'phase': 'preflight'}
            self._check_account(force=True)
            account, session = self.self_wxid, self._friend_session_id
            result = publish_moments(self.pid, account, content=payload['content'],
                                     images=payload['images'], visibility=payload['visibility'],
                                     bridge_path=self._dll, workspace=self.file_directory.parent,
                                     keep_loaded=True)
            try:
                self._check_account(force=True)
                current = account == self.self_wxid and session == self._friend_session_id
            except (OSError, RuntimeError):
                current = False
            if not current:
                return {'status': 'unknown', 'unload_blocked': result.get('unload_blocked') is True,
                        'retain_images': result.get('retain_images') is True}
            return result

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

    def send_file(self, to_wxid, path):
        from wxbot_framework.files import prepare_file
        validate_target(to_wxid)
        with self._calls:
            if not self._owns_bridge or self._stop.is_set():
                raise RuntimeError("原生文件服务没有运行")
            self._check_account(force=True)
            account = validate_account(self.self_wxid)
            file_root = self.file_directory.resolve()
            account_directory = (file_root / account).resolve()
            if account_directory.parent != file_root:
                raise ValueError("文件账号目录必须位于当前项目的文件目录内")
            with prepare_file(path, account_directory) as file:
                if not self._owns_bridge or self._stop.is_set():
                    raise RuntimeError("微信连接已结束，取消待发送文件")
                self._check_account(force=True)
                if self.self_wxid != account:
                    raise RuntimeError("微信账号已发生变化，取消待发送文件")
                self.last_result = self.last_send_result = self._run(
                    "--dispatch", pid=self.pid, keep_loaded=True,
                    preserve_report=self.preserve_reports, target=to_wxid,
                    file_path=str(file.path), account=account)
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
