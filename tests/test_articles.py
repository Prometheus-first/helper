"""合成文章、图片与临时目录；不连接微信或公网。"""
from concurrent.futures import ThreadPoolExecutor
from email.message import Message
import base64
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode, urlsplit

from PIL import Image

from wxbot_framework import articles as module
from wxbot_framework.articles import Articles, ArticlesError, normalize_article_url


ACCOUNT = "synthetic-account-one"
OTHER_ACCOUNT = "synthetic-account-two"
PUBLISHER = {"username": "gh_synthetic_publisher", "nickname": "合成公众号", "alias": "synthetic_alias", "biz": "U1lOVEhFVElD"}
CDN_URL = "https://mmbiz.qpic.cn/mmbiz_png/synthetic_asset/640?wx_fmt=png"


def article_url(number=1, **extra):
    return "https://mp.weixin.qq.com/s?" + urlencode({"__biz": PUBLISHER["biz"], "mid": str(number), "idx": "1", "sn": "a" * 32, **extra})


def metadata(number=1):
    return {"title": "合成文章 " + str(number), "url": article_url(number), "published_at": "2026-01-02T03:04:05+08:00"}


def result(number=1, html="<p>合成正文。</p>"):
    return {"publisher": PUBLISHER.copy(), "article": {**metadata(number), "publisher": PUBLISHER.copy(), "content_html": html},
            "complete_remote_history": False, "truncated": False}


def list_result(*numbers):
    return {"publisher": PUBLISHER.copy(), "articles": [metadata(number) for number in numbers], "complete_remote_history": False, "truncated": False}


def png(size=(3, 2)):
    output = BytesIO()
    Image.new("RGB", size, (30, 120, 220)).save(output, format="PNG")
    return output.getvalue()


class Response:
    def __init__(self, data, *, mime="image/png", url=CDN_URL, length=None, status=200, encoding=None):
        self.data = BytesIO(data)
        self.headers = Message()
        self.headers["Content-Type"] = mime
        if length is not None:
            self.headers["Content-Length"] = str(length)
        if encoding is not None:
            self.headers["Content-Encoding"] = encoding
        self.url = url
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, amount):
        return self.data.read(amount)

    read1 = read

    def geturl(self):
        return self.url


class ArticlesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="helper-articles-synthetic-")
        self.workspace = Path(self.temporary.name).resolve()
        self.store = Articles(self.workspace)

    def tearDown(self):
        self.temporary.cleanup()

    def imported(self, html="<p>合成正文。</p>", number=1):
        return self.store.import_article(ACCOUNT, result(number, html))

    def namespace(self, account=ACCOUNT):
        return self.workspace / ".runtime" / "articles" / hashlib.sha256(account.encode()).hexdigest()

    def test_url_normalization_fixed_public_order_and_identity(self):
        raw = article_url(7, uin="SYNTHETIC-PRIVATE", key="SYNTHETIC-SECRET", sessionid="SYNTHETIC-SESSION")
        normalized = normalize_article_url(raw)
        self.assertEqual([name for name, _ in parse_qsl(urlsplit(normalized).query)], ["__biz", "mid", "idx", "sn"])
        self.assertNotIn("PRIVATE", normalized)
        self.assertNotIn("SECRET", normalized)
        self.assertEqual(normalized, normalize_article_url(normalized.replace("&", "&amp;")))
        self.assertEqual(normalized, normalize_article_url(normalized.replace("https://", "http://")))

    def test_url_rejects_unsupported_hosts_protocols_and_ambiguous_identity(self):
        for url in (None, "javascript:alert(1)", "file:///synthetic", "data:text/html,synthetic",
                    article_url().replace("mp.weixin.qq.com", "mp.weixin.qq.com.evil.test"),
                    article_url().replace("https://", "https://user:secret@"),
                    article_url() + "&mid=999", article_url().replace("sn=" + "a" * 32, "sn=invalid"),
                    "https://mp.weixin.qq.com/s/short", article_url() + "\n"):
            with self.subTest(url_type=type(url).__name__):
                # 尾端空白会按输入清理；真正的控制字符不能在URL中间出现。
                if isinstance(url, str) and url.endswith("\n"):
                    url = url.replace("/s?", "/s\n?")
                self.assertIsNone(normalize_article_url(url))

    def test_identifier_is_full_sha256_of_canonical_url(self):
        imported = self.store.import_list(ACCOUNT, list_result(1))["articles"][0]
        self.assertEqual(imported["id"], hashlib.sha256(article_url().encode()).hexdigest())
        self.assertEqual(len(imported["id"]), 64)

    def test_list_import_and_get_metadata_without_body(self):
        self.assertEqual(self.store.list(ACCOUNT)["articles"], [])
        imported = self.store.import_list(ACCOUNT, list_result(1, 2))
        self.assertEqual(len(imported["articles"]), 2)
        row = imported["articles"][0]
        self.assertFalse(row["has_content"])
        self.assertEqual(row["publisher"], PUBLISHER)
        loaded = self.store.get(ACCOUNT, row["id"])
        self.assertEqual((loaded["markdown"], loaded["images"], loaded["has_content"]), ("", [], False))
        self.assertFalse(imported["complete_remote_history"])

    def test_library_accounts_are_isolated_and_names_are_hashed(self):
        row = self.imported()
        with self.assertRaises(ArticlesError) as caught:
            self.store.get(OTHER_ACCOUNT, row["id"])
        self.assertEqual(caught.exception.status, 404)
        self.assertEqual(self.store.list(OTHER_ACCOUNT)["articles"], [])
        self.assertNotIn(ACCOUNT, str(self.namespace()))
        self.assertTrue((self.namespace() / (row["id"] + ".json")).exists())

    def test_empty_account_and_path_traversal_ids_are_rejected(self):
        for account in (None, "", "  ", "synthetic\0account"):
            with self.subTest(account_type=type(account).__name__), self.assertRaises(ArticlesError):
                self.store.list(account)
        for identifier in ("../escape", "a" * 32, "A" * 64, "a" * 63, "a" * 64 + "/child", None):
            with self.subTest(identifier_type=type(identifier).__name__), self.assertRaises(ArticlesError):
                self.store.get(ACCOUNT, identifier)

    def test_markdown_keeps_text_headings_and_ordered_image_placeholders(self):
        html = ('<h2>合成标题</h2><p>第一段<strong>保留文字</strong></p>'
                '<img src="https://untrusted.invalid/tracker" data-src="' + CDN_URL + '&amp;uin=PRIVATE">'
                '<section>第二段<br>第三行</section><img src="' + CDN_URL + '">')
        row = self.imported(html)
        self.assertIn("## 合成标题", row["markdown"])
        self.assertIn("第一段保留文字", row["markdown"])
        self.assertIn("![图片 1](article-image:1)", row["markdown"])
        self.assertIn("![图片 2](article-image:2)", row["markdown"])
        self.assertEqual([image["index"] for image in row["images"]], [1, 2])
        self.assertTrue(all(image["url"] == CDN_URL for image in row["images"]))
        self.assertNotIn("content_html", row)
        self.assertNotIn("PRIVATE", json.dumps(row))

    def test_hidden_html_and_dangerous_links_are_never_returned(self):
        html = ('<script>SECRET-SCRIPT</script><style>SECRET-STYLE</style><noscript>SECRET-NOSCRIPT</noscript>'
                '<p>可见文字</p><a href="javascript:alert(1)">脚本链接</a>'
                '<a href="file:///synthetic/private">文件链接</a><a href="data:text/html,SECRET">数据链接</a>'
                '<p>&lt;script&gt;是文字&lt;/script&gt;</p><iframe src="https://example.invalid">SECRET-FRAME</iframe>')
        row = self.imported(html)
        text = row["markdown"]
        self.assertIn("可见文字", text)
        self.assertIn("脚本链接", text)
        self.assertIn("&lt;script&gt;", text)
        for bad in ("SECRET", "javascript:", "file:", "data:", "<script", "<iframe"):
            self.assertNotIn(bad, text)

    def test_http_links_remove_session_query_and_markdown_input_is_escaped(self):
        row = self.imported('<a href="https://example.invalid/document?sessionid=PRIVATE">公开链接</a><p>![伪图片](file:///private)</p>')
        self.assertIn("[公开链接](https://example.invalid/document)", row["markdown"])
        self.assertNotIn("sessionid", row["markdown"])
        self.assertIn(r"\!\[伪图片\]", row["markdown"])

    def test_only_public_image_cdn_urls_are_kept(self):
        html = '<p>body</p>' + ''.join('<img src="' + url + '">' for url in (
            "https://example.invalid/image.png", "javascript:bad", "data:image/png;base64,private", "file:///private",
            "https://mmbiz.qpic.cn.evil.invalid/a", "https://user:secret@mmbiz.qpic.cn/a", "https://mmbiz.qpic.cn/%2e%2e/a"))
        row = self.imported(html)
        self.assertEqual(row["images"], [])

    def test_image_normalization_only_keeps_valid_public_format(self):
        cleaned = module._image_url(CDN_URL + "&token=PRIVATE&wxfrom=5&wx_lazy=1#private")
        self.assertEqual(cleaned, CDN_URL)
        self.assertEqual(module._image_url(CDN_URL.replace("https://", "//")), CDN_URL)
        self.assertIsNone(module._image_url(CDN_URL + "&wx_fmt=gif"))
        self.assertIsNone(module._image_url(CDN_URL.replace("wx_fmt=png", "wx_fmt=svg")))

    def test_export_substitutes_public_urls_without_local_paths(self):
        row = self.imported('<p>body</p><img src="' + CDN_URL + '">')
        exported = self.store.export(ACCOUNT, row["id"])
        self.assertEqual(set(exported), {"filename", "markdown"})
        self.assertTrue(exported["filename"].endswith(".md"))
        self.assertTrue(exported["markdown"].startswith("# 合成文章 1\n"))
        self.assertIn("公众号：合成公众号", exported["markdown"])
        self.assertIn("2026-01-02T03:04:05+08:00", exported["markdown"])
        self.assertIn("[原文链接](" + article_url() + ")", exported["markdown"])
        self.assertIn("![图片 1](" + CDN_URL + ")", exported["markdown"])
        self.assertNotIn("article-image:", exported["markdown"])
        self.assertNotIn(str(self.workspace), json.dumps(exported))

    def test_export_without_content_is_observable_conflict(self):
        item = self.store.import_list(ACCOUNT, list_result(1))["articles"][0]
        with self.assertRaises(ArticlesError) as caught:
            self.store.export(ACCOUNT, item["id"])
        self.assertEqual(caught.exception.status, 409)

    def test_no_edit_or_draft_api_is_exposed(self):
        self.assertFalse(hasattr(self.store, "save_draft"))
        row = self.imported()
        self.assertNotIn("draft", row)
        self.assertNotIn("revision", row)

    def test_reimport_list_preserves_cached_original_body(self):
        row = self.imported()
        self.store.import_list(ACCOUNT, list_result(1))
        loaded = self.store.get(ACCOUNT, row["id"])
        self.assertEqual(loaded["markdown"], row["markdown"])
        self.assertTrue(loaded["has_content"])

    def test_imports_drop_credentials_and_unexpected_source_fields(self):
        source = result()
        source["sessionid"] = "PRIVATE-SESSION"
        source["article"]["cookie"] = "PRIVATE-COOKIE"
        source["article"]["publisher"]["own_wxid"] = "PRIVATE-ACCOUNT"
        row = self.store.import_article(ACCOUNT, source)
        encoded = json.dumps(row) + (self.namespace() / (row["id"] + ".json")).read_text(encoding="utf-8")
        self.assertNotIn("PRIVATE", encoded)
        self.assertNotIn("cookie", encoded)
        self.assertNotIn("sessionid", encoded)
        self.assertNotIn("content_html", encoded)

    def test_mismatched_publisher_url_is_rejected(self):
        source = result()
        source["article"]["url"] = source["article"]["url"].replace(PUBLISHER["biz"], "OTHER_PUBLIC_BIZ")
        with self.assertRaises(ArticlesError):
            self.store.import_article(ACCOUNT, source)

    def test_plain_dates_and_unloaded_bodies_are_rejected(self):
        source = list_result(1)
        source["articles"][0]["published_at"] = "2026-01-02"
        with self.assertRaises(ArticlesError):
            self.store.import_list(ACCOUNT, source)
        for html in ("", "<script>hidden</script>"):
            with self.subTest(html_type=len(html)), self.assertRaises(ArticlesError):
                self.imported(html)

    def test_content_and_image_count_limits_fail_before_writing_record(self):
        with patch.object(module, "MAX_MARKDOWN_BYTES", 10), self.assertRaises(ArticlesError) as caught:
            self.imported("<p>" + "x" * 11 + "</p>")
        self.assertEqual(caught.exception.status, 413)
        with patch.object(module, "MAX_IMAGES", 1), self.assertRaises(ArticlesError):
            self.imported('<img src="' + CDN_URL + '"><img src="' + CDN_URL + '">')
        self.assertFalse(list(self.workspace.rglob("*.json")))

    def test_partial_sync_fills_capacity_and_preserves_existing_bodies(self):
        saved = self.imported(number=1)
        with patch.object(module, "MAX_ACCOUNT_ARTICLES", 3):
            imported = self.store.import_list(ACCOUNT, list_result(2, 3, 4), allow_partial=True)
            self.assertTrue(imported['capacity_limited'])
            self.assertEqual(len(imported['articles']), 2)
            full = self.store.import_list(ACCOUNT, list_result(4, 1), allow_partial=True)
            self.assertTrue(full['capacity_limited'])
            self.assertEqual([row['id'] for row in full['articles']], [saved['id']])
            self.assertEqual(len(self.store.list(ACCOUNT)['articles']), 3)
            self.assertEqual(self.store.get(ACCOUNT, saved['id'])['markdown'], saved['markdown'])

    def test_library_count_and_disk_limits(self):
        with patch.object(module, "MAX_ACCOUNT_ARTICLES", 1):
            self.store.import_list(ACCOUNT, list_result(1))
            with self.assertRaises(ArticlesError) as caught:
                self.store.import_list(ACCOUNT, list_result(2))
            self.assertEqual(caught.exception.status, 507)
        with patch.object(module, "MAX_ACCOUNT_BYTES", 2), self.assertRaises(ArticlesError) as caught:
            self.imported(number=2)
        self.assertEqual(caught.exception.status, 507)

    def test_full_publisher_sync_does_not_consume_other_publishers_archive_capacity(self):
        first = list_result(1, 2)
        second = list_result(3, 4)
        second['publisher'] = dict(PUBLISHER, username='gh_second_author', nickname='第二合成公众号', biz='U0VDT05E')
        for row in second['articles']:
            row['url'] = row['url'].replace(PUBLISHER['biz'], second['publisher']['biz'])
        with patch.object(module, 'MAX_ARTICLES', 2), patch.object(module, 'MAX_ACCOUNT_ARTICLES', 4):
            self.store.import_list(ACCOUNT, first)
            self.store.import_list(ACCOUNT, second)
            self.assertEqual(len(self.store.list(ACCOUNT)['articles']), 4)
            self.assertEqual(len(self.store.list(ACCOUNT, PUBLISHER['username'])['articles']), 2)
            self.assertEqual(len(self.store.list(ACCOUNT, second['publisher']['username'])['articles']), 2)
            with self.assertRaises(ArticlesError) as caught:
                self.store.import_list(ACCOUNT, list_result(5))
            self.assertEqual(caught.exception.status, 507)
            with self.assertRaises(ArticlesError) as caught:
                self.store.import_list(OTHER_ACCOUNT, list_result(1, 2, 3))
            self.assertEqual(caught.exception.status, 413)

    def test_list_budget_requires_publisher_filter_without_silently_dropping_records(self):
        self.store.import_list(ACCOUNT, list_result(1, 2))
        other = list_result(3)
        other['publisher'] = dict(PUBLISHER, username='gh_second_author', biz='U0VDT05E')
        other['articles'][0]['url'] = other['articles'][0]['url'].replace(PUBLISHER['biz'], other['publisher']['biz'])
        self.store.import_list(ACCOUNT, other)
        first = self.store.list(ACCOUNT, PUBLISHER['username'])
        budget = len(json.dumps(first, ensure_ascii=False).encode('utf-8')) + 10
        with patch.object(module, 'MAX_LIST_BYTES', budget):
            with self.assertRaises(ArticlesError) as caught:
                self.store.list(ACCOUNT)
            self.assertEqual(caught.exception.status, 413)
            self.assertIn('按公众号读取', caught.exception.message)
            self.assertEqual(len(self.store.list(ACCOUNT, PUBLISHER['username'])['articles']), 2)
            self.assertEqual(len(self.store.list(ACCOUNT, other['publisher']['username'])['articles']), 1)
            self.assertEqual(self.store.list(ACCOUNT, 'gh_unknown_author')['articles'], [])
        self.assertEqual(len(self.store.list(ACCOUNT)['articles']), 3)
        for invalid in ('../publisher', 'gh_ invalid', '', 7):
            with self.subTest(invalid=invalid), self.assertRaises(ArticlesError):
                self.store.list(ACCOUNT, invalid)

    def test_existing_article_cannot_be_reassigned_to_another_publisher(self):
        saved = self.imported()
        foreign = dict(PUBLISHER, username='gh_wrong_author')
        listing = list_result(2, 1)
        listing['publisher'] = foreign
        with self.assertRaises(ArticlesError) as caught:
            self.store.import_list(ACCOUNT, listing, allow_partial=True)
        self.assertEqual(caught.exception.status, 409)
        detail = result(1, '<p>wrong publisher replacement</p>')
        detail['publisher'] = detail['article']['publisher'] = foreign
        with self.assertRaises(ArticlesError) as caught:
            self.store.import_article(ACCOUNT, detail)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.store.get(ACCOUNT, saved['id'])['markdown'], saved['markdown'])
        self.assertEqual(self.store.get(ACCOUNT, saved['id'])['publisher'], saved['publisher'])
        self.assertEqual(len(self.store.list(ACCOUNT)['articles']), 1)

    def test_concurrent_instances_do_not_lose_library_items(self):
        def save(number):
            return Articles(self.workspace).import_article(ACCOUNT, result(number))["id"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            identifiers = list(pool.map(save, range(1, 13)))
        self.assertEqual(len(set(identifiers)), 12)
        self.assertEqual(len(self.store.list(ACCOUNT)["articles"]), 12)
        self.assertFalse(list(self.workspace.rglob(".part-*")))

    def test_atomic_failure_leaves_existing_article_unchanged(self):
        row = self.imported()
        with patch.object(module.os, "replace", side_effect=OSError("PRIVATE-PATH")):
            with self.assertRaises(ArticlesError) as caught:
                self.imported("<p>replacement</p>")
        self.assertNotIn("PRIVATE", caught.exception.message)
        self.assertEqual(self.store.get(ACCOUNT, row["id"])["markdown"], row["markdown"])
        self.assertFalse(list(self.workspace.rglob(".part-*")))

    def test_process_lock_serializes_the_article_count_quota(self):
        source = """
import json,sys,time
from wxbot_framework import articles as module
module.MAX_ACCOUNT_ARTICLES=1
save=module.Articles._save
def slow_save(self,*args):
    time.sleep(0.2)
    return save(self,*args)
module.Articles._save=slow_save
store=module.Articles(sys.argv[1])
try:
    store.import_article(sys.argv[2],json.loads(sys.argv[3]))
    print(200)
except module.ArticlesError as error:
    print(error.status)
"""
        processes = [subprocess.Popen(
            [sys.executable, "-B", "-X", "utf8", "-c", source, str(self.workspace), ACCOUNT, json.dumps(result(number))],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) for number in range(1, 4)]
        try:
            statuses = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stderr)
                statuses.append(int(stdout.strip()))
            self.assertEqual(sorted(statuses), [200, 507, 507])
            self.assertEqual(len(self.store.list(ACCOUNT)["articles"]), 1)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    def test_record_hardlink_and_reparse_escape_are_rejected(self):
        row = self.imported()
        path = self.namespace() / (row["id"] + ".json")
        os.link(path, self.workspace / "outside-link")
        with self.assertRaises(ArticlesError):
            self.store.get(ACCOUNT, row["id"])
        (self.workspace / "outside-link").unlink()
        original = Path.lstat
        root = self.namespace()

        def information(candidate):
            real = original(candidate)
            if candidate == root:
                return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_file_attributes=0x400)
            return real

        with patch.object(Path, "lstat", information), self.assertRaises(ArticlesError):
            self.store.get(ACCOUNT, row["id"])

    def test_tampered_cache_identity_is_rejected(self):
        row = self.imported()
        path = self.namespace() / (row["id"] + ".json")
        value = json.loads(path.read_text(encoding="utf-8"))
        value["url"] = article_url(99)
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaises(ArticlesError) as caught:
            self.store.get(ACCOUNT, row["id"])
        self.assertEqual(caught.exception.status, 409)

    def test_image_only_downloads_recorded_url_and_caches_valid_image(self):
        row = self.imported('<img src="' + CDN_URL + '">')
        data = png()
        with patch.object(module, "_download_image", return_value=(data, "image/png")) as download:
            first = self.store.image(ACCOUNT, row["id"], 1)
            second = self.store.image(ACCOUNT, row["id"], 1)
        self.assertEqual(download.call_count, 1)
        download.assert_called_once_with(CDN_URL)
        self.assertEqual(first, second)
        self.assertEqual(first["mime"], "image/png")
        self.assertEqual(base64.b64decode(first["data_base64"]), data)
        self.assertEqual(set(first), {"mime", "data_base64"})

    def test_invalid_image_indices_and_other_account_are_rejected(self):
        row = self.imported('<img src="' + CDN_URL + '">')
        for index in (0, -1, True, "1", 101):
            with self.subTest(index=index), self.assertRaises(ArticlesError):
                self.store.image(ACCOUNT, row["id"], index)
        with self.assertRaises(ArticlesError) as caught:
            self.store.image(OTHER_ACCOUNT, row["id"], 1)
        self.assertEqual(caught.exception.status, 404)

    def test_invalid_or_svg_download_never_creates_cache(self):
        row = self.imported('<img src="' + CDN_URL + '">')
        for data, declared in ((b'<svg onload="bad"/>', "image/svg+xml"), (b"not image", "image/png"),
                               (png(), "image/jpeg"), (b"x" * (module.MAX_IMAGE_BYTES + 1), "image/png")):
            with self.subTest(declared=declared), patch.object(module, "_download_image", return_value=(data, declared)):
                with self.assertRaises(ArticlesError):
                    self.store.image(ACCOUNT, row["id"], 1)
        self.assertFalse(list(self.namespace().rglob("*.img")))

    def test_image_pixel_and_frame_limits(self):
        with patch.object(module, "MAX_IMAGE_BYTES", 2), self.assertRaises(ArticlesError):
            module._image_info(png(), "image/png")
        output = BytesIO()
        frames = [Image.new("RGB", (1, 1), (index % 256, 0, 0)) for index in range(101)]
        frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], duration=10, optimize=False)
        with self.assertRaises(ArticlesError):
            module._image_info(output.getvalue(), "image/gif")
        with patch.object(Image, "open", side_effect=Image.DecompressionBombError("PRIVATE")), self.assertRaises(ArticlesError) as caught:
            module._image_info(png(), "image/png")
        self.assertNotIn("PRIVATE", caught.exception.message)

    def test_download_uses_no_auth_or_proxy_and_limits_response(self):
        data = png()
        response = Response(data, length=len(data))
        requests = []
        def open_response(request, timeout):
            requests.append(request)
            self.assertLessEqual(timeout, 10)
            return response
        opener = SimpleNamespace(open=open_response)
        with patch("urllib.request.build_opener", return_value=opener) as build:
            actual, mime = module._download_image(CDN_URL)
        self.assertEqual((actual, mime), (data, "image/png"))
        proxy = build.call_args.args[0]
        self.assertEqual(proxy.proxies, {})
        self.assertFalse(any(key.lower() in {"cookie", "authorization", "proxy-authorization"} for key, _ in requests[0].header_items()))

    def test_download_deadline_and_stream_without_length_are_bounded(self):
        opener = SimpleNamespace(open=lambda request, timeout: Response(png()))
        with patch("urllib.request.build_opener", return_value=opener), \
                patch.object(module.time, "monotonic", side_effect=[0, 11]), self.assertRaises(ArticlesError) as caught:
            module._download_image(CDN_URL)
        self.assertEqual(caught.exception.status, 504)
        oversized = Response(b"x" * (module.MAX_IMAGE_BYTES + 1))
        with patch("urllib.request.build_opener", return_value=SimpleNamespace(open=lambda request, timeout: oversized)), self.assertRaises(ArticlesError) as caught:
            module._download_image(CDN_URL)
        self.assertEqual(caught.exception.status, 413)

    def test_download_rejects_host_downgrade_headers_overflow_and_truncation(self):
        cases = (Response(png(), url="https://other.invalid/image"), Response(png(), url=CDN_URL.replace("https", "http", 1)),
                 Response(png(), length=module.MAX_IMAGE_BYTES + 1), Response(png(), length=999),
                 Response(png(), encoding="gzip"), Response(png(), status=403))
        for response in cases:
            opener = SimpleNamespace(open=lambda request, timeout: response)
            with self.subTest(status=response.status), patch("urllib.request.build_opener", return_value=opener), self.assertRaises(ArticlesError):
                module._download_image(CDN_URL)

    def test_redirect_handler_rejects_foreign_http_and_excessive_redirects(self):
        captured = []
        def build(*handlers):
            captured.extend(handlers)
            return SimpleNamespace(open=lambda request, timeout: Response(png()))
        with patch("urllib.request.build_opener", side_effect=build):
            module._download_image(CDN_URL)
        redirects = captured[1]
        from urllib.request import Request
        for target in ("https://evil.invalid/image", "http://mmbiz.qpic.cn/a"):
            with self.assertRaises(ArticlesError):
                redirects.redirect_request(Request(CDN_URL), None, 302, "redirect", {}, target)
        redirects.count = 3
        with self.assertRaises(ArticlesError):
            redirects.redirect_request(Request(CDN_URL), None, 302, "redirect", {}, CDN_URL)

    def test_network_failures_do_not_leak_url_or_private_exception(self):
        opener = SimpleNamespace(open=lambda request, timeout: (_ for _ in ()).throw(OSError("PRIVATE-URL-PATH")))
        with patch("urllib.request.build_opener", return_value=opener), self.assertRaises(ArticlesError) as caught:
            module._download_image(CDN_URL)
        self.assertEqual(caught.exception.status, 502)
        self.assertNotIn("PRIVATE", caught.exception.message)
        self.assertNotIn(CDN_URL, caught.exception.message)


if __name__ == "__main__":
    unittest.main()
