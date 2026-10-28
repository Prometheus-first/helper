"""管理台手动文本／真实艾特、群身份和账号隔离；仅使用模拟连接。"""
import threading
import time
import unittest
from test_web_panel import PanelFixture
from wxbot_framework.sdk import ServiceFacade, Message, PluginContext
from wxbot_framework.web_panel import PanelError
from wxbot_framework.runtime import BotApplication
from wxbot_framework.message_monitor import MessageBuffer

ROOM='123@chatroom'
PEER='wxid_test_member'
SELF='wxid_test_robot'


class SendBackend:
    session_id='session-messaging'
    def __init__(self):
        self.calls=[]
        self.rows=({'wxid':SELF,'nickname':'测试机器人','role':'owner'},
                   {'wxid':PEER,'nickname':'群内称呼','role':'admin'})
        self.gate=None
    def group_members(self,room):
        if self.gate is not None:self.gate.wait(2)
        return self.rows
    def send_text(self,target,text):
        self.calls.append((target,text,()))
        return True
    def send_at_text(self,target,text,members):
        self.calls.append((target,text,tuple(members)))
        return True
    def stop(self):return {}


class ComposerTests(PanelFixture):
    def setUp(self):
        super().setUp()
        self.controller.start()
        self.backend=SendBackend()
        self.service=ServiceFacade(text_delay={'enabled':False})
        self.service.bind(self.backend)
        self.addCleanup(self.service.close)
        self.controller.app.backend=self.backend
        self.controller.app.service=self.service
        for _ in range(100):
            if self.controller.app._connection_ready:break
            time.sleep(.005)
    def data(self,**changes):
        return dict(request_id='synthetic-request-0001',session_id=self.backend.session_id,
                    chat_id=ROOM,content='大家下午好',at_list=[],**changes)
    def wait_job(self,identifier='synthetic-request-0001'):
        for _ in range(300):
            job=self.controller.send_status(identifier)
            if job['status']!='sending':return job
            time.sleep(.005)
        self.fail('模拟发送没有完成')
    def test_plain_text_and_request_deduplication(self):
        data=self.data();self.controller.send_text(data)
        self.assertEqual(self.wait_job()['status'],'sent')
        self.controller.send_text(data)
        self.assertEqual(len(self.backend.calls),1)
        with self.assertRaises(PanelError):self.controller.send_text(dict(data,content='第二条'))
    def test_real_multi_member_mentions_use_current_group_names(self):
        data=self.data();data['at_list']=[PEER,SELF]
        self.controller.send_text(data)
        self.assertEqual(self.wait_job()['status'],'sent')
        self.assertEqual(self.backend.calls,[(ROOM,'@群内称呼\u2005 @测试机器人\u2005\n大家下午好',(PEER,SELF))])
    def test_group_members_export_roles_and_session(self):
        data=self.controller.group_members(ROOM)
        self.assertEqual(data['session_id'],self.backend.session_id)
        self.assertEqual([row['role'] for row in data['members']],['owner','admin'])
        self.assertEqual(self.controller.send_connection(),{'session_id':self.backend.session_id})
    def test_missing_group_cache_is_an_explicit_retryable_error(self):
        self.backend.rows=None
        with self.assertRaisesRegex(PanelError,'缓存尚未就绪'):self.controller.group_members(ROOM)
    def test_stale_connection_and_unlisted_chats_are_rejected(self):
        data=self.data();data['session_id']='expired-session'
        with self.assertRaises(PanelError):self.controller.send_text(data)
        self.controller.app.config['chat_allowlist']=['999@chatroom']
        with self.assertRaises(PanelError):self.controller.send_text(self.data())
        self.assertFalse(self.backend.calls)
    def test_departed_members_do_not_send(self):
        data=self.data();data.update(request_id='synthetic-invalid-0',at_list=['wxid_departed'])
        self.controller.send_text(data)
        self.assertEqual(self.wait_job(data['request_id'])['status'],'failed')
        self.assertFalse(self.backend.calls)

    def test_long_mention_message_is_split_in_order(self):
        data=self.data();data.update(at_list=[PEER],content='文'*341)
        self.controller.send_text(data)
        self.assertEqual(self.wait_job()['status'],'sent')
        self.assertEqual(''.join(row[1] for row in self.backend.calls), '@群内称呼\u2005\n' + data['content'])
        self.assertEqual(self.backend.calls[0][2], (PEER,))
        self.assertTrue(all(not row[2] for row in self.backend.calls[1:]))
    def test_pending_send_blocks_other_requests_and_expired_session_cancels(self):
        gate=self.backend.gate=threading.Event()
        try:
            data=self.data();data['at_list']=[PEER]
            self.controller.send_text(data)
            with self.assertRaises(PanelError):self.controller.send_text(dict(data,request_id='another-synthetic-request'))
            self.service.cancel_pending();gate.set()
            self.assertEqual(self.wait_job()['status'],'failed')
            self.assertFalse(self.backend.calls)
        finally:gate.set()
    def test_invalid_payload_and_private_chat_mention_are_rejected(self):
        for changes in ({'content':''},{'content':'\0'}, {'at_list':[PEER]*2},
                        {'at_list':[PEER],'chat_id':PEER},{'at_list':'bad'},{'request_id':'bad'}):
            with self.subTest(changes=changes),self.assertRaises((ValueError,PanelError)):
                self.controller.send_text(dict(self.data(),**changes))
        self.assertFalse(self.backend.calls)


