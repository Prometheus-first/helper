"""原生收发后端；框架和插件只依赖公开接口。"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

from .sdk import Message

CAPABILITIES = frozenset({"text.receive", "text.send"})
TEXT_CAPABILITIES = CAPABILITIES | frozenset({"friend.text.receive", "friend.text.send",
                                           "group.text.receive", "group.text.send", "group.text.mention",
                                           "image.receive", "image.send",
                                           "file.receive", "file.send",
                                           "friend.file.receive", "friend.file.send",
                                           "group.file.receive", "group.file.send",
                                           "voice.send", "friend.voice.send", "group.voice.send",
                                           "emoticon.send", "emoticon.extract"})


class NativeFileHelperBackend:
    scope = "文件传输助手文本"
    def __init__(self, root, config):
        native = Path(root) / "native"
        if not native.is_dir():
            raise RuntimeError("未找到已验证的原生适配目录")
        # 后端模块按需导入；查看插件和检查配置不需要打开微信。
        if str(native) not in sys.path:
            sys.path.insert(0, str(native))
        from filehelper_service import NativeFileHelperService
        self.raw = NativeFileHelperService(pid=config.get("wechat_pid"), preserve_reports=False)
        self.session_id = uuid.uuid4().hex

    def start(self, emit):
        self.raw.register_msg_handler(lambda _client, protocol, data, _service:
                                      emit(Message.from_native(protocol, data, self.session_id)))
        return self.raw.start()

    @property
    def last_error(self):
        return self.raw.last_error

    @property
    def pid(self):
        return self.raw.pid

    def send_text(self, chat_id, text):
        return self.raw.send_text(chat_id, text)

    def stop(self):
        return self.raw.stop()


class NativeTextBackend(NativeFileHelperBackend):
    scope = "文件传输助手、好友和群聊的文本、图片与文件；语音、表情发送和本地缓存导出"

    def __init__(self, root, config):
        native = Path(root) / "native"
        if not native.is_dir():
            raise RuntimeError("未找到原生适配目录")
        if str(native) not in sys.path:
            sys.path.insert(0, str(native))
        from text_service import NativeTextService
        self.raw = NativeTextService(pid=config.get("wechat_pid"), account=config.get("wechat_account"),
                                     media_directory=Path(root) / config.get("image_directory", ".runtime/images"),
                                     image_allowlist=config.get("chat_allowlist", []),
                                     wechat_data_directory=(Path(root) / config["wechat_data_directory"]
                                                            if config.get("wechat_data_directory") else None),
                                     emoticon_directory=Path(root) / config.get("emoticon_directory", ".runtime/emoticons"),
                                     voice_directory=Path(root) / ".runtime/voices",
                                     file_directory=Path(root) / config.get("file_directory", ".runtime/files"),
                                     preserve_reports=False)
        self.session_id = uuid.uuid4().hex

    def diagnostics(self):
        router = self.raw.router
        return {"account_ready": bool(self.raw.self_wxid),
                "account_source": "current_process",
                "self_wxid": self.raw.self_wxid,
                "bound_account": self.raw.preferred_account,
                "unidentified_messages": router.unidentified,
                "unparsed_groups": router.unparsed_groups,
                "ignored_self_messages": router.ignored_self,
                "mention_messages": self.raw.mention_messages}

    def chats(self):
        return list(self.raw.chats.values())

    def send_at_text(self, room_wxid, content, at_list):
        return self.raw.send_at_text(room_wxid, content, at_list)

    def send_image(self, chat_id, path):
        return self.raw.send_image(chat_id, path)

    def send_voice(self, chat_id, path):
        return self.raw.send_voice(chat_id, path)

    def send_file(self, chat_id, path):
        return self.raw.send_file(chat_id, path)

    def send_emoticon(self, chat_id, path):
        return self.raw.send_emoticon(chat_id, path)

    def export_emoticons(self, *, convert=True):
        return self.raw.export_emoticons(convert=convert)
