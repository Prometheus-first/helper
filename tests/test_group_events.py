"""群系统消息、成员名字和稳定快照差分；使用虚构 ID，不连接微信。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, asdict
import unittest
from xml.sax.saxutils import escape

from wxbot_framework.group_events import (
    GroupEventError, GroupMemberEvent, GroupMembershipTracker, MAX_GROUP_SYSTEM_BYTES,
    parse_group_system_message,
)

ROOM = "70000000001@chatroom"
OTHER_ROOM = "70000000002@chatroom"
BOT = "wxid_group_event_robot"
ALICE = "wxid_group_event_alice"
BOB = "wxid_group_event_bob"
NEW = "wxid_group_event_new"
SESSION = "group-event-session"


def system(template, **links):
    body = []
    for role, rows in links.items():
        members = "".join(f"<member><username>{escape(identifier)}</username>"
                          f"<nickname>{escape(nickname)}</nickname></member>"
                          for identifier, nickname in rows)
        body.append(f'<link name="{role}" type="link_profile"><memberlist>{members}'
                    '</memberlist></link>')
    return ('<sysmsg type="sysmsgtemplate"><sysmsgtemplate>'
            '<content_template type="tmpl_type_profile"><plain />'
            f'<template>{escape(template)}</template><link_list>{"".join(body)}</link_list>'
            '</content_template></sysmsgtemplate></sysmsg>')


def join(identifier=NEW, nickname="新群友"):
    return system('"$username$"加入了群聊', username=[(identifier, nickname)])


def leave(identifier=ALICE, nickname="通知中的微信名"):
    return system('你将"$kickoutname$"移出了群聊', kickoutname=[(identifier, nickname)])


class ParserTests(unittest.TestCase):
    def parse(self, content, msg_type=10002, **options):
        return parse_group_system_message(content, msg_type=msg_type, room_id=ROOM, **options)

    def test_structured_join_and_immutable_public_shape(self):
        event, = self.parse(join())
        self.assertEqual(asdict(event), {"kind": "join", "room_id": ROOM, "member_id": NEW,
                                        "nickname": "新群友", "source": "system_structured"})
        with self.assertRaises(FrozenInstanceError):
            event.member_id = ALICE
        self.assertFalse(hasattr(event, "__dict__"))

    def test_invitation_selects_invitees_not_inviter_and_deduplicates_members(self):
        content = system('「$username$」邀请「$names$」加入了群聊',
                         username=[(ALICE, "邀请者")],
                         names=[(BOB, "鲍勃"), (NEW, "新朋友"), (BOB, "重复名字")])
        events = self.parse(content)
        self.assertEqual([(e.member_id, e.nickname) for e in events], [(BOB, "鲍勃"), (NEW, "新朋友")])

    def test_scan_join_selects_person_not_qrcode_owner(self):
        content = system('「$username$」通过扫描「$qrcode$」分享的二维码加入群聊',
                         username=[(NEW, "扫码人")], qrcode=[(ALICE, "二维码分享者")])
        self.assertEqual([e.member_id for e in self.parse(content)], [NEW])
        for template in ('"$username$"通过扫描二维码加入了群聊',
                         '"$username$"通过群聊二维码加入群聊'):
            self.assertEqual(self.parse(system(template, username=[(NEW, "扫码人")]))[0].kind, "join")

    def test_leave_and_kickout_targets(self):
        for template, role in (('"$username$"退出了群聊', "username"),
                               ('"$username$"被群主移出群聊', "username"),
                               ('你将"$kickoutname$"移出了群聊', "kickoutname"),
                               ('"$admin$"将"$username$"移出了群聊', "username")):
            content = system(template, **{role: [(ALICE, "小艾")], "admin": [(BOB, "管理员")]})
            with self.subTest(template=template):
                event, = self.parse(content)
                self.assertEqual((event.kind, event.member_id), ("leave", ALICE))

    def test_bot_and_unidentified_members_are_handled_without_guessing_ids(self):
        content = system('$username$加入了群聊', username=[(BOT, "机器人"), (NEW, "新人")])
        self.assertEqual([e.member_id for e in self.parse(content, self_id=BOT)], [NEW])
        unknown, = self.parse(system('$username$加入了群聊', username=[("", "只有名字")]))
        self.assertEqual((unknown.member_id, unknown.nickname), ("", "只有名字"))
        # 即使昵称恰好长得像微信 ID，也没有结构化 username 证据。
        unknown, = self.parse(system('$username$加入了群聊', username=[("", NEW)]))
        self.assertEqual((unknown.member_id, unknown.nickname), ("", NEW))

    def test_plain_system_templates_return_name_only_events(self):
        cases = (('“邀请人”邀请“被邀请者”加入了群聊', "join", "被邀请者"),
                 ('"小艾"通过扫描"分享者"分享的二维码加入群聊', "join", "小艾"),
                 ('"示例群友"通过扫描你分享的二维码加入群聊', "join", "示例群友"),
                 ('"新同学"与群里其他人都不是朋友关系，请注意隐私安全', "join", "新同学"),
                 ('「新成员」加入了群聊', "join", "新成员"),
                 ('"小艾"退出了群聊', "leave", "小艾"),
                 ('你将"小艾"移出了群聊', "leave", "小艾"),
                 ('"小艾"被群主移出群聊', "leave", "小艾"))
        for text, kind, nickname in cases:
            with self.subTest(text=text):
                event, = self.parse(text, msg_type=10000)
                self.assertEqual((event.kind, event.member_id, event.nickname, event.source),
                                 (kind, "", nickname, "system_text"))

    def test_plain_chat_and_unrelated_or_ambiguous_text_are_not_events(self):
        for msg_type in (1, 3, 49, 37, True, "10002"):
            with self.subTest(msg_type=msg_type):
                self.assertEqual(self.parse(join(), msg_type=msg_type), ())
                self.assertEqual(self.parse('"小艾"加入了群聊', msg_type=msg_type), ())
        for text in ("小艾加入了群聊", "有人说：小艾退出了群聊", '我说"小艾"加入了群聊',
                     '"小艾"加入了群聊，大家鼓掌', '$quoted_999$加入了群聊',
                     '有人说"小艾"与群里其他人都不是朋友关系，请注意隐私安全',
                     '"小艾"修改了群名', "你加入了群聊", '你被"群主"移出群聊'):
            with self.subTest(text=text):
                self.assertEqual(self.parse(text, msg_type=10000), ())
        self.assertEqual(self.parse(system('"$username$"修改群名为"$remark$"',
                                          username=[(ALICE, "小艾")], remark=[("", "新群名")])), ())

    def test_matching_room_prefix_and_cdata_are_supported(self):
        content = join().replace('<template>"$username$"加入了群聊</template>',
                                 '<template><![CDATA["$username$"加入了群聊]]></template>')
        for prefix in ("", ROOM + ":\n", ROOM + ":\r\n"):
            with self.subTest(prefix=prefix):
                self.assertEqual(self.parse(prefix + content)[0].member_id, NEW)
        with self.assertRaises(GroupEventError):
            self.parse(OTHER_ROOM + ":\n" + content)

    def test_invalid_xml_and_fields_do_not_echo_content(self):
        invalid = (None, "", " ", join() + "\0", join() + "\ud800", "<sysmsg>",
                   '<!DOCTYPE sysmsg [<!ENTITY private "private_value">]>' + join(),
                   '<!DOCTYPE sysmsg SYSTEM "file:///private/path">' + join(),
                   join().replace("新群友", "&unknown;"),
                   join().replace("<username>", "<username><x /></username><username>", 1),
                   join().replace("</link_list>", join().split("<link_list>")[1].split("</link_list>")[0] + "</link_list>"),
                   system('$username$加入群聊', username=[("700@chatroom", "群")]),
                   system('$username$加入群聊', username=[("v3_not_a_member", "名字")]),
                   system('$username$加入群聊', username=[("微信中文", "名字")]),
                   system('$username$加入群聊', username=[(NEW, "行\n名字")]))
        for text in invalid:
            with self.subTest(kind=type(text).__name__), self.assertRaises(GroupEventError) as raised:
                self.parse(text)
            for private in ("private_value", "file:///", "<sysmsg", NEW):
                self.assertNotIn(private, str(raised.exception))

    def test_xml_size_complexity_and_missing_target_link_are_bounded(self):
        base = join()
        maximum = MAX_GROUP_SYSTEM_BYTES
        self.assertEqual(self.parse(base + " " * (maximum - len(base.encode("utf-8"))))[0].member_id, NEW)
        invalid = (base + " " * maximum,
                   system('$username$加入群聊', username=[(NEW, "文" * (maximum // 2))]),
                   base.replace("<plain />", "<x>" * 17 + "</x>" * 17),
                   base.replace("<plain />", "<x />" * 1024),
                   base.replace("<plain />", '<x ' + " ".join(f'a{i}="x"' for i in range(65)) + " />"),
                   system('$username$加入群聊', unrelated=[(NEW, "新群友")]))
        for text in invalid:
            with self.subTest(length=len(text)), self.assertRaises(GroupEventError):
                self.parse(text)


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.tracker = GroupMembershipTracker(BOT, SESSION, clock=lambda: self.now)
        self.baseline = {BOT: "机器人", ALICE: "群昵称小艾", BOB: "鲍勃"}

    def start(self):
        self.assertEqual(self.tracker.update(ROOM, self.baseline), ())

    def observe(self, content, **options):
        return self.tracker.observe_system(ROOM, content, 10002, **options)

    def test_first_snapshot_is_only_baseline_and_members_are_copies(self):
        self.start()
        self.assertEqual(self.tracker.update(ROOM, self.baseline), ())
        self.assertEqual(self.tracker.members(ROOM), self.baseline)
        copy = self.tracker.members(ROOM)
        copy.pop(ALICE)
        self.assertIn(ALICE, self.tracker.members(ROOM))

    def test_joins_and_leaves_require_two_consecutive_complete_snapshots(self):
        self.start()
        changed = {BOT: "机器人", BOB: "鲍勃", NEW: "新人昵称"}
        self.assertEqual(self.tracker.update(ROOM, changed), ())
        self.assertIn(ALICE, self.tracker.members(ROOM))
        events = self.tracker.update(ROOM, changed)
        self.assertEqual({(e.kind, e.member_id, e.nickname) for e in events},
                         {("leave", ALICE, "群昵称小艾"), ("join", NEW, "新人昵称")})
        self.assertTrue(all(e.source == "membership" for e in events))
        self.assertEqual(self.tracker.update(ROOM, changed), ())

    def test_none_empty_bad_or_missing_self_snapshot_never_causes_mass_leave(self):
        self.start()
        invalid = (None, {}, {ALICE: "小艾"}, [(BOT, "机器人")], {BOT: "行\n名字"},
                   {BOT: "机器人", "700@chatroom": "错误成员"})
        for snapshot in invalid:
            with self.subTest(snapshot=snapshot):
                self.assertEqual(self.tracker.update(ROOM, snapshot), ())
                self.assertEqual(self.tracker.update(ROOM, snapshot), ())
                self.assertEqual(self.tracker.members(ROOM), self.baseline)

    def test_failed_snapshot_interrupts_confirmation(self):
        self.start()
        changed = {**self.baseline, NEW: "新人"}
        self.assertEqual(self.tracker.update(ROOM, changed), ())
        self.assertEqual(self.tracker.update(ROOM, None), ())
        self.assertEqual(self.tracker.update(ROOM, changed), ())
        self.assertEqual(self.tracker.update(ROOM, changed)[0].member_id, NEW)

    def test_transient_presence_and_absence_do_not_accumulate_nonconsecutive_counts(self):
        self.start()
        joined = {**self.baseline, NEW: "新人"}
        without_alice = {k: v for k, v in self.baseline.items() if k != ALICE}
        for changed in (joined, without_alice):
            self.assertEqual(self.tracker.update(ROOM, changed), ())
            self.assertEqual(self.tracker.update(ROOM, self.baseline), ())
            self.assertEqual(self.tracker.update(ROOM, changed), ())
            self.assertEqual(self.tracker.update(ROOM, self.baseline), ())
        self.assertEqual(self.tracker.members(ROOM), self.baseline)

    def test_name_changes_update_without_events_and_departure_keeps_last_name(self):
        self.start()
        renamed = {**self.baseline, ALICE: "新的群昵称"}
        self.assertEqual(self.tracker.update(ROOM, renamed), ())
        self.assertEqual(self.tracker.members(ROOM)[ALICE], "新的群昵称")
        self.assertEqual(self.tracker.update(ROOM, {**renamed, ALICE: ""}), ())
        self.assertEqual(self.tracker.members(ROOM)[ALICE], "新的群昵称")
        gone = {k: v for k, v in renamed.items() if k != ALICE}
        self.tracker.update(ROOM, gone)
        event, = self.tracker.update(ROOM, gone)
        self.assertEqual(event.nickname, "新的群昵称")

    def test_system_messages_before_baseline_do_not_send_historical_welcomes(self):
        self.assertEqual(self.observe(join(), sequence=10), ())
        self.start()
        self.assertEqual(self.observe(join(), sequence=10), ())
        self.assertNotIn(NEW, self.tracker.members(ROOM))

    def test_system_and_snapshot_share_baseline_and_do_not_duplicate_events(self):
        self.start()
        event, = self.observe(join(), sequence=1)
        self.assertEqual((event.kind, event.member_id), ("join", NEW))
        self.assertEqual(self.observe(join(), sequence=1), ())
        self.assertEqual(self.observe(join(), sequence=2), ())
        changed = {**self.baseline, NEW: "新群友"}
        self.assertEqual(self.tracker.update(ROOM, changed), ())
        self.assertEqual(self.tracker.update(ROOM, changed), ())
        event, = self.observe(leave(), sequence=3)
        self.assertEqual((event.kind, event.member_id, event.nickname), ("leave", ALICE, "群昵称小艾"))
        self.assertEqual(self.observe(leave(), sequence=4), ())
        changed.pop(ALICE)
        self.assertEqual(self.tracker.update(ROOM, changed), ())
        self.assertEqual(self.tracker.update(ROOM, changed), ())

    def test_system_change_cache_lag_does_not_generate_reverse_event(self):
        self.start()
        self.observe(join(), sequence=1)
        self.observe(leave(), sequence=2)
        for _ in range(3):
            self.assertEqual(self.tracker.update(ROOM, self.baseline), ())
        changed = {BOT: "机器人", BOB: "鲍勃", NEW: "新群友"}
        self.assertEqual(self.tracker.update(ROOM, changed), ())
        self.assertEqual(self.tracker.update(ROOM, changed), ())

    def test_system_cache_expectation_expires_and_then_requires_fresh_confirmations(self):
        self.start()
        self.observe(join(), sequence=1)
        self.now += 121
        self.assertEqual(self.tracker.update(ROOM, self.baseline), ())
        event, = self.tracker.update(ROOM, self.baseline)
        self.assertEqual((event.kind, event.member_id), ("leave", NEW))

    def test_delayed_duplicate_sequence_does_not_undo_newer_state(self):
        self.start()
        self.observe(join(), sequence=1)
        event, = self.observe(leave(NEW, "新群友"), sequence=2)
        self.assertEqual(event.kind, "leave")
        self.assertEqual(self.observe(join(), sequence=1), ())
        self.assertNotIn(NEW, self.tracker.members(ROOM))
        self.assertEqual(self.observe(join(), sequence=3)[0].kind, "join")

    def test_bot_and_name_only_system_messages_never_produce_automatic_events(self):
        self.start()
        for content in (join(BOT, "机器人"), leave(BOT, "机器人"),
                        '"同名群友"加入了群聊',
                        system('$username$加入了群聊', username=[("", "没有ID")])):
            self.assertEqual(self.observe(content), ())
        self.assertEqual(self.tracker.members(ROOM), self.baseline)
        self.assertEqual(self.tracker.observe_system(ROOM, join(), 1), ())

    def test_plain_scan_name_fills_only_a_confirmed_member_without_cached_name(self):
        self.start()
        self.assertEqual(self.observe('"示例群友"通过扫描你分享的二维码加入群聊', sequence=31), ())
        changed = {**self.baseline, NEW: ""}
        self.assertEqual(self.tracker.update(ROOM, changed), ())
        event, = self.tracker.update(ROOM, changed)
        self.assertEqual((event.kind, event.member_id, event.nickname), ("join", NEW, "示例群友"))
        self.assertEqual(self.tracker.members(ROOM)[NEW], "示例群友")
        self.assertEqual(self.tracker.update(ROOM, changed), ())
        self.assertEqual(self.observe('"示例群友"通过扫描你分享的二维码加入群聊', sequence=31), ())

    def test_system_name_does_not_override_a_real_group_nickname(self):
        self.start()
        self.observe('"微信原名"加入了群聊')
        changed = {**self.baseline, NEW: "本群昵称"}
        self.tracker.update(ROOM, changed)
        event, = self.tracker.update(ROOM, changed)
        self.assertEqual(event.nickname, "本群昵称")

    def test_name_hint_does_not_identify_ambiguous_simultaneous_members(self):
        self.start()
        self.observe('"名字甲"加入了群聊')
        changed = {**self.baseline, NEW: "", "wxid_another_new": ""}
        self.tracker.update(ROOM, changed)
        self.assertEqual([event.nickname for event in self.tracker.update(ROOM, changed)], ["", ""])

    def test_multiple_system_names_do_not_guess_one_new_members_name(self):
        self.start()
        self.observe('"名字甲"加入了群聊')
        self.observe('"名字乙"加入了群聊')
        changed = {**self.baseline, NEW: ""}
        self.tracker.update(ROOM, changed)
        self.assertEqual(self.tracker.update(ROOM, changed)[0].nickname, "")

    def test_expired_or_prebaseline_names_are_not_reused(self):
        self.observe('"历史名字"加入了群聊')
        self.start()
        changed = {**self.baseline, NEW: ""}
        self.tracker.update(ROOM, changed)
        self.assertEqual(self.tracker.update(ROOM, changed)[0].nickname, "")
        self.observe('"过期名字"加入了群聊')
        self.now += 61
        changed["wxid_later_new"] = ""
        self.tracker.update(ROOM, changed)
        self.assertEqual(self.tracker.update(ROOM, changed)[0].nickname, "")

    def test_name_hints_are_group_scoped_and_do_not_trigger_leaves(self):
        self.start()
        self.tracker.update(OTHER_ROOM, self.baseline)
        self.observe('"只属于群甲"加入了群聊')
        changed = {**self.baseline, NEW: ""}
        self.tracker.update(OTHER_ROOM, changed)
        self.assertEqual(self.tracker.update(OTHER_ROOM, changed)[0].nickname, "")
        self.assertEqual(self.tracker.update(ROOM, self.baseline), ())

    def test_name_hint_store_is_bounded(self):
        self.start()
        for index in range(80):
            self.observe(f'"名字{index}"加入了群聊')
        self.assertEqual(len(self.tracker._rooms[ROOM].name_hints), 64)

    def test_existing_members_system_name_is_not_assigned_to_a_newcomer(self):
        self.start()
        self.observe('"群昵称小艾"加入了群聊')
        changed = {**self.baseline, NEW: ""}
        self.tracker.update(ROOM, changed)
        self.assertEqual(self.tracker.update(ROOM, changed)[0].nickname, "")

    def test_structured_join_does_not_leave_a_name_hint_for_the_next_person(self):
        self.start()
        self.observe('"示例群友"加入了群聊')
        self.observe(join(nickname="示例群友"))
        changed = {**self.baseline, NEW: "示例群友", "wxid_next_person": ""}
        self.tracker.update(ROOM, changed)
        event, = self.tracker.update(ROOM, changed)
        self.assertEqual((event.member_id, event.nickname), ("wxid_next_person", ""))

    def test_independent_rooms_and_connections_do_not_share_baselines(self):
        self.start()
        self.assertEqual(self.tracker.update(OTHER_ROOM, {**self.baseline, NEW: "另一个群昵称"}), ())
        self.assertEqual(self.tracker.members(OTHER_ROOM)[NEW], "另一个群昵称")
        self.assertNotIn(NEW, self.tracker.members(ROOM))
        with self.assertRaises(RuntimeError):
            self.tracker.update(ROOM, self.baseline, session_id="old-session")
        with self.assertRaises(RuntimeError):
            self.observe(join(), session_id="old-session")
        self.tracker.close()
        replacement = GroupMembershipTracker(BOT, "new-session")
        self.assertEqual(replacement.update(ROOM, {**self.baseline, NEW: "新人"}), ())

    def test_accounts_have_independent_self_identity_and_state(self):
        self.start()
        other_bot = "wxid_group_event_other_robot"
        other = GroupMembershipTracker(other_bot, SESSION)
        self.assertEqual(other.update(ROOM, {**self.baseline, other_bot: "另一机器人"}), ())
        event, = other.observe_system(ROOM, join(NEW, "另一账号看到的名字"), 10002)
        self.assertEqual(event.member_id, NEW)
        self.assertNotIn(NEW, self.tracker.members(ROOM))
        self.assertEqual(other.update(ROOM, self.baseline), ())
        self.assertEqual(other.update(ROOM, self.baseline), ())
        self.assertIn(other_bot, other.members(ROOM))

    def test_configurable_confirmations_and_capacity_limits(self):
        tracker = GroupMembershipTracker(BOT, SESSION, confirmations=3, max_groups=1, max_members=3)
        tracker.update(ROOM, {BOT: "机器人"})
        snapshot = {BOT: "机器人", NEW: "新人"}
        self.assertEqual(tracker.update(ROOM, snapshot), ())
        self.assertEqual(tracker.update(ROOM, snapshot), ())
        self.assertEqual(tracker.update(ROOM, snapshot)[0].member_id, NEW)
        with self.assertRaises(GroupEventError):
            tracker.update(OTHER_ROOM, {BOT: "机器人"})
        self.assertEqual(tracker.update(ROOM, {BOT: "机器人", ALICE: "A", BOB: "B", NEW: "N"}), ())
        self.assertIn(NEW, tracker.members(ROOM))
        tracker.observe_system(ROOM, join(ALICE, "A"), 10002)
        with self.assertRaises(GroupEventError):
            tracker.observe_system(ROOM, join(BOB, "B"), 10002)

    def test_sequence_cache_and_expected_state_are_bounded(self):
        tracker = GroupMembershipTracker(BOT, SESSION, max_members=3)
        tracker.update(ROOM, {BOT: "机器人"})
        for index in range(280):
            identifier = f"wxid_group_event_{index}"
            tracker.observe_system(ROOM, join(identifier, "临时成员"), 10002, sequence=index * 2 + 1)
            tracker.observe_system(ROOM, leave(identifier, "临时成员"), 10002, sequence=index * 2 + 2)
        state = tracker._rooms[ROOM]
        self.assertLessEqual(len(state.sequences), 256)
        self.assertLessEqual(len(state.expected), 3)
        self.assertEqual(len(state.members), 1)

    def test_concurrent_system_and_snapshot_updates_are_serialized(self):
        self.start()
        changed = {**self.baseline, NEW: "新群友"}
        def operation(index):
            return self.observe(join(), sequence=77) if index % 2 else self.tracker.update(ROOM, changed)
        with ThreadPoolExecutor(max_workers=8) as executor:
            events = [event for batch in executor.map(operation, range(40)) for event in batch]
        self.assertEqual([(event.kind, event.member_id) for event in events], [("join", NEW)])

    def test_invalid_options_closed_tracker_and_unknown_room(self):
        self.assertEqual(self.tracker.members(ROOM), {})
        for options in ({"confirmations": 1}, {"confirmations": True}, {"max_groups": 0},
                        {"max_members": 0}, {"system_grace_seconds": float("nan")},
                        {"system_grace_seconds": -1}, {"clock": None}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                GroupMembershipTracker(BOT, SESSION, **options)
        for identifier in ("filehelper", "700@chatroom", "v3_credentials"):
            with self.assertRaises(ValueError):
                GroupMembershipTracker(identifier, SESSION)
        for sequence in (-1, True, "1", 1 << 64):
            with self.assertRaises(ValueError):
                self.observe(join(), sequence=sequence)
        self.tracker.close()
        self.tracker.close()
        for operation in (lambda: self.tracker.update(ROOM, self.baseline),
                          lambda: self.tracker.members(ROOM), lambda: self.observe(join())):
            with self.assertRaises(RuntimeError):
                operation()


if __name__ == "__main__":
    unittest.main()
