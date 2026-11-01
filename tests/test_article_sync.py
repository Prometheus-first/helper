"""主动分页、会话隔离和一次加载正文的合成验证，不访问微信或网络。"""
from contextlib import AbstractContextManager
from copy import deepcopy
from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlencode

from wxbot_framework import official_account_sync as module
from wxbot_framework.official_accounts import OfficialAccountReadError

ACCOUNT = 'wxid_synthetic_article_owner'
OWNER = {'username': 'gh_synthetic_author', 'nickname': '合成作者', 'alias': 'synthetic_author'}
BIZ = 'U3ludGhldGljQml6'


def url(number=1, biz=BIZ):
    return 'https://mp.weixin.qq.com/s?' + urlencode({'__biz': biz, 'mid': str(number), 'idx': '1', 'sn': 'a' * 32})


def page(*numbers, offset='c3ludGhldGljLWN1cnNvcg==', end=False, publisher=OWNER['username']):
    return {'BaseResponse': {'Ret': 0}, 'AccountInfo': {'UserName': publisher, 'PrivateField': 'synthetic-private'},
            'MsgList': {'Msg': [{'AppMsg': {'BaseInfo': {}, 'DetailInfo': [
                {'Title': '合成文章' + str(n), 'ContentUrl': url(n) + '&key=synthetic-key', 'CreateTime': 1700000000}
                for n in numbers]}}], 'PagingInfo': {'Offset': offset, 'IsEnd': int(end)}},
            'private_token': 'synthetic-response-secret'}


def body():
    owner = dict(OWNER, biz=BIZ)
    return {'publisher': owner, 'article': {'title': '合成文章', 'url': url(), 'published_at': None,
                                           'publisher': owner, 'content_html': '<p>纯合成正文</p>'}}


