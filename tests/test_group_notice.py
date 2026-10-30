"""群提醒、查询和昵称隔离验证；所有名单来自内存，不发送微信消息。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from plugins.group_notice import Plugin
from wxbot_framework.config import ConfigStore
from wxbot_framework.members import MemberStore
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import Message, PluginContext, ServiceFacade

ROOT = Path(__file__).resolve().parents[1]
ACCOUNT, FIRST, SECOND = "wxid_notice_robot", "wxid_notice_first", "wxid_notice_second"
ROOM, OTHER = "123456789@chatroom", "987654321@chatroom"


class GroupNoticeTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock(session_id="first")
        self.backend.group_members.return_value = (
            {"wxid": ACCOUNT, "nickname": "", "wechat_name": "机器人"},
            {"wxid": FIRST, "nickname": "本群昵称", "wechat_name": "微信名字"})
        self.service = ServiceFacade(text_delay={"enabled": False})
        self.service.bind(self.backend)
        self.addCleanup(self.service.close)

    def context(self, options=None, members=None):
        return PluginContext("group_notice", options or {}, self.service, (), member_store=members)

    def event(self, kind="join", nickname="群友甲", room=ROOM):
        return Message("", room, sender_id=FIRST, type=10002, is_group=True, session_id="first",
                       data={"group_event": {"kind": kind, "room_id": room,
                                             "member_id": FIRST, "nickname": nickname}})

    def test_members_and_nickname_use_same_room_and_session(self):
        ctx = self.context()
        self.assertEqual(ctx.group_member_name(ROOM, FIRST, expected_session="first"), "本群昵称")
        self.backend.group_members.assert_called_with(ROOM)
        self.assertIsNone(ctx.group_member_name(ROOM, SECOND))
        with self.assertRaises(RuntimeError):
            ctx.group_members(ROOM, expected_session="old")
        with self.assertRaises(ValueError):
            ctx.group_members(FIRST)
        self.backend.group_members.return_value = None
        self.assertIsNone(ctx.group_members(ROOM))

    def test_notice_uses_group_name_and_has_no_raw_ids(self):
        ctx = self.context()
        plugin = Plugin()
        plugin.on_load(ctx)
        plugin.on_message(self.event(), ctx)
        self.backend.send_text.assert_called_once_with(ROOM, "欢迎 群友甲 加入群聊！")
        plugin.on_message(self.event("leave"), ctx)
        self.backend.send_text.assert_called_with(ROOM, "群友甲 离开了群聊。")

    def test_ordinary_text_unknown_event_and_other_room_do_not_trigger(self):
        ctx = self.context()
        plugin = Plugin()
        plugin.on_load(ctx)
        plugin.on_message(Message('群友甲加入了群聊', ROOM, type=1, is_group=True), ctx)
        plugin.on_message(Message('', ROOM, type=10002, is_group=True,
            data={'group_event':{'kind':'join','room_id':OTHER,'member_id':FIRST}}), ctx)
        plugin.on_message(Message('', ROOM, type=10002, is_group=True,
            data={'group_event':{'kind':'join','room_id':ROOM,'member_id':''}}), ctx)
        self.backend.send_text.assert_not_called()

    def test_independent_switches_and_custom_templates(self):
        ctx = self.context({"welcome":False, "leave_template":"{nickname} 已离开"})
        plugin = Plugin()
        plugin.on_load(ctx)
        plugin.on_message(self.event(), ctx)
        self.backend.send_text.assert_not_called()
        plugin.on_message(self.event("leave"), ctx)
        self.backend.send_text.assert_called_once_with(ROOM, "群友甲 已离开")

    def test_template_rejects_attributes_format_specs_controls_and_unknown_fields(self):
        for text in ('{nickname.__class__}', '{nickname:10000}', '{unknown}', '{nickname!r}', '{', '\0'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                Plugin().on_load(self.context({"welcome_template":text}))

    def test_reconnected_session_drops_queued_notice(self):
        plugin, ctx = Plugin(), self.context()
        plugin.on_load(ctx)
        self.backend.session_id = "second"
        with self.assertRaises(RuntimeError):
            plugin.on_message(self.event(), ctx)
        self.backend.send_text.assert_not_called()


class GroupNicknameStoreTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.runtime/tests'
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name).resolve()
        self.assertTrue(path.is_relative_to(scratch.resolve()))
        self.path = path / 'members.json'
        self.path.write_text(json.dumps({'database_file':'members.sqlite3',
            'groups':{ROOM:[],OTHER:[]}}), encoding='utf-8')
        self.store = MemberStore(self.path, ACCOUNT)
        self.addCleanup(self.store.close)

    def test_group_nickname_falls_back_to_wechat_and_never_leaks_across_rooms(self):
        store = self.store
        store.remember_wechat_name(FIRST, "微信名字")
        store.remember_group_nickname(FIRST, ROOM, "群甲名字")
        store.remember_group_nickname(FIRST, OTHER, "群乙名字")
        self.assertEqual(store.display_name(FIRST, ROOM), "群甲名字")
        self.assertEqual(store.display_name(FIRST, OTHER), "群乙名字")
        store.remember_group_nickname(FIRST, OTHER, "")
        self.assertEqual(store.display_name(FIRST, OTHER), "微信名字")
        store.register("本群绑定", ROOM, FIRST)
        self.assertEqual(store.display_name(FIRST, ROOM), "本群绑定")
        self.assertEqual(store.group_nickname(FIRST, ROOM), "群甲名字")
        other_account = MemberStore(self.path, SECOND)
        self.addCleanup(other_account.close)
        self.assertIsNone(other_account.display_name(FIRST, ROOM))


class GroupMonitorIntegrationTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.runtime/tests'
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name).resolve()
        self.assertTrue(path.is_relative_to(scratch.resolve()))
        config = {'backend':'native_text', 'wechat_account':ACCOUNT, 'chat_allowlist':[ROOM],
                  'members_config_file':None,'plugins':{},'text_send_delay':{'enabled':False}}
        config_path = path / 'bot_config.json'
        config_path.write_text(json.dumps(config), encoding='utf-8')
        self.backend = Mock(session_id='first',pid=1)
        self.backend.chats.return_value = [{'chat_id':OTHER,'is_group':True}]
        self.app = BotApplication(ROOT, ConfigStore(config_path), backend_factory=lambda:self.backend)
        self.app.plugins = Mock()
        self.app._connect()
        self.assertTrue(self.app._connection_ready)
        self.addCleanup(self.app._disconnect)

    def poll(self, ids):
        self.backend.group_members.return_value = None if ids is None else tuple(
            {'wxid':wxid,'nickname':name,'wechat_name':''} for wxid,name in ids.items())
        self.app._next_group_poll = 0
        self.app._poll_group_members()

    def test_baseline_is_silent_and_new_members_are_confirmed_once(self):
        self.poll({ACCOUNT:'机器人', FIRST:'成员甲'})
        self.app.plugins.dispatch.assert_not_called()
        self.poll({ACCOUNT:'机器人', FIRST:'成员甲', SECOND:'新人'})
        self.app.plugins.dispatch.assert_not_called()
        self.poll({ACCOUNT:'机器人', FIRST:'成员甲', SECOND:'新人'})
        event = self.app.plugins.dispatch.call_args.args[0]
        self.assertEqual(event.data['group_event']['kind'],'join')
        self.assertEqual(event.data['group_event']['nickname'],'新人')
        self.assertEqual(event.type,10002)
        self.assertEqual(event.text,'')
        self.poll({ACCOUNT:'机器人', FIRST:'成员甲', SECOND:'新人'})
        self.app.plugins.dispatch.assert_called_once()
        self.backend.group_members.assert_called_with(ROOM)

    def test_unbound_configuration_uses_the_connected_account(self):
        self.app._disconnect()
        self.app.config['wechat_account'] = None
        self.backend.raw.self_wxid = ACCOUNT
        self.app._connect()
        self.assertIsNotNone(self.app._group_tracker)
        self.assertEqual(self.app._group_tracker.account, ACCOUNT)

    def test_unbound_unavailable_account_does_not_break_connection(self):
        self.app._disconnect()
        self.app.config['wechat_account'] = None
        self.backend.raw.self_wxid = None
        self.app._connect()
        self.assertTrue(self.app._connection_ready)
        self.assertIsNone(self.app._group_tracker)

    def test_failed_or_empty_snapshots_never_report_mass_leaves(self):
        self.poll({ACCOUNT:'机器人', FIRST:'成员甲'})
        self.poll(None)
        self.poll({})
        self.poll({FIRST:'成员甲'})
        self.app.plugins.dispatch.assert_not_called()

    def test_plain_system_text_is_not_dispatched_as_normal_message(self):
        self.poll({ACCOUNT:'机器人', FIRST:'成员甲'})
        self.app._emit(Message('',ROOM,type=10000,is_group=True,session_id='first',
            data={'system_content':'"昵称"加入了群聊'}))
        self.app.plugins.dispatch.assert_not_called()

    def test_structured_system_change_requires_a_matching_live_roster(self):
        self.poll({ACCOUNT:'机器人', FIRST:'成员甲'})
        xml = ('<sysmsg type="sysmsgtemplate"><sysmsgtemplate><content_template>'
            '<template>$names$加入了群聊</template><link_list><link name="names"><memberlist>'
            f'<member><username>{SECOND}</username><nickname>新人</nickname></member>'
            '</memberlist></link></link_list></content_template></sysmsgtemplate></sysmsg>')
        message = Message('',ROOM,type=10002,is_group=True,session_id='first',sequence=22,
                          data={'system_content':xml})
        # 历史系统提示说某人入群，当前完整名单没有该人，不能发送旧欢迎。
        self.app._emit(message)
        self.app.plugins.dispatch.assert_not_called()
        self.backend.group_members.return_value += ({'wxid':SECOND,'nickname':'新人','wechat_name':''},)
        self.app._emit(message)
        self.app.plugins.dispatch.assert_called_once()
        self.poll({ACCOUNT:'机器人', FIRST:'成员甲', SECOND:'新人'})
        self.poll({ACCOUNT:'机器人', FIRST:'成员甲', SECOND:'新人'})
        self.app.plugins.dispatch.assert_called_once()

    def test_scan_notice_supplies_missing_nickname_after_two_roster_confirmations(self):
        self.poll({ACCOUNT:'机器人', FIRST:'成员甲'})
        self.app._emit(Message('',ROOM,type=10000,is_group=True,session_id='first',sequence=42,
            data={'system_content':'"示例群友"通过扫描你分享的二维码加入群聊'}))
        self.app.plugins.dispatch.assert_not_called()
        changed = {ACCOUNT:'机器人', FIRST:'成员甲', SECOND:''}
        self.poll(changed)
        self.app.plugins.dispatch.assert_not_called()
        self.poll(changed)
        self.app.plugins.dispatch.assert_called_once()
        message = self.app.plugins.dispatch.call_args.args[0]
        self.assertEqual(message.data['group_event']['member_id'], SECOND)
        self.assertEqual(message.data['group_event']['nickname'], '示例群友')
        plugin = Plugin()
        ctx = PluginContext('group_notice', {}, self.app.service, ())
        plugin.on_load(ctx)
        plugin.on_message(message, ctx)
        self.backend.send_text.assert_called_once_with(ROOM, '欢迎 示例群友 加入群聊！')

    def test_privacy_notice_supplies_name_only_after_confirmed_membership(self):
        self.poll({ACCOUNT:'机器人', FIRST:'成员甲'})
        self.app._emit(Message('', ROOM, type=10000, is_group=True, session_id='first', sequence=44,
            data={'system_content':'"新同学"与群里其他人都不是朋友关系，请注意隐私安全'}))
        self.app.plugins.dispatch.assert_not_called()
        changed = {ACCOUNT:'机器人', FIRST:'成员甲', SECOND:''}
        self.poll(changed)
        self.app.plugins.dispatch.assert_not_called()
        self.poll(changed)
        message = self.app.plugins.dispatch.call_args.args[0]
        self.assertEqual(message.data['group_event']['member_id'], SECOND)
        self.assertEqual(message.data['group_event']['nickname'], '新同学')

    def test_privacy_notice_does_not_assign_names_to_ambiguous_joins(self):
        self.poll({ACCOUNT:'机器人'})
        self.app._emit(Message('', ROOM, type=10000, is_group=True, session_id='first', sequence=45,
            data={'system_content':'"新同学"与群里其他人都不是朋友关系，请注意隐私安全'}))
        changed = {ACCOUNT:'机器人', FIRST:'', SECOND:''}
        self.poll(changed)
        self.poll(changed)
        events = [call.args[0].data['group_event'] for call in self.app.plugins.dispatch.call_args_list]
        self.assertEqual(len(events), 2)
        self.assertTrue(all(not event['nickname'] for event in events))

    def test_disconnect_resets_baseline(self):
        self.poll({ACCOUNT:'机器人', FIRST:'成员甲'})
        self.app._disconnect()
        self.backend.session_id='second'
        self.app._connect()
        self.poll({ACCOUNT:'机器人', SECOND:'离线新成员'})
        self.app.plugins.dispatch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