class RoleApiTests(unittest.TestCase):
    def test_robot_proxy_routes_members_and_send_to_selected_profile(self):
        from types import SimpleNamespace
        from wxbot_framework.robot_hub import RobotProxy
        calls=[]
        hub=SimpleNamespace(request=lambda *args: calls.append(args) or {})
        proxy=RobotProxy(hub,'profile-second')
        proxy.group_members(ROOM);proxy.send_connection()
        proxy.send_text({'chat_id':ROOM});proxy.send_status('synthetic-request-123')
        self.assertEqual(calls,[('profile-second','group-members?room=123%40chatroom',None),
            ('profile-second','send-connection',None),
            ('profile-second','send-text',{'chat_id':ROOM}),
            ('profile-second','send-status?id=synthetic-request-123',None)])

    def test_service_and_plugin_role_queries_preserve_session(self):
        backend=SendBackend();service=ServiceFacade(text_delay={'enabled':False});service.bind(backend)
        self.addCleanup(service.close)
        ctx=PluginContext('example',{},service,[])
        self.assertEqual(ctx.group_member_role(ROOM,SELF,expected_session=backend.session_id),'owner')
        self.assertEqual(ctx.group_member_role(ROOM,PEER),'admin')
        self.assertEqual(ctx.group_member_role(ROOM,'wxid_not_member'),'unknown')
        with self.assertRaises(RuntimeError):ctx.group_member_role(ROOM,PEER,expected_session='expired')
        ctx.send_at_text(ROOM,'你好',[PEER],display_names=['群内称呼'])
        self.assertEqual(backend.calls[0][2],(PEER,))
    def test_monitor_keeps_role_but_forwarded_authors_have_no_inherited_role(self):
        buf=MessageBuffer()
        buf.append(Message('你好',ROOM,PEER,is_group=True,data={'sender_role':'admin'}))
        self.assertEqual(buf.tail()['rows'][0]['sender_role'],'admin')
        buf.append(Message('',ROOM,PEER,is_group=True,type=49,data={'sender_role':'owner','app_type':19,
            'forwarded_records':[{'kind':'text','text':'历史文字','sender_name':'原作者'}]}))
        self.assertEqual(buf.tail()['rows'][-1]['sender_role'],'unknown')
    def test_expired_or_other_connection_role_is_unknown(self):
        app=BotApplication.__new__(BotApplication)
        app.config={'wechat_account':SELF}
        message=Message('你好',ROOM,PEER,is_group=True,session_id='current')
        app._group_roles={ROOM:('current',time.monotonic(),{PEER:'admin'})}
        self.assertEqual(app._with_group_role(message).data['sender_role'],'admin')
        app._group_roles={ROOM:('old',time.monotonic(),{PEER:'owner'})}
        self.assertEqual(app._with_group_role(message).data['sender_role'],'unknown')
        app._group_roles={ROOM:('current',time.monotonic()-31,{PEER:'admin'})}
        self.assertEqual(app._with_group_role(message).data['sender_role'],'unknown')


if __name__=='__main__':unittest.main()
