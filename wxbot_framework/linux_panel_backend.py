"""Linux 管理台后端：收信确认账号，文本业务和媒体类型监控。"""
from collections import OrderedDict
import threading

from .account import validate_account
from .linux_backend import LinuxNativeTextBackend
from .linux_messages import LinuxTextAdapter
from .sdk import Message
from .text import validate_target

PANEL_CAPABILITIES = frozenset({"text.receive", "text.send", "friend.text.receive", "friend.text.send",
    "group.text.receive", "group.text.send", "image.send", "file.send", "friend.file.send", "group.file.send"})


class LinuxPanelBackend(LinuxNativeTextBackend):
    capabilities = PANEL_CAPABILITIES
    scope = "Linux 文本收发、图片和文件发送；媒体仅显示类型"

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
        self._chats['filehelper'] = {'chat_id': 'filehelper', 'name': '文件传输助手', 'is_group': False}
        for target in sorted(self.allowed):
            self._chats[target] = {'chat_id': target, 'name': '', 'is_group': target.endswith('@chatroom')}

    def launch_environment(self, env):
        for key in ('HELPER_LINUX_TEST_MARKERS', 'HELPER_LINUX_TEST_MEDIA',
                    'HELPER_LINUX_TEST_VOICE', 'HELPER_LINUX_MEDIA_ARM_TEXT'):
            env.pop(key, None)
        env['HELPER_LINUX_PANEL_MEDIA'] = '1'
        return env

    def convert_event(self, event):
        # Sync 来信入口 receiver 是本账号；不从目录名、登录窗口或旧配置猜账号。
        sender = validate_target(event.get('sender'))
        receiver = validate_target(event.get('receiver'))
        if not self.account:
            if sender == receiver or receiver.endswith('@chatroom'):
                return None
            # 发往文件传输助手的同步消息，其 sender 才是本账号。
            identified = validate_account(sender if receiver == 'filehelper' else receiver)
            adapter = LinuxTextAdapter(identified, self.session_id)
        else:
            if self.account not in (sender, receiver):
                raise RuntimeError('Linux 微信登录账号已变化，请停止后重新连接')
            identified, adapter = self.account, self.adapter
        if event.get('type', 1) == 1:
            message = adapter.convert(event)
        else:
            routed = adapter._router.convert(sender, receiver, event['text'],
                sequence=event['sequence'], msg_type=event['type'])
            if routed is None:
                return None
            # 丢弃原始 XML 和 CDN 凭据；不把媒体内容交给文字业务插件。
            message = Message(text='', chat_id=routed['chat_id'], sender_id=routed['sender_id'],
                receiver_id=receiver, type=event['type'], is_group=bool(routed['room_wxid']),
                is_self=routed['is_self'], sequence=event['sequence'], session_id=self.session_id,
                data={'self_wxid': identified, 'monitor_only': True, 'dispatch_to_plugins': False})
        if message is not None:
            self.account, self.adapter = identified, adapter
            with self._chat_lock:
                self._chats[message.chat_id] = {'chat_id': message.chat_id, 'name': '', 'is_group': message.is_group}
                self._chats.move_to_end(message.chat_id)
                while len(self._chats) > 1000:
                    self._chats.popitem(last=False)
        return message

    def chats(self):
        with self._chat_lock:
            return list(self._chats.values())

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

    def send_file(self, chat_id, path):
        self._authorize(chat_id)
        return self._sender.send_file(chat_id, path)
