"""公众号读取的纯合成测试；不打开微信、网络或真实文章文件。"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime
import json
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from wxbot_framework import official_accounts as reader
from wxbot_framework import official_account_parser as parser

ACCOUNT = "wxid_synthetic_owner"
OWNER = {"username": "gh_synthetic_writer", "nickname": "合成作者", "alias": "SyntheticWriter"}
BIZ = "U3ludGhldGljQml6"
OTHER_BIZ = "U3ludGhldGljT3RoZXI="
MAIN, BROWSER, MODEL, PROFILE = 101, 202, 303, 404
BASE = 0x100000


def article_url(number=1, biz=BIZ, *, tracking=False):
    query = [("__biz", biz), ("mid", str(10000 + number)), ("idx", "1"), ("sn", f"{number:032x}")]
    if tracking:
        query.extend((("key", "synthetic-secret-key"), ("uin", "synthetic-private-uin"), ("pass_ticket", "synthetic-ticket")))
    return "https://mp.weixin.qq.com/s?" + urlencode(query)


def full_article(number=1, *, username=OWNER["username"], biz=BIZ, body="<p>纯合成正文</p>"):
    return {"base_resp": {"ret": 0}, "user_name": username, "nick_name": "合成作者", "alias": "SyntheticWriter",
            "title": f"合成文章{number}", "link": article_url(number, biz, tracking=True),
            "ori_create_time": 1_700_000_000 + number, "content_noencode": body,
            "key": "synthetic-secret-key", "uin": "synthetic-private-uin"}


def directory(numbers=(1, 2, 3)):
    return {"FeaturedList": [{"Title": "不应代替目录的置顶", "ContentUrl": article_url(90)}],
            "Msg": [{"BaseInfo": {"UserName": "", "CreateTime": 1_700_000_000},
                     "AppMsg": {"BaseInfo": {"Username": "synthetic-private-field"}, "DetailInfo": [
                         {"Title": f"合成文章{number}", "ContentUrl": article_url(number, tracking=True), "send_time": 0}
                         for number in numbers]}}],
            "PagingInfo": {"SessionId": "synthetic-session", "ContinueFlag": 1}}


def wire(value, encoding="utf-8"):
    return ("prefix " + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + " suffix").encode(encoding)


class Memory:
    def __init__(self, data=b"\0" * 64, *, hook=None):
        self.data, self.hook = data, hook
        self.closed = False
        self.reads = []

    def read(self, address, size):
        self.reads.append((address, size))
        if self.hook:
            result = self.hook(address, size)
            if result is not None:
                return result
        offset = address - BASE
        if offset < 0 or offset + size > len(self.data):
            raise OSError("synthetic-private-read-error")
        return self.data[offset:offset + size]

    def regions(self, budget):
        budget.check()
        return [(BASE, len(self.data))]

    def close(self):
        self.closed = True


class Platform:
    def __init__(self, source=None, listing=None):
        self.now = 0.0
        self.clock = lambda: self.now
        self.account_value = ACCOUNT
        self.target_value = SimpleNamespace(profile=SimpleNamespace(version="4.2.0.11"))
        self.owner = OWNER.copy()
        self.memories = {MAIN: Memory(), MODEL: Memory(wire(source if source is not None else full_article())),
                         PROFILE: Memory(wire(listing if listing is not None else directory()))}
        self.opened = []
        self.account_calls = 0
        self.process_calls = 0
        self.process_change = None
        self.target_changed = False

    def target(self, pid, budget):
        return self.target_value

    def unchanged(self, pid, target):
        return not self.target_changed

    def account(self, memory, target):
        self.account_calls += 1
        return self.account_value

    def publisher(self, memory, target, expected_account, query):
        return self.owner.copy()

    def memory(self, pid):
        self.opened.append(pid)
        return self.memories[pid]

    def processes(self, pid, budget):
        self.process_calls += 1
        rows = [reader._Process(MAIN, 1, "Weixin.exe", "main-created"),
                reader._Process(BROWSER, MAIN, "WeChatAppEx.exe", "browser-created"),
                reader._Process(MODEL, BROWSER, "WeChatAppEx.exe", "model-created", 6),
                reader._Process(PROFILE, BROWSER, "WeChatAppEx.exe", "profile-created", 7),
                reader._Process(999, 998, "WeChatAppEx.exe", "another-account-browser"),
                reader._Process(1000, 999, "WeChatAppEx.exe", "another-account-renderer", 7)]
        if self.process_calls > 1 and self.process_change:
            rows = self.process_change(rows)
        return rows


class ParserTests(unittest.TestCase):
    def test_source_gh_link_and_success_response_are_required(self):
        proof, = parser.publisher_proofs(full_article(), OWNER["username"])
        self.assertEqual(proof.biz, BIZ)
        self.assertIsNone(proof.content_html)
        self.assertEqual(list(parser.publisher_proofs(full_article(username="gh_synthetic_other"), OWNER["username"])), [])
        failure = full_article()
        failure["base_resp"]["ret"] = 1
        self.assertEqual(list(parser.publisher_proofs(failure, OWNER["username"])), [])

    def test_unverified_userinfo_shape_cannot_establish_publisher_binding(self):
        self.assertEqual(list(parser.publisher_proofs(
            {"biz_username": OWNER["username"], "biz_uin": 1000000001, "uin": "synthetic-private-uin"},
            OWNER["username"])), [])
        self.assertEqual(list(parser.publisher_proofs({"user_name": OWNER["username"], "uin": 1000000001}, OWNER["username"])), [])

    def test_body_must_be_in_same_exact_publisher_and_url_response(self):
        wrapped = {"Content": json.dumps(full_article(2)), "resp_json": json.dumps(full_article(1))}
        proofs = list(parser.publisher_proofs(wrapped, OWNER["username"], requested_url=article_url(1)))
        self.assertEqual(sum(proof.content_html is not None for proof in proofs), 1)
        selected = next(proof for proof in proofs if proof.content_html is not None)
        self.assertEqual(selected.article["url"], article_url(1))

    def test_msg_rows_require_each_public_biz_and_do_not_inherit_featured_ownership(self):
        value = directory()
        value["Msg"][0]["AppMsg"]["DetailInfo"].append({"Title": "其他合成作者", "ContentUrl": article_url(4, OTHER_BIZ)})
        rows = parser.history_articles(value, BIZ)
        self.assertEqual(len(rows), 3)
        self.assertNotIn("其他合成作者", json.dumps(rows, ensure_ascii=False))
        self.assertNotIn("不应代替目录的置顶", json.dumps(rows, ensure_ascii=False))
        self.assertTrue(all(datetime.fromisoformat(row["published_at"].replace("Z", "+00:00")) for row in rows))

    def test_detail_alias_precedence_and_ambiguous_base_url_are_safe(self):
        value = directory((1,))
        appmsg = value["Msg"][0]["AppMsg"]
        appmsg["BaseInfo"] = {"Title": "合成公共基项", "Url": article_url(9)}
        appmsg["DetailInfo"] = [{"Title": "子项优先", "ContentUrl": article_url(1)},
                                {"Title": "没有独立URL的不同子项"}]
        rows = parser.history_articles(value, BIZ)
        self.assertEqual([row["title"] for row in rows], ["子项优先"])
        self.assertEqual(rows[0]["url"], article_url(1))

    def test_utf8_utf16_complete_msg_array_does_not_require_large_featured_root(self):
        value = directory((1, 2))
        value["FeaturedList"] = [{"padding": "x" * 20_000}]
        for encoding in ("utf-8", "utf-16le"):
            whole = wire(value, encoding)
            marker = whole.index('"Msg"'.encode(encoding))
            start = marker - 4096
            raw = whole[start:]
            spans = list(parser.json_spans(raw, marker - start, encoding, message_array=True))
            selected = [span for span in spans if isinstance(span.value, dict) and "Msg" in span.value]
            with self.subTest(encoding=encoding):
                self.assertTrue(selected)
                self.assertEqual(raw[selected[0].offset:selected[0].offset + len(selected[0].payload)], selected[0].payload)
                self.assertEqual(len(parser.history_articles(selected[0].value, BIZ)), 2)

    def test_span_containment_rejects_adjacent_unrelated_response(self):
        prefix = ('"user_name":"' + OWNER["username"] + '";').encode()
        raw = prefix + wire(full_article(username="gh_synthetic_other"))
        marker = raw.index(OWNER["username"].encode())
        spans = list(parser.json_spans(raw, marker, "utf-8"))
        self.assertFalse(any(list(parser.publisher_proofs(span.value, OWNER["username"])) for span in spans))


class NativeGuardTests(unittest.TestCase):
    def login_memory(self, flag=1):
        core, root, context = BASE, BASE + 0x1000, BASE + 0x2000
        values = {
            core + 0xBD0CE30: struct.pack("<Q", root),
            root: struct.pack("<Q", core + 0x9054B98),
            core + 0x9054B98 + 0x68: struct.pack("<Q", core + 0x4FFD0),
            core + 0x4FFD0: b"\x48\x8b\x49\x68\x48\x85\xc9",
            root + 0x68: struct.pack("<Q", context),
            context: struct.pack("<Q", core + 0x90D4F88),
            core + 0x90D4F88: struct.pack("<Q", core + 0x38BDB0),
            core + 0x38BDB0: b"\x0f\xb6\x41\x38\x24\x01\xc3",
            context + 0x38: bytes([flag]),
            context + 0x39: b"\x01",
        }
        return core, context, values

    def test_logged_out_cached_account_and_other_flag_are_rejected(self):
        platform = reader._WindowsPlatform()
        for flag in (0, 2, 1):
            core, _, values = self.login_memory(flag)
            memory = SimpleNamespace(read=lambda address, size: values[address][:size])
            target = SimpleNamespace(core=core, profile=SimpleNamespace(version="4.2.0.11"))
            with patch("wxbot_framework.account.read_current_account", return_value=ACCOUNT) as account:
                self.assertEqual(platform.account(memory, target), ACCOUNT if flag == 1 else None)
                self.assertEqual(account.call_count, 2 if flag == 1 else 1)

    def test_login_code_chain_flag_and_account_must_stay_stable(self):
        platform = reader._WindowsPlatform()
        for change in ("code", "table", "flag", "chain", "account"):
            core, context, values = self.login_memory()
            target = SimpleNamespace(core=core, profile=SimpleNamespace(version="4.2.0.11"))
            if change == "code":
                values[core + 0x38BDB0] = b"\x90" * 7
            if change == "table":
                values[context] = struct.pack("<Q", core + 0x90D4F89)
            flag_reads = 0
            chain_reads = 0

            def read(address, size):
                nonlocal flag_reads, chain_reads
                if address == context + 0x38:
                    flag_reads += 1
                    if change == "flag" and flag_reads == 2:
                        return b"\x00"
                if address == core + 0xBD0CE30:
                    chain_reads += 1
                    if change == "chain" and chain_reads == 2:
                        return struct.pack("<Q", 0)
                if address not in values:
                    raise OSError("synthetic object missing")
                return values[address][:size]

            identities = [ACCOUNT, "wxid_synthetic_replacement"] if change == "account" else [ACCOUNT, ACCOUNT]
            with self.subTest(change=change), patch("wxbot_framework.account.read_current_account", side_effect=identities):
                self.assertIsNone(platform.account(SimpleNamespace(read=read), target))

    def test_cim_query_reads_only_selected_tree_and_outputs_no_command_line(self):
        rows = [
            {"ProcessId": MAIN, "ParentProcessId": 1, "Name": "Weixin.exe", "CreationDate": "created-main"},
            {"ProcessId": BROWSER, "ParentProcessId": MAIN, "Name": "WeChatAppEx.exe", "CreationDate": "created-browser", "IsBrowser": True},
            {"ProcessId": MODEL, "ParentProcessId": BROWSER, "Name": "WeChatAppEx.exe", "CreationDate": "created-model", "RenderType": 6},
            {"ProcessId": 888, "ParentProcessId": MAIN, "Name": "WeChatAppEx.exe", "CreationDate": "created-utility", "IsBrowser": False},
            {"ProcessId": 999, "ParentProcessId": 998, "Name": "WeChatAppEx.exe", "CreationDate": "created-other", "RenderType": 7},
        ]
        completed = SimpleNamespace(stdout=json.dumps(rows).encode())
        with patch.object(reader.subprocess, "run", return_value=completed) as run:
            selected = reader._WindowsPlatform().processes(MAIN, reader._Budget())
        self.assertEqual([item.pid for item in selected], [MAIN, BROWSER, MODEL])
        command = run.call_args.args[0][-1]
        self.assertIn(f"ProcessId={MAIN} OR (ParentProcessId={MAIN} AND Name='WeChatAppEx.exe')", command)
        self.assertIn('ParentProcessId=$taskBrowserId', command)
        self.assertEqual(command.count("Get-CimInstance Win32_Process -Filter"), 2)
        self.assertNotIn("CommandLine", command.split("[pscustomobject]", 1)[1])
        self.assertNotIn("$pid", command.casefold())


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.platform = Platform()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(reader, "_platform", return_value=self.platform))

    def run_read(self, **kwargs):
        return reader.read_loaded_articles(MAIN, ACCOUNT, "合成作者", **kwargs)

    def test_list_only_returns_public_metadata_from_selected_process_chain(self):
        result = self.run_read()
        self.assertEqual(result["publisher"], dict(OWNER, biz=BIZ))
        self.assertEqual(len(result["articles"]), 3)
        self.assertFalse(result["complete_remote_history"])
        self.assertNotIn("article", result)
        encoded = json.dumps(result)
        for forbidden in ("synthetic-secret-key", "synthetic-private-uin", "synthetic-session", "content_html", ACCOUNT):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(set(self.platform.opened), {MAIN, MODEL, PROFILE})
        self.assertTrue(all(self.platform.memories[pid].closed for pid in self.platform.opened))

    def test_requested_article_only_has_its_own_stable_body(self):
        result = self.run_read(url=article_url(1, tracking=True))
        self.assertEqual(len(result["articles"]), 1)
        self.assertEqual(result["article"]["url"], article_url(1))
        self.assertEqual(result["article"]["content_html"], "<p>纯合成正文</p>")
        self.assertEqual(result["article"]["publisher"], dict(OWNER, biz=BIZ))

    def test_value_free_diagnostics_and_source_scan_are_scoped(self):
        self.run_read()
        diagnostic = reader.last_diagnostics()
        self.assertLess(diagnostic["total_read_bytes"], reader._MAX_BYTES)
        self.assertIn("source_read_bytes", diagnostic["renderers"][str(MODEL)])
        self.assertNotIn("source_read_bytes", diagnostic["renderers"][str(PROFILE)])
        self.assertEqual(diagnostic["renderers"][str(PROFILE)]["msg_field_markers"], 1)
        for forbidden in (ACCOUNT, OWNER["username"], "合成文章", "synthetic-secret", "synthetic-private", "content_html"):
            self.assertNotIn(forbidden, json.dumps(diagnostic))
        diagnostic["total_read_bytes"] = -1
        self.assertGreaterEqual(reader.last_diagnostics()["total_read_bytes"], 0)

    def test_other_article_cannot_supply_requested_body(self):
        with self.assertRaises(reader.OfficialAccountReadError) as raised:
            self.run_read(url=article_url(2))
        self.assertEqual(raised.exception.status, 409)
        self.assertNotIn("纯合成正文", str(raised.exception))

    def test_wrong_publisher_url_is_rejected_after_independent_binding(self):
        with self.assertRaises(reader.OfficialAccountReadError) as raised:
            self.run_read(url=article_url(1, OTHER_BIZ))
        self.assertEqual(raised.exception.status, 409)

    def test_msg_alone_cannot_establish_publisher_mapping(self):
        self.platform.memories[MODEL] = Memory(wire({"request": {"user_name": OWNER["username"]}}))
        with self.assertRaises(reader.OfficialAccountReadError) as raised:
            self.run_read()
        self.assertIn("打开该公众号一篇文章", str(raised.exception))

    def test_account_switch_discards_otherwise_valid_body(self):
        original = self.platform.memories[MODEL]

        def switch(address, size):
            self.platform.account_value = "wxid_synthetic_replacement"

        original.hook = switch
        with self.assertRaises(reader.OfficialAccountReadError) as raised:
            self.run_read(url=article_url())
        self.assertEqual(raised.exception.status, 409)

    def test_process_creation_or_parent_changes_discard_result(self):
        def changed(rows):
            return [reader._Process(row.pid, row.parent, row.name,
                                    "reused-created" if row.pid == MODEL else row.created, row.render_type) for row in rows]

        self.platform.process_change = changed
        with self.assertRaises(reader.OfficialAccountReadError):
            self.run_read()
        self.platform.process_calls = 0
        self.platform.process_change = lambda rows: [row for row in rows if row.pid != BROWSER]
        with self.assertRaises(reader.OfficialAccountReadError):
            self.run_read()

    def test_changed_core_is_rejected(self):
        self.platform.target_changed = True
        with self.assertRaises(reader.OfficialAccountReadError):
            self.run_read()

    def test_unstable_exact_json_span_is_not_used_for_mapping_or_body(self):
        memory = self.platform.memories[MODEL]
        source = full_article()
        payload = json.dumps(source, ensure_ascii=False, separators=(",", ":")).encode()

        def mutate(address, size):
            if address == BASE + len(b"prefix ") and size == len(payload):
                return b" " * size

        memory.hook = mutate
        with self.assertRaises(reader.OfficialAccountReadError):
            self.run_read(url=article_url())

    def test_capabilities_never_enumerates_contacts_or_renderers(self):
        self.platform.publisher = Mock(side_effect=AssertionError("must not read contacts"))
        self.platform.processes = Mock(side_effect=AssertionError("must not enumerate renderers"))
        result = reader.capabilities(MAIN, ACCOUNT)
        self.assertEqual(result, {"supported": True, "version": "4.2.0.11", "reason": ""})
        self.assertEqual(self.platform.opened, [MAIN])

    def test_unverified_build_is_501_without_opening_memory(self):
        self.platform.target_value.profile.version = "4.2.0.99"
        with self.assertRaises(reader.OfficialAccountReadError) as raised:
            self.run_read()
        self.assertEqual(raised.exception.status, 501)
        self.assertEqual(self.platform.opened, [])

    def test_budget_returns_only_guarded_partial_metadata(self):
        memory = self.platform.memories[PROFILE]

        def expire(address, size):
            self.platform.now = 10.2

        memory.hook = expire
        result = self.run_read()
        self.assertTrue(result["truncated"])
        self.assertEqual([row["url"] for row in result["articles"]], [article_url(1)])

    def test_hard_budget_cannot_skip_final_identity_validation(self):
        self.platform.memories[PROFILE].hook = lambda address, size: setattr(self.platform, "now", 12.1)
        with self.assertRaises(reader.OfficialAccountReadError) as raised:
            self.run_read()
        self.assertEqual(raised.exception.status, 409)

    def test_ambiguous_browser_parent_chain_is_rejected(self):
        original = self.platform.processes
        self.platform.processes = lambda pid, budget: original(pid, budget) + [
            reader._Process(777, MAIN, "WeChatAppEx.exe", "ambiguous-browser")]
        with self.assertRaises(reader.OfficialAccountReadError):
            self.run_read()

    def test_renderer_limit_excludes_extra_processes_and_reports_truncation(self):
        original = self.platform.processes
        extras = [reader._Process(500 + index, BROWSER, "WeChatAppEx.exe", f"older-{index}", 7) for index in range(10)]
        self.platform.processes = lambda pid, budget: original(pid, budget) + extras
        for process in extras:
            self.platform.memories[process.pid] = Memory()
        result = self.run_read()
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(set(self.platform.opened) - {MAIN}), reader._MAX_RENDERERS)

    def test_missing_or_duplicate_publisher_is_not_fallback_to_another_account(self):
        self.platform.publisher = Mock(side_effect=reader.OfficialAccountReadError("联系人不唯一", 409))
        with self.assertRaises(reader.OfficialAccountReadError):
            self.run_read()
        self.assertEqual(self.platform.opened, [MAIN])


if __name__ == "__main__":
    unittest.main()
