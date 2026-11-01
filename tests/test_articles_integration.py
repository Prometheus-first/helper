"""Article routes and account isolation; all source records are synthetic."""
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from test_web_panel import PanelFixture, ROOT
from wxbot_framework.panel_common import PanelError
from wxbot_framework.panel_http import PanelServer
from wxbot_framework.robot_hub import RobotProxy, RobotWorker
from wxbot_framework.web_panel import PanelController

ACCOUNT = 'wxid_test_robot'
OTHER = 'wxid_other_robot'
URL = 'https://mp.weixin.qq.com/s?__biz=REVNTw%3D%3D&mid=123&idx=1&sn=' + 'a' * 32
IDENTIFIER = hashlib.sha256(URL.encode()).hexdigest()


def sample(*, detail=False):
    publisher = {'username': 'gh_example_author', 'nickname': '示例作者', 'alias': 'example_author', 'biz': 'REVNTw=='}
    row = {'title': '测试文章', 'url': URL, 'published_at': '2026-01-01T12:00:00+08:00'}
    result = {'publisher': publisher, 'articles': [row], 'complete_remote_history': True, 'truncated': False,
              'has_more': False, 'continuation': None, 'limit_reached': False, 'page_count': 1, 'fetched_count': 1}
    if detail:
        result['article'] = dict(row, publisher=publisher,
                                 content_html='<h2>示例标题</h2><p>用于离线验证的原创测试文字。</p><script>untrusted()</script>')
    return result


class ArticleFixture(PanelFixture):
    def setUp(self):
        super().setUp()
        self.accounts.append({'pid': 4321, 'wxid': OTHER, 'name': '另一个测试账号'})
        self.bind(ACCOUNT)

    def bind(self, account):
        self.controller.bind_account({'wxid': account, 'pid': 1234 if account == ACCOUNT else 4321})

    def body(self, **extra):
        return dict(account=ACCOUNT, publisher='示例作者', **extra)

    def import_article(self):
        self.controller.articles.import_list(ACCOUNT, sample())
        with patch.object(self.controller.article_sync, 'read_article', return_value=sample(detail=True)):
            return self.controller.read_article(self.body(url=URL))


