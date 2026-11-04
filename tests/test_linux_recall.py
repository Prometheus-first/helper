"""Linux 协议事件到现有防撤回插件的离线闭环，不向微信发消息。"""
import json
import base64
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from wxbot_framework.config import ConfigStore
from wxbot_framework.linux_panel_backend import LinuxPanelBackend, PANEL_CAPABILITIES
from wxbot_framework.runtime import BotApplication

ROOT = Path(__file__).resolve().parents[1]
ROOM = '12345678@chatroom'
SELF = 'wxid_linux_recall_self'
MEMBER = 'wxid_linux_recall_member'


class LinuxRecallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        self.directory = directory
        executable = directory / 'wechat'
        executable.write_bytes(b'fixture')
        self.adapter = LinuxPanelBackend(executable, executable, executable)
        options = {p.parent.name: {'enabled': p.parent.name == 'anti_recall'}
                   for p in (ROOT / 'plugins').glob('*/plugin.json')
                   if not p.parent.name.startswith('_')}
        config = directory / 'bot_config.json'
        config.write_text(json.dumps({'backend': 'native_text', 'wechat_account': SELF,
            'members_config_file': None, 'chat_allowlist': [ROOM, MEMBER],
            'text_send_delay': {'enabled': False}, 'plugins': options}))
        self.app = BotApplication(ROOT, ConfigStore(config), initial_capabilities=PANEL_CAPABILITIES)
        self.backend = Mock(session_id=self.adapter.session_id)
        self.backend.send_text.return_value = True
        self.app.service.bind(self.backend)
        self.addCleanup(self.app.service.close)
        self.app.plugins.reload()
        self.addCleanup(self.app.plugins.shutdown)
        self.sequence = 0

    def event(self, content, *, kind=1, chat=ROOM, identifier='18446744073709551600', author=MEMBER):
        self.sequence += 1
        text = author + ':\n' + content if chat.endswith('@chatroom') and kind not in (10000, 10002) else content
        return self.adapter.convert_event(dict(sender=chat, receiver=SELF, text=text,
            type=kind, sequence=self.sequence, native_message_id=identifier))

    def revoke(self, *, chat=ROOM, identifier='18446744073709551600', kind=10002):
        content = ('<sysmsg type="revokemsg"><revokemsg><session>' + chat + '</session>'
                   '<newmsgid>' + identifier + '</newmsgid></revokemsg></sysmsg>')
        return self.event(content, kind=kind, chat=chat, identifier='2')

    def drain(self):
        deadline = time.monotonic() + 3
        while any(record.queue.unfinished_tasks for record in self.app.plugins.records.values()):
            if time.monotonic() > deadline:
                self.fail('防撤回插件队列未完成')
            time.sleep(.01)

    def test_linux_capability_loads_plugin_and_restores_group_unicode_once(self):
        self.assertTrue(self.app.plugins.is_active('anti_recall'))
        text = 'Linux 原文🐱\n第二行 [微笑]'
        self.app._emit(self.event(text))
        self.app._emit(self.revoke())
        self.app._emit(self.revoke(kind=10000))
        self.drain()
        self.backend.send_text.assert_called_once_with(ROOM, '撤回的消息是：' + text)

    def test_private_recall_before_original_and_wrong_room(self):
        self.app._emit(self.revoke(chat=MEMBER))
        self.app._emit(self.event('私聊原文', chat=MEMBER))
        self.app._emit(self.revoke(chat='9999@chatroom'))
        self.drain()
        self.backend.send_text.assert_called_once_with(MEMBER, '撤回的消息是：私聊原文')

    def test_own_group_message_only_caches_until_recall(self):
        source = self.event('本人发出的原文', author=SELF)
        self.assertTrue(source.is_self)
        self.assertFalse(source.data['dispatch_to_plugins'])
        self.app._emit(source)
        self.drain()
        self.backend.send_text.assert_not_called()
        self.app._emit(self.revoke())
        self.drain()
        self.backend.send_text.assert_called_once_with(ROOM, '撤回的消息是：本人发出的原文')

    def test_media_label_never_reposts_xml_credentials(self):
        self.app._emit(self.event('<msg aeskey="PRIVATE_FIXTURE"/>', kind=47))
        self.app._emit(self.revoke())
        self.drain()
        self.backend.send_text.assert_called_once_with(ROOM, '撤回的消息是：[表情]')

    def test_allowlist_and_disabled_plugin_do_not_restore(self):
        self.app._emit(self.event('范围外', chat='9999@chatroom'))
        self.app._emit(self.revoke(chat='9999@chatroom'))
        self.drain()
        self.app.plugins.disable('anti_recall')
        self.app._emit(self.event('已关闭'))
        self.app._emit(self.revoke())
        self.backend.send_text.assert_not_called()

    def test_reconnect_does_not_restore_old_session(self):
        self.app._emit(self.event('旧连接'))
        self.drain()
        self.backend.session_id = 'new-session'
        self.app._emit(self.revoke())
        self.drain()
        self.backend.send_text.assert_not_called()

    def test_cached_image_and_file_survive_source_removal_and_restore_once(self):
        from wxbot_framework.linux_media_receiver import LinuxMediaReceiver
        for plugin in ('anti_recall_image', 'anti_recall_file'):
            self.app.plugins.enable(plugin)
            self.assertTrue(self.app.plugins.is_active(plugin))
        storage = self.directory / 'xwechat_files'
        profile = storage / SELF / 'msg'
        observed, sent = [], []
        def ready(message):
            self.app._emit(message)
            observed.append(message)
        def capture(kind):
            def send(chat, path):
                sent.append((kind, chat, Path(path).name, Path(path).read_bytes()))
                return True
            return send
        self.backend.send_image.side_effect = capture('image')
        self.backend.send_file.side_effect = capture('file')
        receiver = LinuxMediaReceiver(account=SELF, session_id=self.adapter.session_id,
            pid=1234, storage_root=storage, cache_root=self.directory / 'cache',
            is_current=lambda: True, callback=ready, timeout=2)
        self.addCleanup(receiver.close)
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII=')
        cases = ((3, 'image', 'image.png', png), (49, 'file', 'test.txt', b'cached original file'))
        for kind, label, name, body in cases:
            with self.subTest(kind=kind):
                path = profile / label / '2026-01' / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(body)
                md5 = hashlib.md5(body).hexdigest()
                xml = (f'<msg><img md5="{md5}" length="{len(body)}"/></msg>' if kind == 3 else
                    f'<msg><appmsg><type>6</type><title>{name}</title><md5>{md5}</md5>'
                    f'<appattach><totallen>{len(body)}</totallen></appattach></appmsg></msg>')
                pending = receiver.submit(self.event(xml, kind=kind, identifier=str(kind)))
                self.assertEqual(pending.data[label + '_status'], 'pending')
                self.app._emit(pending)
                deadline = time.monotonic() + 3
                while not any(row.type == kind for row in observed) and time.monotonic() < deadline:
                    time.sleep(.01)
                result = next(row for row in observed if row.type == kind)
                self.assertEqual(result.data[label + '_status'], 'ready')
                path.unlink()
                self.app._emit(self.revoke(identifier=str(kind)))
                self.app._emit(self.revoke(identifier=str(kind)))
                self.drain()
                self.assertEqual(sum(row[0] == label for row in sent), 1)
                self.assertEqual(sent[-1][1], ROOM)
                self.assertEqual(sent[-1][3], body)
                if kind == 49:
                    self.assertEqual(sent[-1][2], name)
        self.backend.send_text.assert_not_called()


if __name__ == '__main__':
    unittest.main()
