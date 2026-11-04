"""Linux 群系统事件与完整名单验证；全部使用合成账号，不连接真实微信。"""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from wxbot_framework.config import ConfigStore
from wxbot_framework.linux_messages import LinuxTextAdapter
from wxbot_framework.linux_panel_backend import LinuxPanelBackend
from wxbot_framework.message_monitor import MessageBuffer
from wxbot_framework.runtime import BotApplication

ROOT = Path(__file__).resolve().parents[1]
BOT, FIRST, NEW = 'wxid_event_bot', 'wxid_event_first', 'wxid_event_new'
ROOM = '70000000001@chatroom'


def notice(kind='join', member=NEW):
    template = '$names$加入了群聊' if kind == 'join' else '你将$names$移出了群聊'
    return ('<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template>'
        f'<template>{template}</template><link_list><link name="names"><memberlist>'
        f'<member><username>{member}</username><nickname>合成新成员</nickname></member>'
        '</memberlist></link></link_list></content_template></sysmsgtemplate></sysmsg>')


class FixtureBackend(LinuxPanelBackend):
    def __init__(self, path):
        super().__init__(path, path, path)
        self.account = BOT
        self.adapter = LinuxTextAdapter(BOT, self.session_id)
        self.rows = [{'wxid':BOT, 'nickname':'机器人'}, {'wxid':FIRST, 'nickname':'原群友'}]

    def start(self, emit):
        self.emit = emit
        return True

    def stop(self):
        return {'capture_stopped':True}

    def group_members(self, room):
        return self.rows

    def chats(self):
        return [{'chat_id':ROOM, 'is_group':True}]


class LinuxGroupEventTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='helper-linux-group-events-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        path = self.root / 'fixture'
        path.touch()
        self.backend = FixtureBackend(path)

    def convert(self, content, kind=10002, sequence=1, **overrides):
        fields = dict(sender=ROOM, receiver=BOT, text=content, type=kind, sequence=sequence)
        fields.update(overrides)
        return self.backend.convert_event(fields)

    def application(self):
        config = self.root / 'bot_config.json'
        config.write_text(json.dumps({'backend':'native_text', 'wechat_account':None,
            'chat_allowlist':[ROOM], 'members_config_file':None, 'plugins':{},
            'text_send_delay':{'enabled':False}}))
        self.app = BotApplication(ROOT, ConfigStore(config), backend_factory=lambda:self.backend)
        self.app.plugins = Mock(capabilities=self.backend.capabilities)
        self.monitor = MessageBuffer()
        self.app.message_observer = self.monitor.append
        self.app._connect()
        self.addCleanup(self.app._disconnect)
        self.assertTrue(self.app._connection_ready)
        self.assertEqual(self.app._group_tracker.account, BOT)
        return self.app

    def poll(self, rows):
        self.backend.rows = rows
        self.app._next_group_poll = 0
        self.app._poll_group_members()

    def test_system_notices_enter_tracker_without_becoming_public_xml(self):
        for kind, body in ((10000, '"合成新成员"加入了群聊'), (10002, notice())):
            message = self.convert(body, kind)
            self.assertFalse(message.data['monitor_only'])
            self.assertFalse(message.data['dispatch_to_plugins'])
            self.assertEqual(message.data['system_content'], body)
            self.assertEqual(message.text, '')
            self.assertNotIn('group_event', message.data)
            monitor = MessageBuffer()
            monitor.append(message)
            self.assertNotIn(body, json.dumps(monitor.tail(0), ensure_ascii=False))
            self.assertEqual(monitor.tail(0)['rows'][0]['content'], '[系统消息]')

    def test_unknown_forged_private_and_mismatched_notices_are_discarded(self):
        for body in (notice() + '\0', '<!DOCTYPE x>' + notice(), '名字加入了群聊',
                '999@chatroom:\n' + notice(), 'x'*16385,
                '<sysmsg type="revokemsg"><revokemsg><session>999@chatroom</session>'
                '<newmsgid>1</newmsgid></revokemsg></sysmsg>'):
            self.assertIsNone(self.convert(body))
        self.assertIsNone(self.convert(notice(), sender=FIRST))
        with self.assertRaises(RuntimeError):
            self.convert(notice(), receiver='wxid_other_account')
        plain = self.convert(FIRST + ':\n' + notice(), 1)
        self.assertNotIn('system_content', plain.data)
        self.assertNotIn('group_event', plain.data)

    def test_structured_join_requires_live_members_and_is_not_duplicated(self):
        app = self.application()
        baseline = self.backend.rows[:]
        self.poll(baseline)
        app.plugins.dispatch.assert_not_called()
        message = self.convert(notice(), sequence=20)
        app._emit(message)
        app.plugins.dispatch.assert_not_called()
        changed = baseline + [{'wxid':NEW, 'nickname':'合成新成员'}]
        self.backend.rows = changed
        app._emit(message)
        event = app.plugins.dispatch.call_args.args[0].data['group_event']
        self.assertEqual((event['kind'], event['member_id']), ('join', NEW))
        app._emit(message)
        self.poll(changed)
        self.poll(changed)
        app.plugins.dispatch.assert_called_once()
        self.assertNotIn('sysmsgtemplate', json.dumps(self.monitor.tail(0)))

    def test_unknown_snapshot_interrupts_leave_confirmation(self):
        app = self.application()
        baseline = self.backend.rows[:]
        self.poll(baseline)
        changed = baseline[:1]
        self.poll(changed)
        self.poll(None)
        self.poll(changed)
        app.plugins.dispatch.assert_not_called()
        self.poll(changed)
        event = app.plugins.dispatch.call_args.args[0].data['group_event']
        self.assertEqual((event['kind'], event['member_id']), ('leave', FIRST))
        for snapshot in (None, [], [{'wxid':FIRST, 'nickname':'原群友'}]):
            self.poll(snapshot)
        app.plugins.dispatch.assert_called_once()

    def test_plain_name_hint_only_fills_a_confirmed_newcomer(self):
        app = self.application()
        baseline = self.backend.rows[:]
        self.poll(baseline)
        app._emit(self.convert('"合成新人名"加入了群聊', 10000))
        app.plugins.dispatch.assert_not_called()
        changed = baseline + [{'wxid':NEW, 'nickname':''}]
        self.poll(changed)
        app.plugins.dispatch.assert_not_called()
        self.poll(changed)
        event = app.plugins.dispatch.call_args.args[0].data['group_event']
        self.assertEqual((event['member_id'], event['nickname']), (NEW, '合成新人名'))

    def test_stale_session_and_restart_do_not_replay_old_system_events(self):
        app = self.application()
        baseline = self.backend.rows[:]
        self.poll(baseline)
        changed = baseline + [{'wxid':NEW, 'nickname':'合成新成员'}]
        self.backend.rows = changed
        app._emit(replace(self.convert(notice()), session_id='old-session'))
        app.plugins.dispatch.assert_not_called()
        app._disconnect()
        self.backend.session_id = 'replacement-session'
        self.backend.adapter = LinuxTextAdapter(BOT, self.backend.session_id)
        app._connect()
        self.poll(changed)
        app._emit(self.convert(notice(), sequence=30))
        app.plugins.dispatch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