class ArticleControllerTests(ArticleFixture):
    def test_followed_publishers_are_account_bound_without_starting_bot(self):
        owners = {'publishers':[{key:sample()['publisher'][key] for key in ('username','nickname','alias')}],
                  'source':'wechat_contacts', 'complete':False}
        with patch.object(self.controller.article_sync, 'list_publishers', return_value=owners) as read:
            for account in (None, OTHER):
                with self.assertRaises(PanelError):
                    self.controller.article_publishers(account)
            read.assert_not_called()
            result = self.controller.article_publishers(ACCOUNT)
            self.assertEqual(read.call_args.args, (1234, ACCOUNT))
            self.assertFalse(read.call_args.kwargs['cancelled']())
        self.assertIsNone(self.controller.worker)
        self.assertEqual(result['account'], ACCOUNT)
        self.assertEqual(result['publishers'], owners['publishers'])

    def test_account_or_pid_change_discards_followed_directory(self):
        for change in ('account','pid'):
            self.bind(ACCOUNT); self.accounts[0]['pid'] = 1234
            def read(*args, **kwargs):
                if change == 'account':
                    self.bind(OTHER)
                    self.assertTrue(kwargs['cancelled']())
                else:
                    self.accounts[0]['pid'] = 9999
                return {'publishers':[], 'source':'wechat_contacts', 'complete':False}
            with patch.object(self.controller.article_sync, 'list_publishers', side_effect=read):
                with self.assertRaises(PanelError) as error:
                    self.controller.article_publishers(ACCOUNT)
                self.assertEqual(error.exception.status, 409)

    def test_explicit_read_needs_no_running_bot_and_only_persists_public_fields(self):
        with patch.object(self.controller.article_sync, 'read_page', return_value=sample()) as read:
            listing = self.controller.read_articles(self.body())
            self.assertEqual(read.call_args.args, (1234, ACCOUNT, '示例作者'))
            self.assertIsNone(read.call_args.kwargs['continuation'])
            self.assertFalse(read.call_args.kwargs['cancelled']())
        self.assertIsNone(self.controller.worker)
        self.assertEqual(listing['articles'][0]['id'], IDENTIFIER)
        result = self.import_article()
        self.assertEqual(result['account'], ACCOUNT)
        self.assertIn('原创测试文字', result['article']['markdown'])
        self.assertNotIn('content_html', result['article'])
        self.assertNotIn('untrusted()', result['article']['markdown'])
        self.assertEqual(self.controller.article(IDENTIFIER, ACCOUNT), result)
        self.assertIn('测试文章', self.controller.export_article({'account': ACCOUNT, 'id': IDENTIFIER})['markdown'])

    def test_archive_remains_readable_offline_but_not_under_another_account(self):
        self.import_article()
        self.accounts.clear()
        self.assertEqual(len(self.controller.list_articles(ACCOUNT)['articles']), 1)
        self.assertEqual(self.controller.article(IDENTIFIER, ACCOUNT)['article']['title'], '测试文章')
        self.accounts.extend([{'pid': 1234, 'wxid': ACCOUNT}, {'pid': 4321, 'wxid': OTHER}])
        self.bind(OTHER)
        self.assertEqual(self.controller.list_articles(OTHER)['articles'], [])
        with self.assertRaises(PanelError) as error:
            self.controller.article(IDENTIFIER, OTHER)
        self.assertEqual(error.exception.status, 404)
        with self.assertRaises(PanelError):
            self.controller.list_articles(ACCOUNT)

    def test_missing_or_stale_account_is_rejected_before_reading(self):
        with patch.object(self.controller.article_sync, 'read_page') as read:
            for body in ({'publisher': '示例作者'}, {'account': OTHER, 'publisher': '示例作者'}):
                with self.assertRaises(PanelError) as error:
                    self.controller.read_articles(body)
                self.assertEqual(error.exception.status, 409)
            read.assert_not_called()
        with self.assertRaises(PanelError):
            self.controller.list_articles()

    def test_account_change_and_overlapping_read_do_not_commit_old_result(self):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def read(*args, **kwargs):
            entered.set()
            release.wait(3)
            return sample()
        def run():
            try:
                self.controller.read_articles(self.body())
            except PanelError as error:
                errors.append(error)
        with patch.object(self.controller.article_sync, 'read_page', side_effect=read):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                with self.assertRaises(PanelError):
                    self.controller.read_articles(self.body())
                self.bind(OTHER)
            finally:
                release.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual([error.status for error in errors], [409])
        self.assertEqual(self.controller.articles.list(ACCOUNT)['articles'], [])

    def test_pid_change_discards_result_and_releases_read_lock(self):
        def read(*args, **kwargs):
            self.accounts[0]['pid'] = 8888
            return sample()
        with patch.object(self.controller.article_sync, 'read_page', side_effect=read):
            with self.assertRaises(PanelError) as error:
                self.controller.read_articles(self.body())
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.controller.articles.list(ACCOUNT)['articles'], [])
        self.assertFalse(self.controller._articles_read_lock.locked())

    def test_late_image_response_is_not_returned_after_rebinding(self):
        def image(*args):
            self.bind(OTHER)
            return {'mime': 'image/png', 'data_base64': 'cG5n'}
        with patch.object(self.controller.articles, 'image', side_effect=image):
            with self.assertRaises(PanelError) as error:
                self.controller.article_media({'account': ACCOUNT, 'id': IDENTIFIER, 'index': 1})
        self.assertEqual(error.exception.status, 409)

    def test_body_load_requires_article_from_current_account_catalog(self):
        with patch.object(self.controller.article_sync, 'read_article') as load:
            with self.assertRaises(PanelError):
                self.controller.read_article(self.body(url=URL))
            self.controller.articles.import_list(ACCOUNT, sample())
            with self.assertRaises(PanelError):
                self.controller.read_article(dict(self.body(url=URL), publisher='gh_another_author'))
            load.assert_not_called()

    def test_sync_continuation_is_forwarded_and_private_cursor_is_not_exposed(self):
        result = dict(sample(), continuation='a' * 64, has_more=True, complete_remote_history=False)
        with patch.object(self.controller.article_sync, 'read_page', return_value=result) as read:
            listing = self.controller.read_articles(self.body(continuation='b' * 64))
        self.assertEqual(read.call_args.kwargs['continuation'], 'b' * 64)
        self.assertEqual(listing['continuation'], 'a' * 64)
        self.assertEqual(listing['fetched_count'], 1)
        self.assertTrue(listing['has_more'])
        self.assertNotIn('content_html', json.dumps(listing))

    def test_archive_capacity_ends_sync_without_losing_prior_records(self):
        self.controller.articles.import_article(ACCOUNT, sample(detail=True))
        result = dict(sample(), articles=[dict(sample()['articles'][0], url=URL.replace('mid=123', 'mid=456'))],
                      has_more=True, continuation='c' * 64, complete_remote_history=False,
                      page_count=2, fetched_count=2)
        with patch('wxbot_framework.articles.MAX_ACCOUNT_ARTICLES', 1), \
                patch.object(self.controller.article_sync, 'read_page', return_value=result), \
                patch.object(self.controller.article_sync, 'clear') as clear:
            listing = self.controller.read_articles(self.body(continuation='b' * 64))
        clear.assert_called_once()
        self.assertEqual(listing['articles'], [])
        self.assertEqual(listing['fetched_count'], 1)
        self.assertTrue(listing['limit_reached'])
        self.assertTrue(listing['truncated'])
        self.assertFalse(listing['has_more'])
        self.assertFalse(listing['complete_remote_history'])
        self.assertIsNone(listing['continuation'])
        self.assertNotIn('capacity_limited', listing)
        self.assertTrue(self.controller.article(IDENTIFIER, ACCOUNT)['article']['has_content'])

    def test_mismatched_body_url_is_not_saved_as_the_requested_article(self):
        self.controller.articles.import_list(ACCOUNT, sample())
        result = sample(detail=True)
        result['article']['url'] = URL.replace('mid=123', 'mid=456')
        with patch.object(self.controller.article_sync, 'read_article', return_value=result):
            with self.assertRaises(PanelError) as error:
                self.controller.read_article(self.body(url=URL))
        self.assertEqual(error.exception.status, 409)
        self.assertFalse(self.controller.article(IDENTIFIER, ACCOUNT)['article']['has_content'])


