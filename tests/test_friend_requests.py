"""好友申请解析与隔离目录测试；不启动微信，不调用任何通过申请的接口。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
import json
import unittest
from xml.sax.saxutils import quoteattr

from wxbot_framework.friend_requests import (
    FriendRequest, FriendRequestError, MAX_FRIEND_REQUEST_XML_BYTES,
    PendingFriendRequests, parse_friend_request,
)

ACCOUNT = "wxid_friend_request_robot"
SESSION = "friend-request-session"
V3 = "v3_private_test_credential"
V4 = "v4_private_test_ticket"


def xml(**changes):
    attributes = {"fromusername": "wxid_test_requester", "fromnickname": "测试好友",
                  "content": "你好，我是测试好友", "scene": "3", "encryptusername": V3,
                  "ticket": V4, "opcode": "2"}
    attributes.update(changes)
    return "<msg " + " ".join(f"{key}={quoteattr(value)}" for key, value in attributes.items()
                             if value is not None) + " />"


class ParseTests(unittest.TestCase):
    def test_extracts_fields_and_hides_credentials_and_original_xml(self):
        request = parse_friend_request(xml(fromnickname="测试&好友", content="你好<朋友>"))
        self.assertEqual((request.sender_id, request.nickname, request.verification, request.scene),
                         ("wxid_test_requester", "测试&好友", "你好<朋友>", 3))
        self.assertEqual((request.encrypted_username, request.ticket), (V3, V4))
        self.assertEqual(request.opcode, 2)
        for secret in (V3, V4, "<msg", "ticket="):
            self.assertNotIn(secret, repr(request))
        self.assertTrue(request.raw_xml.startswith("<msg"))
        self.assertFalse(hasattr(request, "__dict__"))
        with self.assertRaises(FrozenInstanceError):
            request.scene = 4

    def test_sender_can_be_unknown_but_is_not_an_ephemeral_credential(self):
        for sender in (None, "", V3, V4, "v3_" + "x" * 200):
            with self.subTest(sender=sender):
                self.assertEqual(parse_friend_request(xml(fromusername=sender)).sender_id, "")
        for sender in ("123@chatroom", "filehelper", "gh_service", "微信号", "bad id", "x" * 128):
            with self.subTest(sender=sender), self.assertRaises(FriendRequestError):
                parse_friend_request(xml(fromusername=sender))

    def test_optional_names_and_verification_and_nickname_fallback(self):
        result = parse_friend_request(xml(fromusername=None, fromnickname=None, content=None))
        self.assertEqual((result.sender_id, result.nickname, result.verification), ("", "", ""))
        self.assertEqual(parse_friend_request(xml(fromnickname=None, nickname="兼容昵称")).nickname,
                         "兼容昵称")
        self.assertEqual(parse_friend_request(xml(nickname="另一个昵称")).nickname, "测试好友")
        self.assertEqual(parse_friend_request(xml(content="第一行\n第二行")).verification,
                         "第一行\n第二行")

    def test_missing_invalid_credentials_and_scene_are_rejected(self):
        for key in ("encryptusername", "ticket", "scene"):
            for value in (None, ""):
                with self.subTest(key=key, value=value), self.assertRaises(FriendRequestError):
                    parse_friend_request(xml(**{key: value}))
        for key, values in (("encryptusername", ("wxid_someone", "v3_", "v4_wrong", "v3_含中文", "v3_a b")),
                            ("ticket", ("ticket", "v4_", "v3_wrong", "v4_含中文", "v4_a\n")),
                            ("scene", ("-1", "+1", "1.0", " 3", "３", "4294967296", "0" * 11))):
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(FriendRequestError):
                    parse_friend_request(xml(**{key: value}))
        self.assertEqual(parse_friend_request(xml(scene="0")).scene, 0)
        self.assertEqual(parse_friend_request(xml(scene="4294967295")).scene, (1 << 32) - 1)

    def test_opcode_is_required_and_only_known_states_are_preserved(self):
        for opcode in range(7):
            with self.subTest(opcode=opcode):
                self.assertEqual(parse_friend_request(xml(opcode=str(opcode))).opcode, opcode)
        for opcode in (None, "", "-1", "+2", "7", "02", "2.0", "２", " 2"):
            with self.subTest(opcode=opcode), self.assertRaises(FriendRequestError):
                parse_friend_request(xml(opcode=opcode))

    def test_bad_xml_declarations_and_surrogates_never_echo_input(self):
        invalid = (None, b"<msg />", "", " ", xml() + "\0", xml() + "\ud800", "<msg>",
                   xml().replace("<msg", "<friend", 1),
                   '<!DOCTYPE msg [<!ENTITY private "secret">]>' + xml(content="&private;"),
                   '<!DOCTYPE msg SYSTEM "file:///C:/secret.txt">' + xml(),
                   '<!ENTITY private "secret">' + xml(),
                   xml().replace("测试好友", "&unknown;"),
                   xml().replace(' scene="3"', ' scene="3" scene="3"'))
        for value in invalid:
            with self.subTest(kind=type(value).__name__), self.assertRaises(FriendRequestError) as raised:
                parse_friend_request(value)
            for secret in (V3, V4, "<msg", "file:///", "secret"):
                self.assertNotIn(secret, str(raised.exception))

    def test_xml_and_individual_attribute_limits_are_utf8_byte_limits(self):
        body = xml()
        limit = MAX_FRIEND_REQUEST_XML_BYTES
        self.assertEqual(parse_friend_request(body + " " * (limit - len(body.encode("utf-8")))),
                         parse_friend_request(body))
        for body in (body + " " * limit, xml(content="文" * (limit // 2)),
                     xml(content="x" * 4097), xml(fromnickname="x" * 1025),
                     xml(encryptusername="v3_" + "x" * 8190), xml(ticket="v4_" + "x" * 8190),
                     xml(fromnickname="x\n"), xml(content="x\x7f")):
            with self.subTest(length=len(body)), self.assertRaises(FriendRequestError):
                parse_friend_request(body)

    def test_unknown_fields_do_not_become_public_properties(self):
        request = parse_friend_request(xml(headimgurl="https://example.test/private", extra="value"))
        self.assertFalse(hasattr(request, "headimgurl"))
        self.assertNotIn("https://", repr(request))
        # 从根属性读取，不把内层节点误认成第二位申请人。
        with_child = xml().replace(" />", '><brandlist version="1" /></msg>')
        self.assertEqual(parse_friend_request(with_child), parse_friend_request(xml()))

    def test_structure_is_bounded(self):
        for inside in ("<x>" * 17 + "</x>" * 17, "<x />" * 256,
                       '<x ' + " ".join(f'a{i}="x"' for i in range(65)) + " />"):
            body = xml().replace(" />", ">" + inside + "</msg>")
            with self.subTest(length=len(body)), self.assertRaises(FriendRequestError):
                parse_friend_request(body)


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.now = 50.0
        self.directory = PendingFriendRequests(ACCOUNT, SESSION, clock=lambda: self.now)
        self.request = parse_friend_request(xml())

    def resolve(self, request_id, **overrides):
        args = {"account": ACCOUNT, "session_id": SESSION}
        args.update(overrides)
        return self.directory.resolve(request_id, **args)

    def test_add_returns_only_public_metadata_and_stable_opaque_id(self):
        first = self.directory.add(self.request)
        self.assertRegex(first["request_id"], r"^fr_[0-9a-f]{32}$")
        self.assertEqual(first["status"], "pending")
        self.assertEqual(first, self.directory.add(self.request))
        self.assertEqual(self.directory.list(), [first])
        self.assertEqual(self.resolve(first["request_id"]).request, self.request)
        for value in (first, self.directory.list(), self.resolve(first["request_id"])):
            for secret in (V3, V4, "<msg", "raw_xml", "encrypted_username", "ticket"):
                self.assertNotIn(secret, repr(value))

    def test_public_snapshots_cannot_modify_internal_state(self):
        result = self.directory.add(self.request)
        key = result["request_id"]
        result["status"] = "accepted"
        result["ticket"] = "injected"
        snapshot = self.directory.list()
        snapshot[0]["nickname"] = "changed"
        snapshot.clear()
        record = self.resolve(key)
        self.assertEqual(record.status, "pending")
        self.assertEqual(record.request.nickname, "测试好友")
        with self.assertRaises(FrozenInstanceError):
            record.status = "accepted"
        for attribute in ("account", "session_id"):
            with self.assertRaises(AttributeError):
                setattr(self.directory, attribute, "changed")

    def test_same_fields_in_different_xml_formatting_deduplicate(self):
        first = self.directory.add(self.request)
        formatted = parse_friend_request(xml().replace("<msg ", "<msg\n ") + "\n")
        self.assertNotEqual(formatted.raw_xml, self.request.raw_xml)
        self.assertEqual(self.directory.add(formatted)["request_id"], first["request_id"])
        changed = parse_friend_request(xml(ticket=V4 + "_new"))
        self.assertNotEqual(self.directory.add(changed)["request_id"], first["request_id"])

    def test_same_credentials_deduplicate_when_display_fields_change(self):
        first = self.directory.add(self.request)
        self.directory.mark_accepted(first["request_id"], account=ACCOUNT, session_id=SESSION)
        replay = parse_friend_request(xml(fromnickname="另一个昵称", content="换一段验证文字"))
        result = self.directory.add(replay)
        self.assertEqual(result["request_id"], first["request_id"])
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["nickname"], "测试好友")
        self.assertEqual(len(self.directory.list()), 1)

    def test_non_request_opcodes_cannot_enter_or_overwrite_the_directory(self):
        first = self.directory.add(self.request)
        self.directory.mark_accepted(first["request_id"], account=ACCOUNT, session_id=SESSION)
        for opcode in (0, 1, 3, 4, 5, 6):
            notification = parse_friend_request(xml(opcode=str(opcode)))
            with self.subTest(opcode=opcode), self.assertRaises(FriendRequestError):
                self.directory.add(notification)
        result = self.resolve(first["request_id"])
        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.public()["opcode"], 2)
        self.assertEqual(len(self.directory.list()), 1)

    def test_resolve_rechecks_opcode_before_exposing_internal_credentials(self):
        key = self.directory.add(self.request)["request_id"]
        record = self.resolve(key)
        # 即使内部目录被错误代码填入状态通知，手动通过入口仍然守住边界。
        self.directory._records[key] = replace(record, request=parse_friend_request(xml(opcode="3")))
        with self.assertRaises(FriendRequestError):
            self.resolve(key)
        with self.assertRaises(FriendRequestError):
            self.directory.mark_accepted(key, account=ACCOUNT, session_id=SESSION)

    def test_account_and_connection_must_match_on_resolve_and_update(self):
        key = self.directory.add(self.request)["request_id"]
        for values in ({"account": "wxid_another_robot"}, {"session_id": "other-connection"}):
            with self.subTest(values=values), self.assertRaises(RuntimeError):
                self.resolve(key, **values)
            owner = {"account": ACCOUNT, "session_id": SESSION, **values}
            for method in (self.directory.mark_accepted, self.directory.mark_failed,
                           self.directory.mark_rejected):
                with self.assertRaises(RuntimeError):
                    method(key, **owner)
        other = PendingFriendRequests(ACCOUNT, "other-connection")
        with self.assertRaises(KeyError):
            other.resolve(key, account=ACCOUNT, session_id="other-connection")
        self.assertNotEqual(other.add(self.request)["request_id"], key)

    def test_processed_and_failed_records_stay_deduplicated_without_implicit_retry(self):
        for status in ("accepted", "failed", "rejected"):
            directory = PendingFriendRequests(ACCOUNT, SESSION)
            first = directory.add(self.request)
            method = getattr(directory, "mark_" + status)
            updated = method(first["request_id"], account=ACCOUNT, session_id=SESSION)
            self.assertEqual(updated["status"], status)
            self.assertEqual(directory.add(self.request), updated)
            self.assertEqual(method(first["request_id"], account=ACCOUNT, session_id=SESSION), updated)
            other = directory.mark_failed if status != "failed" else directory.mark_accepted
            with self.assertRaises(RuntimeError):
                other(first["request_id"], account=ACCOUNT, session_id=SESSION)

    def test_failure_reason_never_serializes_raw_exception_or_credentials(self):
        class PrivateError:
            def __str__(self):
                raise AssertionError("不应格式化原始异常")
        key = self.directory.add(self.request)["request_id"]
        result = self.directory.mark_failed(key, account=ACCOUNT, session_id=SESSION,
                                            error=PrivateError())
        self.assertEqual(result["error"], "friend_request_failed")
        self.assertNotIn(V4, json.dumps(self.directory.list(), ensure_ascii=False))

    def test_ttl_is_fixed_retention_not_extended_by_replay(self):
        first = self.directory.add(self.request)
        self.assertEqual(self.resolve(first["request_id"]).expires_at, 3650)
        self.now = 3649
        self.assertEqual(self.directory.add(self.request), first)
        self.now = 3650
        self.assertEqual(self.directory.list(), [])
        with self.assertRaises(KeyError):
            self.resolve(first["request_id"])
        self.assertNotEqual(self.directory.add(self.request)["request_id"], first["request_id"])

    def test_capacity_evicts_oldest_including_processed_record(self):
        directory = PendingFriendRequests(ACCOUNT, SESSION, max_requests=2)
        first = directory.add(self.request)
        directory.mark_accepted(first["request_id"], account=ACCOUNT, session_id=SESSION)
        second = directory.add(parse_friend_request(xml(ticket=V4 + "_2")))
        third = directory.add(parse_friend_request(xml(ticket=V4 + "_3")))
        self.assertEqual([r["request_id"] for r in directory.list()],
                         [second["request_id"], third["request_id"]])
        with self.assertRaises(KeyError):
            directory.resolve(first["request_id"], account=ACCOUNT, session_id=SESSION)

    def test_close_clears_and_invalidates_all_operations(self):
        key = self.directory.add(self.request)["request_id"]
        self.directory.close()
        self.directory.close()
        self.assertEqual(len(self.directory._records), 0)
        for operation in (self.directory.list, lambda: self.directory.add(self.request),
                          lambda: self.resolve(key),
                          lambda: self.directory.mark_accepted(key, account=ACCOUNT, session_id=SESSION)):
            with self.assertRaises(RuntimeError):
                operation()

    def test_parallel_duplicate_adds_have_one_record(self):
        with ThreadPoolExecutor(max_workers=8) as executor:
            rows = list(executor.map(lambda _: self.directory.add(self.request), range(32)))
        self.assertEqual(len({row["request_id"] for row in rows}), 1)
        self.assertEqual(len(self.directory.list()), 1)

    def test_invalid_initialization_and_ids_are_rejected(self):
        for account in (None, "", "filehelper", "1@chatroom", "gh_service"):
            with self.subTest(account=account), self.assertRaises(ValueError):
                PendingFriendRequests(account, SESSION)
        for session in (None, "", "x" * 257):
            with self.subTest(session=session), self.assertRaises(ValueError):
                PendingFriendRequests(ACCOUNT, session)
        for options in ({"ttl_seconds": 0}, {"ttl_seconds": -1}, {"ttl_seconds": float("inf")},
                        {"ttl_seconds": float("nan")}, {"ttl_seconds": True},
                        {"max_requests": 0}, {"max_requests": 10001}, {"max_requests": True},
                        {"clock": None}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                PendingFriendRequests(ACCOUNT, SESSION, **options)
        for request_id in (None, "", V3, V4, "../id", "fr_not_valid"):
            with self.subTest(request_id=request_id), self.assertRaises(ValueError) as raised:
                self.resolve(request_id)
            if request_id:
                self.assertNotIn(str(request_id), str(raised.exception))

    def test_manually_forged_dataclass_is_not_trusted(self):
        for request in (None, {}, xml()):
            with self.subTest(kind=type(request).__name__), self.assertRaises(TypeError):
                self.directory.add(request)
        for request in (replace(self.request, scene=4), replace(self.request, ticket="v4_other"),
                        replace(self.request, raw_xml="<msg />")):
            with self.assertRaises(FriendRequestError):
                self.directory.add(request)
        self.assertEqual(self.directory.list(), [])


if __name__ == "__main__":
    unittest.main()
