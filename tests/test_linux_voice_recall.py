"""Linux 事件、连接接收器和现有防撤回插件闭环；只用模拟发送端。"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from wxbot_framework.config import ConfigStore
from wxbot_framework.linux_panel_backend import LinuxPanelBackend, PANEL_CAPABILITIES
from wxbot_framework.linux_sessions import SessionLease
from wxbot_framework.runtime import BotApplication

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT, MEMBER, ROOM = 'wxid_voice_self', 'wxid_voice_friend', '12345678@chatroom'
SILK = b'\x02#!SILK_V3' + b'\x03\0abc' * 50  # 封装 fixture，不是可播放录音。
XML = f'<msg><voicemsg length="{len(SILK)}" voicelength="1000" endflag="1" aeskey="private-key-fixture"/></msg>'


class LinuxVoiceRecallTests(unittest.TestCase):
    def setUp(self):
        database = patch('wxbot_framework.linux_voice_database.LinuxVoiceDatabaseReader')
        self.database = database.start().return_value
        self.database.lookup.side_effect = RuntimeError('fixture database unavailable')
        self.addCleanup(database.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        executable = self.directory / 'wechat'
        executable.write_bytes(b'fixture')
        self.adapter = LinuxPanelBackend(executable, executable, executable)
        self.adapter.instance_home = self.directory / 'home'
        self.adapter.last_error = None
        self.owner = SimpleNamespace(profile={'wxid': ACCOUNT, 'logged_in': True},
            backend=self.adapter, lock=threading.RLock(), workspace=self.directory,
            active_lease=None, emitter=None, wechat_running=lambda: True)
        def attach(lease, emit):
            self.owner.active_lease, self.owner.emitter = lease, emit
        def detach(lease):
            if self.owner.active_lease is lease:
                self.owner.active_lease = self.owner.emitter = None
        self.owner.attach, self.owner.detach = attach, detach
        self.capabilities = PANEL_CAPABILITIES | {'voice.send', 'voice.recall.receive'}
        options = {p.parent.name: {'enabled': p.parent.name in ('anti_recall', 'anti_recall_voice')}
                   for p in (ROOT / 'plugins').glob('*/plugin.json') if not p.parent.name.startswith('_')}
        config = self.directory / 'bot_config.json'
        config.write_text(json.dumps({'backend': 'native_text', 'wechat_account': ACCOUNT,
            'members_config_file': None, 'chat_allowlist': [ROOM, MEMBER],
            'text_send_delay': {'enabled': False}, 'plugins': options}))
        self.app = BotApplication(ROOT, ConfigStore(config), initial_capabilities=self.capabilities)
        self.backend = Mock(session_id='fixture')
        self.sent, self.rows = [], []
        def send(chat, path):
            self.sent.append((chat, Path(path).read_bytes()))
            return True
        self.backend.send_voice.side_effect = send
        self.app.service.bind(self.backend)
        self.addCleanup(self.app.service.close)
        self.app.plugins.reload()
        self.addCleanup(self.app.plugins.shutdown)
        self.lease = self.new_lease()
        self.sequence = 0

    def emit(self, message):
        self.rows.append(message)
        self.app._emit(message)

    def new_lease(self):
        lease = SessionLease(self.owner, [ROOM, MEMBER])
        lease.start(self.emit)
        self.backend.session_id = lease.session_id
        self.addCleanup(lease.stop)
        return lease

    def event(self, *, identifier='81', chat=ROOM, body=SILK, xml=XML, author=MEMBER):
        self.sequence += 1
        content = author + ':\n' + xml if chat.endswith('@chatroom') else xml
        return self.adapter.convert_event(dict(sender=chat, receiver=ACCOUNT,
            text=content, type=34, sequence=self.sequence, native_message_id=identifier, voice_bytes=body))

    def revoke(self, *, identifier='81', chat=ROOM):
        self.sequence += 1
        text = ('<sysmsg type="revokemsg"><revokemsg><session>' + chat + '</session><newmsgid>' +
                identifier + '</newmsgid></revokemsg></sysmsg>')
        return self.adapter.convert_event(dict(sender=chat, receiver=ACCOUNT, text=text,
            type=10002, sequence=self.sequence, native_message_id=str(1000 + self.sequence)))

    def wait(self, predicate):
        end = time.monotonic() + 3
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.01)
        self.fail('Linux voice fixture did not finish')

    def drain(self):
        self.wait(lambda: not any(record.queue.unfinished_tasks for record in self.app.plugins.records.values()))

    def test_adapter_lease_cache_and_plugin_restore_group_and_private_once(self):
        self.assertTrue(self.app.plugins.is_active('anti_recall_voice'))
        for identifier, chat, author in (('81', ROOM, MEMBER), ('82', MEMBER, MEMBER), ('83', ROOM, ACCOUNT)):
            source = self.event(identifier=identifier, chat=chat, author=author)
            self.assertNotIn('private-key-fixture', repr(source))
            self.lease.receive(source)
            self.wait(lambda: any(row.data.get('native_message_id') == identifier and
                                  row.data.get('voice_status') == 'ready' for row in self.rows))
            self.lease.receive(self.revoke(identifier=identifier, chat=chat))
            self.lease.receive(self.revoke(identifier=identifier, chat=chat))
            self.drain()
        self.assertEqual(self.sent, [(ROOM, SILK), (MEMBER, SILK), (ROOM, SILK)])
        self.backend.send_text.assert_not_called()
        self.assertTrue(all('_linux_voice' not in row.data for row in self.rows))
        voice_rows = [row for row in self.rows if row.type == 34]
        self.assertTrue(all(row.data['media_cache_only'] for row in voice_rows))
        self.assertEqual([row.data['voice_status'] for row in voice_rows], ['pending', 'ready'] * 3)

    def test_voice_xml_reaches_normal_plugins_once_after_success_or_cache_failure(self):
        original_dispatch = self.app.plugins.dispatch
        self.app.plugins.dispatch = Mock(wraps=original_dispatch)
        for identifier, body in (('91', SILK), ('92', b'#!AMR\\n' + b'x' * 30)):
            self.lease.receive(self.event(identifier=identifier, body=body))
            self.wait(lambda: any(row.data.get('native_message_id') == identifier and
                row.data.get('voice_status') in ('ready', 'failed') for row in self.rows))
            self.lease.receive(self.event(identifier=identifier, body=body))
        deliveries = [call.args[0] for call in self.app.plugins.dispatch.call_args_list
                      if not call.kwargs.get('only') and call.args[0].type == 34]
        self.assertEqual(len(deliveries), 2)
        self.assertEqual({row.data['voice_status'] for row in deliveries}, {'ready', 'failed'})
        self.assertTrue(all(row.data['voice_xml'] == XML and row.data['raw_content'] == XML
                            for row in deliveries))

    def test_recall_before_original_waits_for_ready_and_never_sends_twice(self):
        self.lease.receive(self.revoke())
        self.drain()
        self.assertEqual(self.sent, [])
        self.lease.receive(self.event())
        self.wait(lambda: any(row.data.get('voice_status') == 'ready' for row in self.rows))
        self.drain()
        self.lease.receive(self.revoke())
        self.lease.receive(self.event())
        self.drain()
        self.assertEqual(self.sent, [(ROOM, SILK)])

    def test_database_only_voice_restores_once_through_existing_plugin(self):
        self.database.lookup.side_effect = None
        self.database.lookup.return_value = SILK
        self.lease.receive(self.event(body=b''))
        self.wait(lambda: any(row.data.get('voice_status') == 'ready' for row in self.rows))
        self.database.lookup.assert_called_once_with(ROOM, '81')
        self.lease.receive(self.revoke())
        self.lease.receive(self.revoke())
        self.drain()
        self.assertEqual(self.sent, [(ROOM, SILK)])

    def test_absent_or_unsupported_audio_does_not_restore_or_leak_xml(self):
        self.lease.receive(self.event(body=b''))
        self.lease.receive(self.revoke())
        self.lease.receive(self.event(identifier='82', body=b'#!AMR\n' + b'x' * 30))
        self.wait(lambda: any(row.data.get('voice_status') == 'failed' for row in self.rows))
        self.lease.receive(self.revoke(identifier='82'))
        self.drain()
        self.assertEqual(self.sent, [])
        self.backend.send_text.assert_not_called()
        self.assertNotIn('private-key-fixture', repr(self.rows))
        self.assertNotIn('voice_bytes', repr(self.rows))
        self.assertEqual(self.rows[0].data['voice_status'], 'pending')

    def test_stopped_lease_and_old_native_event_cannot_cross_restart(self):
        self.lease.receive(self.event())
        self.wait(lambda: any(row.data.get('voice_status') == 'ready' for row in self.rows))
        old = self.lease
        ready = next(row for row in self.rows if row.data.get('voice_status') == 'ready')
        old.stop()
        self.lease = self.new_lease()
        count = len(self.rows)
        old._media_ready(ready)
        self.lease.receive(replace(self.event(identifier='83'), session_id='stale-native'))
        self.assertEqual(len(self.rows), count)
        self.lease.receive(self.revoke())
        self.drain()
        self.assertEqual(self.sent, [])
        self.assertFalse(Path(ready.data['voice_path']).exists())
        self.lease.receive(self.event(identifier='82'))
        self.wait(lambda: any(row.data.get('native_message_id') == '82' and
                              row.data.get('voice_status') == 'ready' for row in self.rows))
        self.lease.receive(self.revoke(identifier='82'))
        self.drain()
        self.assertEqual(self.sent, [(ROOM, SILK)])

    def test_outside_exact_allowlist_never_creates_voice_cache_or_restores(self):
        self.lease.receive(self.event(chat='999999@chatroom'))
        self.lease.receive(self.revoke(chat='999999@chatroom'))
        self.drain()
        self.assertIsNone(self.lease._voice_receiver)
        self.assertEqual(self.sent, [])
        self.assertNotIn('_linux_voice', repr(self.rows))


if __name__ == '__main__':
    unittest.main()
