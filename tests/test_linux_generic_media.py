"""完整 Linux 媒体仅交付普通插件一次；合成缓存和模拟发送，不连接微信。"""
import base64
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from wxbot_framework.config import ConfigStore
from wxbot_framework.linux_panel_backend import LinuxPanelBackend, PANEL_CAPABILITIES
from wxbot_framework.linux_sessions import SessionLease
from wxbot_framework.message_monitor import MessageBuffer
from wxbot_framework.runtime import BotApplication

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT, PEER, ROOM = 'wxid_generic_self', 'wxid_generic_peer', '12345678@chatroom'
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aL1sAAAAASUVORK5CYII=')
SILK = b'\x02#!SILK_V3' + b'\x03\0abc' * 50


@unittest.skipUnless(os.name == 'posix', 'Linux 媒体缓存')
class GenericMediaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        database = patch('wxbot_framework.linux_voice_database.LinuxVoiceDatabaseReader')
        self.database = database.start().return_value
        self.database.lookup.side_effect = RuntimeError('fixture database unavailable')
        self.addCleanup(database.stop)
        plugin_root = self.root / 'plugins'
        for name in ('anti_recall', 'anti_recall_image', 'anti_recall_file', 'anti_recall_voice'):
            shutil.copytree(ROOT / 'plugins' / name, plugin_root / name)
        directory = plugin_root / 'media_probe'
        directory.mkdir()
        directory.joinpath('plugin.json').write_text(json.dumps({'api': 'v1', 'message_types': [3, 34, 49],
            'requires': ['image.receive', 'file.receive', 'voice.receive',
                         'friend.file.receive', 'group.file.receive'], 'default_enabled': True}))
        directory.joinpath('__init__.py').write_text('''from pathlib import Path
class Plugin:
    def on_load(self, ctx): self.messages = []
    def on_message(self, message, ctx):
        media = {3: 'image', 49: 'file', 34: 'voice'}[message.type]
        self.messages.append((message, Path(message.data[media + '_path']).read_bytes()))
''')
        config = self.root / 'bot_config.json'
        config.write_text(json.dumps({'backend': 'native_text', 'wechat_account': ACCOUNT,
            'members_config_file': None, 'chat_allowlist': [ROOM, PEER],
            'text_send_delay': {'enabled': False}}))
        executable = self.root / 'wechat'
        executable.write_bytes(b'fixture')
        self.adapter = LinuxPanelBackend(executable, executable, executable)
        self.adapter.instance_home = self.root / 'home'
        self.adapter.last_error = None
        self.adapter.process = SimpleNamespace(pid=1234)
        self.owner = SimpleNamespace(profile={'wxid': ACCOUNT, 'logged_in': True}, backend=self.adapter,
            lock=threading.RLock(), workspace=self.root, active_lease=None, emitter=None, wechat_running=lambda: True)
        def attach(lease, emit): self.owner.active_lease, self.owner.emitter = lease, emit
        def detach(lease):
            if self.owner.active_lease is lease:
                self.owner.active_lease = self.owner.emitter = None
        self.owner.attach, self.owner.detach = attach, detach
        self.app = BotApplication(self.root, ConfigStore(config), initial_capabilities=PANEL_CAPABILITIES | {'voice.send'})
        self.backend = Mock(session_id='fixture')
        self.sent, self.rows = [], []
        self.monitor = MessageBuffer()
        self.app.message_observer = lambda message: self.monitor.append(message, account=ACCOUNT)
        for media in ('image', 'file', 'voice'):
            def capture(chat, path, media=media):
                self.sent.append((media, chat, Path(path).read_bytes()))
                return True
            getattr(self.backend, 'send_' + media).side_effect = capture
        self.app.service.bind(self.backend)
        self.addCleanup(self.app.service.close)
        self.app.plugins.reload()
        self.addCleanup(self.app.plugins.shutdown)
        self.assertTrue(self.app.plugins.is_active('media_probe'))
        self.lease = self.new_lease()
        self.sequence = 0

    @property
    def ordinary(self): return self.app.plugins.records['media_probe'].instance.messages

    def emit(self, message):
        self.rows.append(message)
        self.app._emit(message)

    def new_lease(self):
        lease = SessionLease(self.owner, [ROOM, PEER])
        lease.start(self.emit)
        self.backend.session_id = lease.session_id
        self.addCleanup(lease.stop)
        return lease

    def event(self, kind, identifier, *, chat=ROOM, author=PEER, body=None):
        self.sequence += 1
        if body is None: body = {3: PNG, 49: b'fixture attachment', 34: SILK}[kind]
        if kind == 3:
            xml = f'<msg><img md5="{hashlib.md5(body).hexdigest()}" length="{len(body)}"/></msg>'
            path = self.adapter.instance_home / 'xwechat_files' / ACCOUNT / 'msg/image/fixture.png'
        elif kind == 49:
            xml = (f'<msg><appmsg><type>74</type><title>fixture.txt</title><md5>{hashlib.md5(body).hexdigest()}</md5>'
                f'<appattach><totallen>{len(body)}</totallen></appattach></appmsg></msg>')
            path = self.adapter.instance_home / 'xwechat_files' / ACCOUNT / 'msg/file/2026-01/fixture.txt'
        else:
            xml = f'<msg><voicemsg length="{len(body)}" voicelength="1000" aeskey="private-fixture"/></msg>'
            path = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        text = author + ':\n' + xml if chat.endswith('@chatroom') else xml
        event = dict(sender=chat, receiver=ACCOUNT, type=kind, text=text,
            sequence=self.sequence, native_message_id=str(identifier))
        if kind == 34: event['voice_bytes'] = body
        return self.adapter.convert_event(event)

    def revoke(self, identifier, chat=ROOM):
        self.sequence += 1
        xml = ('<sysmsg type="revokemsg"><revokemsg><session>' + chat + '</session><newmsgid>' +
            str(identifier) + '</newmsgid></revokemsg></sysmsg>')
        return self.adapter.convert_event(dict(sender=chat, receiver=ACCOUNT, type=10002, text=xml,
            sequence=self.sequence, native_message_id=str(100000 + self.sequence)))

    def wait(self, condition):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if condition(): return
            time.sleep(.01)
        self.fail('generic media fixture did not finish')

    def drain(self): self.wait(lambda: not any(r.queue.unfinished_tasks for r in self.app.plugins.records.values()))

    def ready(self, identifier):
        self.wait(lambda: any(row.data.get('native_message_id') == str(identifier) and
            row.data.get({3: 'image', 49: 'file', 34: 'voice'}.get(row.type, '') + '_status') == 'ready' for row in self.rows))
        self.drain()

    def test_group_and_private_full_ready_dispatch_once_and_recall_still_restores_once(self):
        for index, (kind, chat) in enumerate((kind, chat) for kind in (3, 49, 34) for chat in (ROOM, PEER)):
            identifier = 100 + index
            source = self.event(kind, identifier, chat=chat)
            self.lease.receive(self.revoke(identifier, chat))  # 撤回可以先于接收完成。
            self.lease.receive(source)
            self.ready(identifier)
            self.lease.receive(source)
            self.lease.receive(self.revoke(identifier, chat))
            self.drain()
            self.assertEqual(len(self.ordinary), index + 1)
            self.assertEqual(len(self.sent), index + 1)
            message, body = self.ordinary[-1]
            self.assertEqual(message.type, kind)
            self.assertEqual(message.chat_id, chat)
            self.assertEqual(self.sent[-1][1:], (chat, body))
            self.assertFalse(message.data['monitor_only'])
            self.assertTrue(message.data['dispatch_to_plugins'])
            self.assertNotIn('_linux_voice', message.data)
            self.assertNotIn('_linux_media', message.data)
            if kind == 34:
                self.assertIn('<voicemsg ', message.data['voice_xml'])
                self.assertEqual(message.data['voice_xml'], message.data['raw_content'])
            visible = {key: value for key, value in message.data.items()
                       if key not in ('voice_xml', 'raw_content')}
            self.assertNotIn('private-fixture', repr(visible))
        self.backend.send_text.assert_not_called()
        self.assertEqual(self.app.plugins.records['media_probe'].errors, 0)
        monitored = [row for row in self.monitor.tail(0)['rows']
                     if row['type'] in (3, 34, 49) and row['direction'] == 'incoming']
        self.assertEqual(len(monitored), 6)
        self.assertTrue(all(row.get('recalled') for row in monitored))

    def test_pending_failed_unavailable_and_self_or_bot_echo_do_not_reach_ordinary(self):
        for identifier, kind in enumerate((3, 49, 34), 200):
            own = self.event(kind, identifier, author=ACCOUNT)
            self.lease.receive(own)
        self.ready(202)  # own voice 可缓存供防撤回，普通插件仍不能收到。
        self.lease.receive(self.event(34, 203, body=b'#!AMR\n' + b'x' * 30))
        self.wait(lambda: any(row.data.get('voice_status') == 'failed' for row in self.rows))
        echo = self.event(34, 204)
        self.lease.receive(replace(echo, data=dict(echo.data, bot_generated=True)))
        self.drain()
        self.assertEqual(self.ordinary, [])
        self.assertTrue(all(row.data.get('monitor_only') for row in self.rows))
        self.lease.receive(self.revoke(202))
        self.drain()
        self.assertEqual(self.sent, [('voice', ROOM, SILK)])

    def test_ready_callback_waits_until_pending_finishes_without_duplicate_generic_delivery(self):
        pending, resume, complete = threading.Event(), threading.Event(), threading.Event()
        def emit(message):
            if message.data.get('voice_status') == 'pending':
                pending.set()
                resume.wait(3)
            self.emit(message)
        self.lease._emitter = emit
        source = self.event(34, 300)
        delivery = threading.Thread(target=lambda: (self.lease.receive(source), complete.set()), daemon=True)
        delivery.start()
        try:
            self.assertTrue(pending.wait(2))
            self.wait(lambda: list((self.root / 'recall_voice').rglob('*.silk')))
            self.assertFalse(complete.is_set())
            self.assertEqual(self.ordinary, [])
        finally:
            resume.set()
            delivery.join(2)
        self.ready(300)
        self.assertEqual(len(self.ordinary), 1)
        self.assertEqual([row.data['voice_status'] for row in self.rows], ['pending', 'ready'])

    def test_image_and_file_failed_cache_stays_monitor_only(self):
        with patch('wxbot_framework.linux_media_receiver.LinuxMediaReceiver._find',
                   side_effect=ValueError('private-source-path-and-key')):
            for kind, identifier in ((3, 310), (49, 311)):
                self.lease.receive(self.event(kind, identifier))
                media = 'image' if kind == 3 else 'file'
                self.wait(lambda: any(row.data.get('native_message_id') == str(identifier) and
                    row.data.get(media + '_status') == 'failed' for row in self.rows))
        self.drain()
        self.assertEqual(self.ordinary, [])
        self.assertTrue(all(row.data.get('monitor_only') for row in self.rows))
        self.assertNotIn('private-source-path-and-key', repr([dict(row.data) for row in self.rows]))

    def test_unowned_replaced_or_wrong_scope_ready_cannot_be_promoted(self):
        self.lease.receive(self.event(34, 400))
        self.ready(400)
        ready = next(row for row in self.rows if row.data.get('voice_status') == 'ready')
        path = Path(ready.data['voice_path'])
        self.lease._media_ready(replace(ready, data=dict(ready.data, native_message_id='401')))
        self.lease._media_ready(replace(ready, chat_id=PEER))
        path.write_bytes(b'replaced bytes')
        for values in ({'native_message_id': '401'}, {'self_wxid': 'wxid_other'}, {'voice_path': '/etc/passwd'}):
            self.lease._media_ready(replace(ready, data=dict(ready.data, **values)))
        self.lease._media_ready(replace(ready, chat_id='999999@chatroom'))
        self.drain()
        self.assertEqual(len(self.ordinary), 1)

    def test_restart_and_old_native_source_do_not_replay_ready_or_cross_account(self):
        source = self.event(34, 500)
        self.lease.receive(source)
        self.ready(500)
        ready = next(row for row in self.rows if row.data.get('voice_status') == 'ready')
        old = self.lease
        old.stop()
        self.lease = self.new_lease()
        old._media_ready(ready)
        self.lease.receive(replace(source, session_id='old-native'))
        self.owner.profile['wxid'] = 'wxid_other'
        self.lease.receive(self.event(34, 501))
        self.owner.profile['wxid'] = ACCOUNT
        self.drain()
        self.assertEqual(len(self.ordinary), 1)
        self.assertFalse(Path(ready.data['voice_path']).exists())

    def test_capabilities_expose_generic_and_specific_scopes_but_not_unrelated_features(self):
        for media in ('image', 'file', 'voice'):
            for prefix in ('', 'friend.', 'group.'):
                self.assertIn(prefix + media + '.receive', PANEL_CAPABILITIES)
        self.assertNotIn('forwarded.receive', PANEL_CAPABILITIES)


if __name__ == '__main__': unittest.main()