class Guard(AbstractContextManager):
    def __init__(self, scenario, pid, account, query):
        self.scenario = scenario
        self.owner = scenario.owner.copy()
        self.identity = scenario.identity
        scenario.guards.append(self)

    def verify(self):
        if self.scenario.changed:
            raise OfficialAccountReadError('合成会话已变化', 409)

    def __exit__(self, *args):
        self.scenario.closed += 1


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.owner = OWNER.copy()
        self.identity = ('synthetic-process-birth', 'synthetic-login-session')
        self.changed, self.closed, self.guards = False, 0, []
        self.native = SimpleNamespace(request_page=Mock(), open_article=Mock())
        self.cache = Mock(return_value=body())
        self.sync = module.OfficialAccountSync(guard_factory=lambda *args: Guard(self, *args),
            transport_factory=lambda: self.native, clock=lambda: self.now,
            sleeper=self.sleep, cache_reader=self.cache)

    def sleep(self, seconds):
        self.now += seconds

    def read(self, token=None, **kwargs):
        return self.sync.read_page(101, ACCOUNT, OWNER['nickname'], continuation=token, **kwargs)

    def test_two_pages_use_opaque_server_cursor_but_return_only_local_token_and_public_metadata(self):
        self.native.request_page.side_effect = [page(1, 2), page(2, 3, end=True)]
        first = self.read()
        self.assertTrue(first['has_more'])
        self.assertRegex(first['continuation'], r'^[a-f0-9]{64}$')
        second = self.read(first['continuation'])
        self.assertEqual(self.native.request_page.call_args.args, (101, ACCOUNT, OWNER['username'], 'c3ludGhldGljLWN1cnNvcg=='))
        self.assertEqual([row['title'] for row in second['articles']], ['合成文章3'])
        self.assertEqual((second['page_count'], second['fetched_count']), (2, 3))
        self.assertFalse(second['has_more'])
        self.assertTrue(second['complete_remote_history'])
        serialized = json.dumps([first, second])
        for value in ('synthetic-private', 'synthetic-key', 'synthetic-response-secret', 'c3ludGhldGljLWN1cnNvcg=='):
            self.assertNotIn(value, serialized)
        self.assertEqual(self.closed, 2)

    def test_repeated_or_reused_cursor_fails_and_does_not_restart_silently(self):
        self.native.request_page.side_effect = [page(1), page(2)]
        token = self.read()['continuation']
        with self.assertRaisesRegex(OfficialAccountReadError, '没有前进'):
            self.read(token)
        with self.assertRaisesRegex(OfficialAccountReadError, '已过期'):
            self.read(token)
        self.assertEqual(self.native.request_page.call_count, 2)

    def test_expiry_and_session_pid_account_or_publisher_mismatch_do_not_request_next_page(self):
        for change in ('expiry', 'session', 'pid', 'account', 'publisher'):
            with self.subTest(change=change):
                self.sync.clear();self.owner=OWNER.copy();self.identity=('birth','login');self.now=0
                self.native.request_page.reset_mock();self.native.request_page.side_effect=None
                self.native.request_page.return_value=page(1)
                token=self.read()['continuation']
                args=[101,ACCOUNT,OWNER['nickname']]
                if change=='expiry':self.now=121
                if change=='session':self.identity=('birth','later-login')
                if change=='pid':args[0]=102
                if change=='account':args[1]='wxid_synthetic_other'
                if change=='publisher':self.owner['username']='gh_other_author'
                with self.assertRaises(OfficialAccountReadError):
                    self.sync.read_page(*args,continuation=token)
                self.assertEqual(self.native.request_page.call_count,1)

    def test_wrong_response_owner_or_mixed_link_source_is_rejected(self):
        wrong=page(1,publisher='gh_other_author')
        mixed=page(1,2);mixed['MsgList']['Msg'][0]['AppMsg']['DetailInfo'][1]['ContentUrl']=url(2,'T3RoZXI=')
        for response in (wrong,mixed):
            self.native.request_page.return_value=response
            with self.assertRaisesRegex(OfficialAccountReadError,'来源不一致'):
                self.read()

    def test_bad_remote_status_paging_shape_and_cursor_are_rejected(self):
        cases=[]
        bad=page(1);bad['BaseResponse']['Ret']=True;cases.append(bad)
        bad=page(1);bad['MsgList']['PagingInfo']['IsEnd']=True;cases.append(bad)
        bad=page(1);bad['MsgList']['PagingInfo']['Offset']='private\ninvalid';cases.append(bad)
        bad=page(1);bad['MsgList']['Msg']='wrong';cases.append(bad)
        for response in cases:
            self.native.request_page.return_value=response
            with self.assertRaises(OfficialAccountReadError):self.read()

    def test_empty_page_can_advance_without_fabricating_a_biz(self):
        self.native.request_page.side_effect=[page(),page(1,end=True)]
        first=self.read();second=self.read(first['continuation'])
        self.assertEqual(first['articles'],[])
        self.assertNotIn('biz',first['publisher'])
        self.assertEqual(second['fetched_count'],1)
        self.assertEqual(second['publisher']['biz'],BIZ)

    def test_page_and_unique_article_caps_end_with_partial_status(self):
        self.native.request_page.return_value=page(1,2,3,4)
        with patch.object(module,'MAX_ARTICLES',3):result=self.read()
        self.assertEqual(len(result['articles']),3)
        self.assertTrue(result['limit_reached'])
        self.assertFalse(result['complete_remote_history'])
        self.assertFalse(result['has_more'])
        self.native.request_page.side_effect=[page(1),page(2,offset='bmV4dA==')]
        with patch.object(module,'_MAX_PAGES',2):
            first=self.read();result=self.read(first['continuation'])
        self.assertTrue(result['limit_reached'])
        self.assertFalse(result['has_more'])

    def test_cancellation_or_account_change_discards_result_and_tokens(self):
        self.native.request_page.return_value=page(1)
        with self.assertRaises(OfficialAccountReadError):self.read(cancelled=lambda:True)
        self.native.request_page.assert_not_called()
        def change(*args,**kwargs):self.changed=True;return page(1)
        self.native.request_page.side_effect=change
        with self.assertRaises(OfficialAccountReadError):self.read()
        self.assertEqual(len(self.sync._states),0)

    def test_unknown_native_error_is_value_free(self):
        self.native.request_page.side_effect=RuntimeError('synthetic-private-native-context')
        with self.assertRaises(OfficialAccountReadError) as error:self.read()
        self.assertNotIn('synthetic-private',str(error.exception))

    def test_cached_body_needs_no_native_navigation(self):
        result=self.sync.read_article(101,ACCOUNT,OWNER['username'],url())
        self.assertEqual(result,body())
        self.native.open_article.assert_not_called()

    def test_missing_body_loads_client_once_then_polls_without_reopening(self):
        waiting=OfficialAccountReadError('合成缓存未到达',409)
        self.cache.side_effect=[waiting,waiting,body()]
        result=self.sync.read_article(101,ACCOUNT,OWNER['username'],url())
        self.assertEqual(result['article']['content_html'],'<p>纯合成正文</p>')
        self.native.open_article.assert_called_once()
        self.assertEqual(self.cache.call_count,3)
        self.assertGreater(self.now,0)

    def test_body_timeout_never_repeats_navigation(self):
        def missing(*args,**kwargs):self.now+=4;raise OfficialAccountReadError('未到达',409)
        self.cache.side_effect=missing
        with self.assertRaises(OfficialAccountReadError) as error:
            self.sync.read_article(101,ACCOUNT,OWNER['username'],url())
        self.assertEqual(error.exception.status,504)
        self.native.open_article.assert_called_once()

    def test_body_account_change_after_native_open_stops_polling(self):
        self.cache.side_effect=OfficialAccountReadError('未到达',409)
        self.native.open_article.side_effect=lambda *a,**k:setattr(self,'changed',True)
        with self.assertRaises(OfficialAccountReadError):
            self.sync.read_article(101,ACCOUNT,OWNER['username'],url())
        self.assertEqual(self.cache.call_count,1)


if __name__=='__main__':unittest.main()
