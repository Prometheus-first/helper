"""Outgoing friend requests: validation, account binding, membership and deduplication."""
from pathlib import Path
import sys, threading, time, unittest
from types import SimpleNamespace
from unittest.mock import Mock
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'native'))
from wxbot_framework.friend_add import validate_friend_add
from wxbot_framework.web_panel import PanelController,PanelError
from text_service import NativeTextService

class FriendAddTests(unittest.TestCase):
 def test_validates_real_and_legacy_ids(self):
  for target in ('wxid_fixture','legacyUser123'):
   self.assertEqual(validate_friend_add(target,'123@chatroom','你好')[0],target)
  for target in ('filehelper','123@chatroom','gh_example','bad id','../path','@all'):
   with self.subTest(target=target),self.assertRaises(ValueError): validate_friend_add(target,'123@chatroom','你好')
  for room,message in [('wxid_x','你好'),('123@chatroom',''),('123@chatroom','a\0b'),('123@chatroom','中'*342)]:
   with self.subTest(room=room,message=message[:4]),self.assertRaises(ValueError):validate_friend_add('wxid_fixture',room,message)
  with self.assertRaises(ValueError):validate_friend_add('wxid_fixture','123@chatroom','hello',self_wxid='wxid_fixture')
 def test_native_requires_same_account_and_group_membership(self):
  fake=SimpleNamespace(self_wxid='wxid_self',_calls=threading.RLock(),_owns_bridge=True,
     _stop=threading.Event(),_channel=object(),wechat_version='4.2.0.12',pid=7,preserve_reports=False,
     image_allowlist=frozenset(),friend_add_rooms=('123@chatroom',),chats={},
     group_members=Mock(return_value=[{'wxid':'wxid_friend'}]),_check_account=Mock(),
     _run=Mock(return_value={'success':True}),last_result=None)
  self.assertTrue(NativeTextService.send_friend_request(fake,'wxid_friend','123@chatroom','hello'))
  fake._run.assert_called_once()
  self.assertEqual(fake._run.call_args.kwargs['friend_add'],{'wxid':'wxid_friend','room':'123@chatroom','message':'hello'})
  fake._run.reset_mock();self.assertTrue(NativeTextService.send_friend_request(fake,'wxid_friend','','hello'))
  self.assertEqual(fake._run.call_args.kwargs['friend_add']['room'],'123@chatroom')
  fake._run.reset_mock();fake.group_members.return_value=[]
  with self.assertRaises(ValueError):NativeTextService.send_friend_request(fake,'wxid_friend','123@chatroom','hello')
  fake._run.assert_not_called()
  fake.group_members.return_value=None
  with self.assertRaises(RuntimeError):NativeTextService.send_friend_request(fake,'wxid_friend','123@chatroom','hello')
  fake.wechat_version='4.2.0.11'
  with self.assertRaises(RuntimeError):NativeTextService.send_friend_request(fake,'wxid_friend','123@chatroom','hello')
 def test_auto_match_respects_group_whitelist(self):
  fake=SimpleNamespace(self_wxid='wxid_self',_calls=threading.RLock(),_owns_bridge=True,
     _stop=threading.Event(),_channel=object(),wechat_version='4.2.0.12',pid=7,preserve_reports=False,
     image_allowlist=frozenset({'456@chatroom'}),friend_add_rooms=('123@chatroom',),chats={},
     group_members=Mock(return_value=[{'wxid':'wxid_friend'}]),_check_account=Mock(),
     _run=Mock(return_value={'success':True}),last_result=None)
  with self.assertRaises(ValueError):NativeTextService.send_friend_request(fake,'wxid_friend','','hello')
  fake.group_members.assert_not_called();fake._run.assert_not_called()
  with self.assertRaises(ValueError):NativeTextService.send_friend_request(fake,'wxid_friend','123@chatroom','hello')
  fake._run.assert_not_called()
 def test_http_route_requires_local_token(self):
  import http.client,json
  from wxbot_framework.panel_http import PanelServer
  calls=[]
  controller=SimpleNamespace(send_friend_request=lambda data:calls.append(data) or {'status':'sending'})
  server=PanelServer(controller,0);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
  self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
  def request(token):
   conn=http.client.HTTPConnection('127.0.0.1',server.server_address[1],timeout=3)
   headers={'Content-Type':'application/json'}
   if token:headers['X-Helper-Token']=server.token
   conn.request('POST','/api/friend-request-send',body=json.dumps({'wxid':'fixture'}),headers=headers)
   response=conn.getresponse();status=response.status;response.read();conn.close();return status
  self.assertEqual(request(False),403);self.assertEqual(calls,[])
  self.assertEqual(request(True),200);self.assertEqual(len(calls),1)

 def test_plugin_requires_explicit_capability_and_preserves_session(self):
  from wxbot_framework.sdk import PluginContext,CapabilityError
  service=SimpleNamespace(send_friend_request=Mock(return_value=True))
  denied=PluginContext('test',{},service,set())
  with self.assertRaises(CapabilityError):denied.send_friend_request('wxid_peer','hello')
  service.send_friend_request.assert_not_called()
  allowed=PluginContext('test',{},service,{'friend.request.send'})
  self.assertTrue(allowed.send_friend_request('wxid_peer','hello',expected_session='session_1'))
  service.send_friend_request.assert_called_once_with('wxid_peer','hello',room='',expected_session='session_1')

 def test_panel_deduplicates_and_rejects_stale_or_unavailable_session(self):
  c=PanelController.__new__(PanelController);c.lock=threading.RLock();c.send_jobs={};c.state='running'
  service=SimpleNamespace(session_is_current=Mock(return_value=True),send_friend_request=Mock(return_value=True))
  c.app=SimpleNamespace(_connection_ready=True,service=service,backend=SimpleNamespace(capabilities={'friend.request.send'}),config={'chat_allowlist':[]})
  data={'request_id':'fixture_friend_add_01','session_id':'session_1','wxid':'wxid_friend','room':'123@chatroom','message':'hello'}
  c.send_friend_request(data)
  for _ in range(200):
   if c.send_status(data['request_id'])['status']!='sending':break
   time.sleep(.005)
  self.assertEqual(c.send_status(data['request_id'])['status'],'sent')
  c.send_friend_request(data);service.send_friend_request.assert_called_once()
  with self.assertRaises(PanelError):c.send_friend_request(dict(data,message='changed'))
  service.session_is_current.return_value=False
  with self.assertRaises(PanelError):c.send_friend_request(dict(data,request_id='fixture_friend_add_02'))
  service.session_is_current.return_value=True;c.app.backend.capabilities=set()
  with self.assertRaises(PanelError):c.send_friend_request(dict(data,request_id='fixture_friend_add_03'))
  service.send_friend_request.assert_called_once()
if __name__=='__main__':unittest.main()