class ArticleHTTPTests(ArticleFixture):
    def request(self, path, data=None, *, token=True, origin=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Helper-Token'] = self.server.token
        if origin is not None:
            headers['Origin'] = origin
        body = None if data is None else json.dumps(data).encode('utf-8')
        connection.request('GET' if body is None else 'POST', path, body=body, headers=headers)
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def setUp(self):
        super().setUp()
        shutil.copytree(ROOT / 'webui', self.root / 'webui')
        self.server = PanelServer(self.controller, 0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def test_read_routes_require_local_token_and_archive_export_work(self):
        self.controller.articles.import_list(ACCOUNT, sample())
        with patch.object(self.controller.article_sync, 'read_article', return_value=sample(detail=True)) as read:
            for options in ({'token': False}, {'origin': 'https://attacker.invalid'}):
                try:
                    self.assertEqual(self.request('/api/article-read', self.body(url=URL), **options)[0], 403)
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                    pass
            read.assert_not_called()
            self.assertEqual(self.request('/api/article-read')[0], 404)
            self.assertEqual(self.request('/api/article-read', self.body(url=URL))[0], 200)
        query = urlencode({'account': ACCOUNT, 'id': IDENTIFIER})
        for route in ('/api/articles?' + query, '/api/article?' + query):
            status, _, raw = self.request(route)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)['account'], ACCOUNT)
        status, _, raw = self.request('/api/article-export', {'account': ACCOUNT, 'id': IDENTIFIER})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)['filename'].endswith('.md'))
        self.assertEqual(self.request('/api/article-draft', self.body(id=IDENTIFIER))[0], 404)

    def test_connection_capability_and_static_module(self):
        with patch('wxbot_framework.official_account_sync.capabilities', return_value={'supported': True, 'version': 'test'}):
            status, _, raw = self.request('/api/articles-connection')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(raw)['supported'])
        self.assertEqual(self.request('/articles.js', token=False)[0], 200)

    def test_followed_directory_requires_token_and_explicit_selected_account(self):
        response = {'publishers':[], 'source':'wechat_contacts', 'complete':False}
        route = '/api/articles-publishers?' + urlencode({'account':ACCOUNT})
        with patch.object(self.controller.article_sync, 'list_publishers', return_value=response) as read:
            self.assertEqual(self.request(route, token=False)[0], 403)
            self.assertEqual(self.request(route, origin='https://attacker.invalid')[0], 403)
            self.assertEqual(self.request('/api/articles-publishers')[0], 409)
            read.assert_not_called()
            status, _, raw = self.request(route)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(raw)['account'], ACCOUNT)
            self.assertEqual(read.call_args.args, (1234, ACCOUNT))

    def test_articles_can_be_read_by_exact_publisher(self):
        self.controller.articles.import_list(ACCOUNT, sample())
        query = urlencode({'account':ACCOUNT, 'publisher':'gh_example_author'})
        status, _, raw = self.request('/api/articles?' + query)
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(raw)['articles']), 1)
        query = urlencode({'account':ACCOUNT, 'publisher':'gh_other_author'})
        self.assertEqual(json.loads(self.request('/api/articles?' + query)[2])['articles'], [])


