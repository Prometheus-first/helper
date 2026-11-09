"""Linux 管理台后端：收信确认账号，文本业务和媒体类型监控。"""
from collections import OrderedDict
from pathlib import Path
import threading

from .account import validate_account
from .linux_backend import LinuxNativeTextBackend
from .linux_messages import LinuxTextAdapter
from .sdk import Message
from .recall import parse_recall, message_id
from .text import validate_target

PANEL_CAPABILITIES = frozenset({"text.receive", "text.send", "friend.text.receive", "friend.text.send",
    "group.text.receive", "group.text.send", "group.text.mention", "image.send", "file.send", "friend.file.send", "group.file.send",
    "contact.read", "contact.labels.read", "group.members.read", "group.events", "message.recall",
    "image.recall.receive", "file.recall.receive", "voice.recall.receive",
    "image.receive", "friend.image.receive", "group.image.receive",
    "file.receive", "friend.file.receive", "group.file.receive",
    "voice.receive", "friend.voice.receive", "group.voice.receive", "emoticon.extract"})


class LinuxPanelBackend(LinuxNativeTextBackend):
    capabilities = PANEL_CAPABILITIES
    scope = "Linux 文本与媒体收发、群艾特和防撤回"

    def __init__(self, wechat_path, bridge_path, send_bridge_path, *, allowed_targets=(), **kwargs):
        super().__init__(wechat_path, bridge_path, "wxid_linux_pending", test_markers_only=False,
            send_bridge_path=send_bridge_path, send_targets=("filehelper",), **kwargs)
        self.adapter = None
        self.account = ""
        self.allowed = frozenset(validate_target(t) for t in allowed_targets)
        self.capabilities = PANEL_CAPABILITIES
        self.scope = type(self).scope
        self._chats = OrderedDict()
        self._chat_lock = threading.Lock()
        self._group_lock = threading.RLock()
        self._group_reader = None
        self._chats['filehelper'] = {'chat_id': 'filehelper', 'name': '文件传输助手', 'is_group': False}
        for target in sorted(self.allowed):
            self._chats[target] = {'chat_id': target, 'name': '', 'is_group': target.endswith('@chatroom')}

    def launch_environment(self, env):
        for key in ('HELPER_LINUX_TEST_MARKERS', 'HELPER_LINUX_TEST_MEDIA',
                    'HELPER_LINUX_TEST_VOICE', 'HELPER_LINUX_MEDIA_ARM_TEXT'):
            env.pop(key, None)
        env['HELPER_LINUX_PANEL_MEDIA'] = '1'
        return env

    def start(self, emit):
        result = super().start(emit)
        # 老桥接仍能做文字防撤回，但不能把通用消息入口冒充表情发送。
        if self._sender is not None and 'local_emoticons' in self._sender.features:
            self.capabilities = self.capabilities | frozenset({'emoticon.send'})
        if self._sender is not None and 'local_voice' in self._sender.features:
            self.capabilities = self.capabilities | frozenset({'voice.send', 'friend.voice.send', 'group.voice.send'})
        if self._sender is not None and 'local_video' in self._sender.features:
            self.capabilities = self.capabilities | frozenset({'video.send', 'friend.video.send', 'group.video.send'})
        return result

    def convert_event(self, event):
        # Sync 来信入口 receiver 是本账号；不从目录名、登录窗口或旧配置猜账号。
        sender = validate_target(event.get('sender'))
        receiver = validate_target(event.get('receiver'))
        if not self.account:
            if sender == receiver or receiver.endswith('@chatroom'):
                return None
            # 发往文件传输助手的同步消息，其 sender 才是本账号。
            identified = validate_account(sender if receiver == 'filehelper' else receiver)
            adapter = LinuxTextAdapter(identified, self.session_id, include_self_groups=True)
        else:
            if self.account not in (sender, receiver):
                raise RuntimeError('Linux 微信登录账号已变化，请停止后重新连接')
            identified, adapter = self.account, self.adapter
        if event.get('type') == 37:
            # The ticket-bearing XML stays in an internal object until the
            # account-bound lease replaces it with public request metadata.
            from .friend_requests import FriendRequestError, parse_friend_request
            if (not getattr(self, 'identity_ready', False) or receiver != identified or
                    sender == identified or sender.endswith('@chatroom')):
                return None
            try:
                request = parse_friend_request(event['text'])
                if request.opcode != 2:
                    return None
            except (FriendRequestError, ValueError):
                return None
            message = Message(text='', chat_id='fmessage', sender_id=request.sender_id,
                receiver_id=receiver, type=37, sequence=event['sequence'], session_id=self.session_id,
                data={'self_wxid':identified, '_linux_friend_request':request})
        elif event.get('type', 1) == 1:
            message = adapter.convert(event)
        else:
            routed = adapter._router.convert(sender, receiver, event['text'],
                sequence=event['sequence'], msg_type=event['type'])
            if routed is None:
                return None
            # 原始媒体先只供监控与防撤回；完整私有缓存由 SessionLease 提升为普通事件。
            metadata = {'self_wxid': identified, 'monitor_only': True,
                        'dispatch_to_plugins': False, 'dispatch_to_recall_plugins': True}
            native_id = message_id(event.get('native_message_id'))
            if native_id:
                metadata['native_message_id'] = native_id
            if event['type'] in (3, 49):
                from .linux_media_receiver import parse_linux_media_payload
                try:
                    payload = parse_linux_media_payload(event['type'], routed['content'])
                    if payload is not None:
                        metadata['_linux_media'] = payload
                except ValueError:
                    pass
            if event['type'] == 43:
                # 未验证 Linux 下载入口，仅保留不含 CDN 凭据的监控信息。
                from .video_metadata import VideoMetadataError, parse_video_metadata
                metadata['video_status'] = 'unavailable'
                try:
                    video = parse_video_metadata(routed['content'])
                    metadata['video_metadata'] = {'duration': video.duration, 'size': video.raw_length or video.length}
                except VideoMetadataError:
                    pass
            if event['type'] == 47:
                from .sticker_metadata import StickerError, parse_sticker
                try:
                    metadata['sticker_metadata'] = parse_sticker(routed['content'])
                except StickerError:
                    pass
            if event['type'] == 34:
                from .linux_voice_receiver import LinuxVoicePayload
                metadata['_linux_voice'] = LinuxVoicePayload(xml=routed['content'], body=event.get('voice_bytes', b''))
            if event['type'] in (10000, 10002):
                content = routed['content']
                recall = parse_recall(content, chat_id=routed['chat_id'])
                if recall:
                    metadata['recall'] = recall
                else:
                    if not routed['room_wxid']:
                        return None
                    from .group_events import GroupEventError, parse_group_system_message
                    try:
                        parsed = parse_group_system_message(content, msg_type=event['type'],
                            room_id=routed['chat_id'], self_id=identified)
                    except GroupEventError:
                        return None
                    if not parsed:
                        return None
                    # 解析只确认系统消息结构。完整名单、历史同步、会话和去重仍由
                    # BotApplication / GroupMembershipTracker 判断，不能在这里发事件。
                    metadata.update(monitor_only=False, system_content=content)
            message = Message(text='', chat_id=routed['chat_id'], sender_id=routed['sender_id'],
                receiver_id=receiver, type=event['type'], is_group=bool(routed['room_wxid']),
                is_self=routed['is_self'], sequence=event['sequence'], session_id=self.session_id,
                data=metadata)
        if message is not None:
            self.account, self.adapter = identified, adapter
            with self._chat_lock:
                if message.type == 37:
                    return message
                self._chats[message.chat_id] = {'chat_id': message.chat_id, 'name': '', 'is_group': message.is_group}
                self._chats.move_to_end(message.chat_id)
                while len(self._chats) > 1000:
                    self._chats.popitem(last=False)
        return message

    def chats(self):
        with self._chat_lock:
            return list(self._chats.values())

    def contacts(self):
        from .linux_contact_database import read_process_linux_contacts
        if not self.process or self.process.poll() is not None or not self.account:
            raise RuntimeError('微信尚未连接')
        home = getattr(self, 'instance_home', None)
        if home is None:
            raise RuntimeError('当前 Linux 会话尚未配置隔离目录')
        result = read_process_linux_contacts(self.pid, expected_account=self.account,
                                             expected_sha256=self.expected_hash, instance_home=home)
        return dict(result, account=self.account, session_id=self.session_id)

    def contact_labels(self):
        from .linux_contacts import read_process_linux_labels
        if not self.process or self.process.poll() is not None or not self.account:
            raise RuntimeError('微信尚未连接')
        result = read_process_linux_labels(self.pid, expected_account=self.account,
                                           expected_sha256=self.expected_hash)
        return dict(result, account=self.account, session_id=self.session_id)

    def export_emoticons(self, *, convert=True, is_current=None):
        from .linux_emoticons import export_linux_emoticons
        if 'emoticon.extract' not in self.capabilities:
            raise RuntimeError('当前 Linux 会话未开放表情导出')
        process, account, session = self.process, self.account, self.session_id
        home = getattr(self, 'instance_home', None)
        if process is None or process.poll() is not None or not account or home is None:
            raise RuntimeError('请先登录当前 Linux 微信')
        def current():
            return bool(not self._stopping.is_set() and self.process is process and
                process.poll() is None and self.account == account and self.session_id == session and
                (is_current is None or is_current()))
        return export_linux_emoticons(self.pid, expected_account=account,
            expected_sha256=self.expected_hash, instance_home=home,
            output_directory=Path(home).parent / 'emoticons', convert=convert, is_current=current)

    def group_members(self, room):
        from .group_management import validate_group_id
        from .linux_group_members import LinuxGroupReader
        validate_group_id(room)
        if self.allowed and room not in self.allowed:
            raise ValueError('该会话不在收发白名单中')
        with self._group_lock:
            if not self.process or self.process.poll() is not None or not self.account or self._stopping.is_set():
                raise RuntimeError('群成员读取所属的微信连接已结束')
            home = getattr(self, 'instance_home', None)
            if home is None:
                return None
            reader = self._group_reader
            if reader and (reader.pid != self.pid or reader.account != self.account):
                reader.close()
                reader = self._group_reader = None
            if reader is None:
                reader = self._group_reader = LinuxGroupReader(self.pid, expected_account=self.account,
                    expected_sha256=self.expected_hash, instance_home=home)
            result = reader.members(room)
            if self._stopping.is_set() or self.process.poll() is not None:
                raise RuntimeError('群成员读取所属的微信连接已结束')
            return result

    def stop(self):
        result = super().stop()
        with self._group_lock:
            if self._group_reader is not None:
                self._group_reader.close()
                self._group_reader = None
        return result

    def _authorize(self, target):
        validate_target(target)
        if not self.account or self._stopping.is_set() or self._sender is None:
            raise RuntimeError('请先登录，并用另一账号发一条消息以确认当前账号')
        if self.allowed and target not in self.allowed:
            raise ValueError('该会话不在收发白名单中')
        self._sender.allowed_targets = self._sender.allowed_targets | frozenset({target})

    def send_text(self, chat_id, text):
        self._authorize(chat_id)
        return super().send_text(chat_id, text)

    def send_image(self, chat_id, path):
        self._authorize(chat_id)
        return self._sender.send_image(chat_id, path)

    def send_at_text(self, chat_id, text, at_list):
        self._authorize(chat_id)
        return super().send_at_text(chat_id, text, at_list)

    def send_file(self, chat_id, path):
        self._authorize(chat_id)
        return self._sender.send_file(chat_id, path)

    def send_video(self, chat_id, path):
        self._authorize(chat_id)
        if 'video.send' not in self.capabilities:
            raise RuntimeError('当前 Linux 桥接尚未开放视频发送')
        self._sender.bind_account(self.account)
        return self._sender.send_video(chat_id, path)

    def send_emoticon(self, chat_id, path):
        self._authorize(chat_id)
        if 'emoticon.send' not in self.capabilities:
            raise RuntimeError('当前 Linux 桥接尚不支持本地表情发送，请更新并重启微信')
        self._sender.bind_account(self.account)
        return self._sender.send_emoticon(chat_id, path)

    def send_voice(self, chat_id, path):
        self._authorize(chat_id)
        if 'voice.send' not in self.capabilities:
            raise RuntimeError('当前 Linux 桥接不支持语音发送，请更新并重启微信')
        self._sender.bind_account(self.account)
        return self._sender.send_voice(chat_id, path)

    def configure_image_downloads(self, targets):
        sender = self._sender
        if sender is None or 'image_scope_groups' not in sender.features or len(targets) > 64:
            return None
        from .text import SYSTEM_PEERS
        unrestricted = not targets
        targets = sorted(target for target in targets if target != 'filehelper' and
            not target.startswith('gh_') and target not in SYSTEM_PEERS)
        if not targets and not unrestricted:
            return None
        sender.bind_account(self.account)
        if unrestricted:
            return sender.bind_all_image_scopes(self.account)
        return sender.bind_image_scopes(targets, self.account)

    def clear_image_downloads(self, generation):
        if self._sender is not None and generation is not None:
            self._sender.clear_image_scope(generation)

    def request_image(self, message, metadata, generation, is_current):
        sender = self._sender
        if sender is None or generation is None or not is_current():
            return False
        if sender.wait_image_context(metadata.md5, timeout=5, is_current=is_current,
                                     scope_generation=generation) and is_current():
            return sender.download_message_image(metadata.md5, scope_generation=generation)
        return False