class ArticleProxyTests(unittest.TestCase):
    def test_only_body_read_uses_extended_worker_timeout(self):
        worker = RobotWorker(ROOT, ROOT)
        worker.endpoint, worker.token = 'http://127.0.0.1:12345', 'synthetic-local-token'
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=SimpleNamespace(read=lambda size: b'{}'))
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch.object(worker, 'launch'), patch('urllib.request.build_opener', return_value=opener):
            worker.request('article-read', {})
            self.assertEqual(opener.open.call_args.kwargs['timeout'], 45)
            worker.request('articles-read', {})
            self.assertEqual(opener.open.call_args.kwargs['timeout'], 20)

    def test_all_article_routes_keep_the_robot_and_explicit_account(self):
        hub = SimpleNamespace(request=Mock(return_value={}))
        proxy = RobotProxy(hub, 'robot-example')
        proxy.list_articles(ACCOUNT)
        self.assertEqual(hub.request.call_args.args, ('robot-example', 'articles?account=' + ACCOUNT, None))
        proxy.article_publishers(ACCOUNT)
        self.assertEqual(hub.request.call_args.args, ('robot-example', 'articles-publishers?account=' + ACCOUNT, None))
        proxy.list_articles(ACCOUNT, 'gh_example_author')
        self.assertEqual(hub.request.call_args.args, ('robot-example', 'articles?account=' + ACCOUNT + '&publisher=gh_example_author', None))
        body = {'account': ACCOUNT, 'id': IDENTIFIER}
        for method, route in ((proxy.read_articles, 'articles-read'), (proxy.read_article, 'article-read'),
                              (proxy.article_media, 'article-media'), (proxy.export_article, 'article-export')):
            method(body)
            self.assertEqual(hub.request.call_args.args, ('robot-example', route, body))


class ArticleWorkerTests(unittest.TestCase):
    def test_real_worker_serves_only_its_bound_archive_and_exits_without_starting_bot(self):
        scratch = ROOT / '.runtime/robots'
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='article-test-', dir=scratch)
        self.assertTrue(Path(temporary.name).resolve().is_relative_to(scratch.resolve()))
        self.addCleanup(temporary.cleanup)
        controller = PanelController(ROOT, workspace=temporary.name)
        try:
            controller.store.bind_account(ACCOUNT)
            controller.articles.import_article(ACCOUNT, sample(detail=True))
        finally:
            controller.close()
        worker = RobotWorker(ROOT, temporary.name)
        self.addCleanup(worker.close)
        self.assertEqual(worker.request('state')['state'], 'stopped')
        listing = worker.request('articles?' + urlencode({'account': ACCOUNT}))
        self.assertEqual(listing['articles'][0]['id'], IDENTIFIER)
        detail = worker.request('article?' + urlencode({'account': ACCOUNT, 'id': IDENTIFIER}))
        self.assertIn('原创测试文字', detail['article']['markdown'])
        exported = worker.request('article-export', {'account': ACCOUNT, 'id': IDENTIFIER})
        self.assertEqual(exported['account'], ACCOUNT)
        with self.assertRaises(PanelError) as error:
            worker.request('articles?' + urlencode({'account': OTHER}))
        self.assertEqual(error.exception.status, 409)
        worker.close()
        self.assertIsNotNone(worker.process.poll())


if __name__ == '__main__':
    unittest.main()
