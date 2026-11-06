import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from wxbot_framework.linux_sessions import LinuxSessionController, ManagedLinuxBackend
from wxbot_framework.panel_common import PanelError
from wxbot_framework.sdk import Message

ROOT=Path(__file__).resolve().parents[1]

class Process:
    pid=99999
    def __init__(self): self.running=True
    def poll(self): return None if self.running else 0

class Backend:
    def __init__(self, account):
        self.account=account;self.process=Process();self.pid=self.process.pid;self.last_error=None;self.calls=[]
    def bind_identity(self,account): self.bound=account
    def set_identity(self,profile): self.account=profile['wxid']
    def send_text(self,target,text): self.calls.append((target,text));return True
    def send_at_text(self,target,text,members): self.calls.append((target,text,members));return True
    def group_members(self,room): return ({'wxid':self.account,'nickname':'fixture'},)
    def chats(self): return []
    def stop(self): self.process.running=False

class LinuxSessionTests(unittest.TestCase):
    def test_upload_snapshots_are_cleaned_only_after_managed_wechat_exits(self):
        owner = self.sessions[0]
        backend = owner.backend
        cleaned = []
        backend._sender = Mock()
        backend._sender.cleanup_retained_files.side_effect = lambda: cleaned.append(backend.process.poll())
        owner.close_wechat()
        self.assertEqual(cleaned, [0])

    def test_image_download_scope_is_bound_to_lease_and_explicit_whitelist(self):
        owner = self.sessions[0]
        owner.backend.configure_image_downloads = Mock(return_value=7)
        owner.backend.clear_image_downloads = Mock()
        owner.backend.request_image = Mock(return_value=True)
        lease = owner.lease(['123@chatroom'])
        lease.start(lambda message: None)
        owner.backend.configure_image_downloads.assert_called_once_with(frozenset({'123@chatroom'}))
        source = Message('', '123@chatroom', type=3, session_id=lease.session_id)
        metadata = object()
        self.assertTrue(lease._request_image(source, metadata))
        self.assertEqual(owner.backend.request_image.call_args.args[:3], (source, metadata, 7))
        self.assertFalse(lease._request_image(Message('', '999@chatroom', type=3), metadata))
        lease.stop()
        owner.backend.clear_image_downloads.assert_called_once_with(7)
        self.assertFalse(lease._request_image(source, metadata))
        self.assertEqual(owner.backend.request_image.call_count, 1)

    def test_empty_allowlist_permits_regular_scoped_image_downloads(self):
        owner = self.sessions[0]
        owner.backend.configure_image_downloads = Mock(return_value=8)
        owner.backend.clear_image_downloads = Mock()
        owner.backend.request_image = Mock(return_value=True)
        lease = owner.lease([])
        lease.start(lambda message: None)
        owner.backend.configure_image_downloads.assert_called_once_with(frozenset())
        for chat in ('123@chatroom', 'wxid_peer'):
            self.assertTrue(lease._request_image(Message('', chat, type=3, session_id=lease.session_id), object()))
        for chat in ('filehelper', 'gh_public', 'weixin'):
            self.assertFalse(lease._request_image(Message('', chat, type=3, session_id=lease.session_id), object()))
        lease.stop()
        owner.backend.clear_image_downloads.assert_called_once_with(8)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for file in ROOT.glob('*.example.json'):shutil.copy2(file,self.root/file.name)
        for name in ('echo','id_responder'):
            shutil.copytree(ROOT/'plugins'/name,self.root/'plugins'/name)
        (self.root/'.runtime/linux/sessions').mkdir(parents=True)
        self.sessions=[]
        for name in ('one','two'):
            obj=LinuxSessionController(self.root,self.root/'.runtime/linux/sessions'/name,wechat_path='/tmp/fixture')
            obj.backend=Backend('wxid_'+name)
            obj.accept_profile({'logged_in':True,'wxid':'wxid_'+name,'name':name})
            self.sessions.append(obj)
        self.addCleanup(lambda:[s.close() for s in self.sessions])

    def wait_for(self, condition):
        deadline=time.monotonic()+4
        while time.monotonic()<deadline:
            if condition():return
            time.sleep(.02)
        self.fail('timed out')

    def test_scheduled_tasks_use_current_linux_account_and_isolate_sessions(self):
        first, second = self.sessions
        task_data = dict(name='测试', chat_id='filehelper', content='测试内容',
                         kind='interval', interval_seconds=300)
        with patch.object(first.panel.scheduled_tasks, 'start'), patch.object(second.panel.scheduled_tasks, 'start'):
            first.panel.guard_route('/api/scheduled-tasks', True)
            first.validate_request('/api/scheduled-tasks', {'expected_account': 'wxid_one'})
            with self.assertRaises(PanelError):
                first.validate_request('/api/scheduled-tasks', {'expected_account': 'wxid_two'})
            with patch('wxbot_framework.scheduled_tasks.time.time', return_value=1000):
                task = first.panel.save_scheduled_task(task_data)['task']
            self.assertEqual(task['account'], 'wxid_one')
            self.assertEqual(len(first.panel.list_scheduled_tasks()['tasks']), 1)
            self.assertEqual(second.panel.list_scheduled_tasks()['tasks'], [])
            with self.assertRaises(PanelError):
                second.panel.delete_scheduled_task({'id': task['id']})
            first.panel.send_connection = Mock(return_value={'session_id':'session'})
            first.panel.send_text = Mock(return_value={'status':'sent'})
            first.panel.scheduled_tasks.tick(1300)
            self.assertEqual(first.panel.send_text.call_count, 1)
            self.assertEqual(first.panel.list_scheduled_tasks()['tasks'][0]['last_status'], '已发送')
            first.profile = dict(first.profile, logged_in=False)
            with self.assertRaises(PanelError):
                first.panel.save_scheduled_task(task_data)
            self.assertEqual(first.panel.list_scheduled_tasks()['tasks'], [])

    def test_scheduled_task_http_uses_linux_identity_and_rejects_stale_account(self):
        from urllib.request import Request, urlopen
        from urllib.error import HTTPError
        from wxbot_framework.panel_http import PanelServer
        first = self.sessions[0]
        server = PanelServer(first, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f'http://127.0.0.1:{server.server_address[1]}'
        def request(path, data=None):
            headers = {'X-Helper-Token': server.token}
            raw = None
            if data is not None:
                headers['Content-Type'] = 'application/json'
                raw = json.dumps(data).encode()
            with urlopen(Request(base + path, data=raw, headers=headers), timeout=4) as response:
                return json.load(response)
        payload = dict(name='提醒', kind='interval', interval_seconds=300,
                       params={'chat_id':'filehelper', 'content':'测试'}, expected_account='wxid_one')
        with patch.object(first.panel.scheduled_tasks, 'start'):
            task = request('/api/scheduled-tasks', payload)['task']
            self.assertEqual(task['account'], 'wxid_one')
            self.assertEqual(request('/api/scheduled-tasks')['tasks'][0]['id'], task['id'])
            with self.assertRaises(HTTPError) as error:
                request('/api/scheduled-tasks/delete', {'id':task['id'], 'expected_account':'wxid_two'})
            self.assertEqual(error.exception.code, 409)
            self.assertTrue(request('/api/scheduled-tasks/delete',
                {'id':task['id'], 'expected_account':'wxid_one'})['ok'])
            self.assertEqual(request('/api/scheduled-tasks')['tasks'], [])

    def test_friend_request_credentials_stay_in_own_lease_and_duplicates_are_suppressed(self):
        from wxbot_framework.friend_requests import parse_friend_request
        owner = self.sessions[0]
        owner.backend.configure_image_downloads = Mock(return_value=None)
        lease = owner.lease([])
        rows = []
        lease.start(rows.append)
        xml = ('<msg fromusername="wxid_peer_fixture" fromnickname="测试好友" '
               'content="测试验证词" scene="30" opcode="2" encryptusername="v3_SYNTHETIC_USER" '
               'ticket="v4_SYNTHETIC_TICKET"/>')
        request = parse_friend_request(xml)
        message = Message('', 'fmessage', sender_id='wxid_peer_fixture', receiver_id='wxid_one',
            type=37, session_id=lease.session_id, data={'self_wxid':'wxid_one', '_linux_friend_request':request})
        lease.receive(message)
        lease.receive(message)
        self.assertEqual(len(rows), 1)
        public = rows[0].data['friend_request']
        self.assertEqual(public['status'], 'pending')
        self.assertEqual(lease.friend_requests(), [public])
        self.assertNotIn('_linux_friend_request', rows[0].data)
        self.assertNotIn('v3_SYNTHETIC_USER', repr(rows[0]))
        self.assertNotIn('v4_SYNTHETIC_TICKET', repr(rows[0]))
        lease.stop()
        with self.assertRaises(RuntimeError):
            lease.friend_requests()

    def test_two_accounts_have_independent_configuration_and_stop(self):
        first,second=self.sessions
        self.assertNotEqual(first.panel.workspace,second.panel.workspace)
        first.panel.save_configuration({'groups':['123@chatroom'],'group_names':{'123@chatroom':'one'}})
        second.panel.save_configuration({'groups':['456@chatroom'],'group_names':{'456@chatroom':'two'}})
        self.assertEqual(first.configuration()['group_names'],{'123@chatroom':'one'})
        self.assertEqual(second.configuration()['group_names'],{'456@chatroom':'two'})
        first.start();second.start()
        self.wait_for(lambda:first.snapshot()['connected'] and second.snapshot()['connected'])
        first._on_message(Message(text='first',chat_id='wxid_peer',sender_id='wxid_peer',session_id='native',sequence=1))
        second._on_message(Message(text='second',chat_id='wxid_peer',sender_id='wxid_peer',session_id='native',sequence=1))
        self.assertEqual(first.message_tail()['rows'][-1]['content'],'first')
        self.assertEqual(second.message_tail()['rows'][-1]['content'],'second')
        first.stop();self.wait_for(lambda:first.snapshot()['state']=='stopped')
        self.assertTrue(first.wechat_running())
        self.assertTrue(second.snapshot()['connected'])
        first.start();self.wait_for(lambda:first.snapshot()['connected'])
        self.assertTrue(second.snapshot()['connected'])

    def test_media_callback_keeps_original_lease_and_does_not_cross_restart(self):
        from dataclasses import replace
        first = self.sessions[0]
        first.backend.instance_home = self.root / 'home'
        receivers = []
        class Receiver:
            def __init__(self, **kwargs):
                self.callback = kwargs['callback']
                self.current = kwargs['is_current']
                self.closed = False
                receivers.append(self)
            def submit(self, message):
                self.message = replace(message, data={'image_status': 'ready'})
                return replace(message, data={'image_status': 'pending'})
            def close(self):
                self.closed = True
        with patch('wxbot_framework.linux_media_receiver.LinuxMediaReceiver', Receiver):
            old_rows, new_rows = [], []
            old = first.lease(['123@chatroom'])
            old.start(old_rows.append)
            source = Message('', '123@chatroom', type=3, session_id='native',
                data={'_linux_media': object()})
            first._on_message(source)
            self.assertEqual(old_rows[0].data['image_status'], 'pending')
            receivers[0].callback(receivers[0].message)
            self.assertEqual(old_rows[-1].data['image_status'], 'ready')
            self.assertTrue(all(row.session_id == old.session_id for row in old_rows))
            old.stop()
            new = first.lease(['123@chatroom'])
            new.start(new_rows.append)
            receivers[0].callback(receivers[0].message)
            self.assertTrue(receivers[0].closed)
            self.assertFalse(receivers[0].current())
            self.assertEqual(new_rows, [])
            first._on_message(replace(source, chat_id='999@chatroom'))
            self.assertEqual(len(receivers), 1)
            self.assertNotIn('_linux_media', new_rows[0].data)
            new.stop()

    def test_media_initialization_failure_is_sanitized_and_text_still_arrives(self):
        first = self.sessions[0]
        first.backend.instance_home = self.root / 'home'
        first.backend.session_id = 'native-current'
        rows = []
        lease = first.lease([])
        lease.start(rows.append)
        self.addCleanup(lease.stop)
        with patch('wxbot_framework.linux_media_receiver.LinuxMediaReceiver',
                   side_effect=PermissionError('private-path-and-key')):
            first._on_message(Message('', 'wxid_peer', type=3, session_id='native-current',
                data={'_linux_media': object(), 'native_message_id': '91'}))
        first._on_message(Message('still receiving', 'wxid_peer', session_id='native-current'))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].data['image_status'], 'failed')
        self.assertEqual(rows[0].data['image_error'], '媒体缓存暂不可用')
        self.assertNotIn('_linux_media', rows[0].data)
        self.assertNotIn('private-path-and-key', repr(rows))
        self.assertEqual(rows[1].text, 'still receiving')
        self.assertTrue(all(row.session_id == lease.session_id for row in rows))

    def test_ready_callback_waits_for_pending_emission_to_complete(self):
        from dataclasses import replace
        first = self.sessions[0]
        first.backend.instance_home = self.root / 'home'
        pending_entered, release_pending, callback_started = (
            threading.Event(), threading.Event(), threading.Event())
        rows, callbacks = [], []
        def emit(message):
            if message.data['image_status'] == 'pending':
                pending_entered.set()
                release_pending.wait(3)
            rows.append(message.data['image_status'])
        class Receiver:
            def __init__(self, **kwargs):
                self.callback = kwargs['callback']
            def submit(self, message):
                def finish():
                    callback_started.set()
                    self.callback(replace(message, data={
                        'native_message_id': '92', 'image_status': 'ready'}))
                worker = threading.Thread(target=finish, daemon=True)
                callbacks.append(worker)
                worker.start()
                return replace(message, data={'native_message_id': '92', 'image_status': 'pending'})
            def close(self): pass
        lease = first.lease([])
        lease.start(emit)
        self.addCleanup(lease.stop)
        with patch('wxbot_framework.linux_media_receiver.LinuxMediaReceiver', Receiver):
            delivery = threading.Thread(target=first._on_message, args=(Message('', 'wxid_peer',
                type=3, session_id='native', data={'_linux_media': object(), 'native_message_id': '92'}),),
                daemon=True)
            delivery.start()
            try:
                self.assertTrue(pending_entered.wait(2))
                self.assertTrue(callback_started.wait(2))
                callbacks[0].join(.1)
                self.assertTrue(callbacks[0].is_alive(), 'ready must wait while pending emitter is blocked')
                self.assertEqual(rows, [])
            finally:
                release_pending.set()
                delivery.join(2)
                for worker in callbacks:
                    worker.join(2)
            self.assertFalse(delivery.is_alive())
            self.assertTrue(all(not worker.is_alive() for worker in callbacks))
            self.assertEqual(rows, ['pending', 'ready'])

    def test_stop_does_not_wait_for_blocked_emitter_and_releases_ready_waiter(self):
        from dataclasses import replace
        first = self.sessions[0]
        first.backend.instance_home = self.root / 'home'
        pending_entered, release_pending = threading.Event(), threading.Event()
        ready_finished, stopped = threading.Event(), threading.Event()
        rows, callbacks, receivers = [], [], []
        def emit(message):
            if message.data['image_status'] == 'pending':
                pending_entered.set()
                release_pending.wait(3)
            rows.append(message.data['image_status'])
        class Receiver:
            def __init__(self, **kwargs):
                self.callback = kwargs['callback']
                self.closed = False
                receivers.append(self)
            def submit(self, message):
                def finish():
                    self.callback(replace(message, data={
                        'native_message_id': '93', 'image_status': 'ready'}))
                    ready_finished.set()
                worker = threading.Thread(target=finish, daemon=True)
                callbacks.append(worker)
                worker.start()
                return replace(message, data={'native_message_id': '93', 'image_status': 'pending'})
            def close(self): self.closed = True
        lease = first.lease([])
        lease.start(emit)
        self.addCleanup(lease.stop)
        def stop():
            lease.stop()
            stopped.set()
        with patch('wxbot_framework.linux_media_receiver.LinuxMediaReceiver', Receiver):
            delivery = threading.Thread(target=first._on_message, args=(Message('', 'wxid_peer',
                type=3, session_id='native', data={'_linux_media': object(), 'native_message_id': '93'}),),
                daemon=True)
            stopper = threading.Thread(target=stop, daemon=True)
            delivery.start()
            try:
                self.assertTrue(pending_entered.wait(2))
                stopper.start()
                self.assertTrue(stopped.wait(1), 'stop must not wait for an emitter that may need the service lock')
                self.assertTrue(ready_finished.wait(1), 'stop must wake the gated ready callback')
                self.assertTrue(receivers[0].closed)
                self.assertEqual(rows, [])
            finally:
                release_pending.set()
                delivery.join(2)
                if stopper.ident is not None:
                    stopper.join(2)
                for worker in callbacks:
                    worker.join(2)
            self.assertFalse(delivery.is_alive())
            self.assertFalse(stopper.is_alive())
            self.assertEqual(rows, ['pending'])

    def test_old_native_capture_cannot_deliver_to_current_lease(self):
        first = self.sessions[0]
        first.backend.session_id = 'native-current'
        rows = []
        lease = first.lease([])
        lease.start(rows.append)
        self.addCleanup(lease.stop)
        first._on_message(Message('old', 'wxid_peer', session_id='native-old'))
        first._on_message(Message('', 'wxid_peer', type=3, session_id='native-old',
            data={'_linux_media': object(), 'native_message_id': '94'}))
        self.assertEqual(rows, [])
        self.assertIsNone(lease._media_receiver)
        first._on_message(Message('current', 'wxid_peer', session_id='native-current'))
        self.assertEqual([row.text for row in rows], ['current'])

    def test_changed_account_fails_closed_and_preserves_original_workspace(self):
        first=self.sessions[0];original=first.panel.workspace
        first.accept_profile({'logged_in':True,'wxid':'wxid_other','name':'other'})
        self.assertFalse(first.profile['logged_in'])
        self.assertEqual(first.login_state,'error')
        self.assertEqual(first.panel.workspace,original)
        with self.assertRaises(PanelError):first.lease([])
        with self.assertRaises(PanelError):first.validate_request('/api/config',{'expected_account':'wxid_other'})

    def test_mention_lease_enforces_scope_account_and_stop(self):
        first, second = self.sessions
        lease = first.lease(['123@chatroom'])
        lease.start(lambda message: None)
        self.assertTrue(lease.send_at_text('123@chatroom', 'test', ['wxid_peer']))
        self.assertEqual(first.backend.calls, [('123@chatroom', 'test', ['wxid_peer'])])
        self.assertEqual(second.backend.calls, [])
        with self.assertRaises(ValueError):
            lease.send_at_text('456@chatroom', 'test', ['wxid_peer'])
        first.profile['wxid'] = 'wxid_changed'
        with self.assertRaises(RuntimeError):
            lease.send_at_text('123@chatroom', 'test', ['wxid_peer'])
        first.profile['wxid'] = 'wxid_one'
        lease.stop()
        with self.assertRaises(RuntimeError):
            lease.send_at_text('123@chatroom', 'test', ['wxid_peer'])
        self.assertEqual(len(first.backend.calls), 1)

    def test_group_member_lease_rejects_wrong_scope_and_account_changed_during_read(self):
        first, second = self.sessions
        lease = first.lease(['123@chatroom'])
        lease.start(lambda message:None)
        self.assertEqual(lease.group_members('123@chatroom')[0]['wxid'],'wxid_one')
        with self.assertRaises(ValueError):lease.group_members('456@chatroom')
        def stale(room):
            first.profile['wxid']='wxid_two'
            return ({'wxid':'wxid_one'},)
        first.backend.group_members=stale
        with self.assertRaises(RuntimeError):lease.group_members('123@chatroom')
        self.assertEqual(second.backend.account,'wxid_two')

    def test_login_qr_is_session_scoped_and_removed_after_login(self):
        session=self.sessions[0]
        class Display:
            def capture_qr(self): return 'cG5n'
            def close(self): pass
        session.display=Display()
        session.profile=dict(session.profile,logged_in=False)
        session.login_state='waiting_login'
        result=session.linux_login_qr()
        self.assertEqual(result,{'status':'qr_available','mime_type':'image/png','qrcode_base64':'cG5n'})
        session.profile=dict(session.profile,logged_in=True)
        self.assertEqual(session.linux_login_qr(),{'status':'logged_in'})
        self.assertNotIn('qrcode_base64',session.linux_login_qr())

    def test_waiting_qr_reports_elapsed_time_without_claiming_a_network_cause(self):
        import time
        session=self.sessions[0]
        class Display:
            def capture_qr(self): return None
            def close(self): pass
        session.display=Display()
        session.profile=dict(session.profile,logged_in=False)
        session.login_state='waiting_login'
        session.login_started_at=time.monotonic()-20
        result=session.linux_login_qr()
        self.assertEqual(result['status'],'waiting')
        self.assertGreaterEqual(result['elapsed_seconds'],20)
        self.assertNotIn('qrcode_base64',result)

    def test_old_qr_is_discarded_when_session_changes_during_capture(self):
        session=self.sessions[0]
        class Display:
            def capture_qr(self):
                session.profile=dict(session.profile,logged_in=True)
                return 'cG5n'
            def close(self): pass
        session.display=Display()
        session.profile=dict(session.profile,logged_in=False)
        self.assertEqual(session.linux_login_qr(),{'status':'logged_in'})

    def test_linux_panel_restarts_port_without_allowing_two_listeners(self):
        from wxbot_framework.panel_http import PanelServer
        first=PanelServer(self.sessions[0],0)
        port=first.server_address[1]
        try:
            with self.assertRaises(OSError):
                PanelServer(self.sessions[1],port)
        finally:
            first.server_close()
        second=PanelServer(self.sessions[0],port)
        second.server_close()

    def test_each_wechat_has_private_home_and_shared_display_environment(self):
        home=self.root/'home';home.mkdir()
        fixture=self.root/'fixture';fixture.write_text('fake')
        backend=ManagedLinuxBackend(fixture,fixture,fixture,instance_home=home)
        env=backend.launch_environment({'DISPLAY':':0','WAYLAND_DISPLAY':'wayland-0','XDG_RUNTIME_DIR':'/run/user/1000'})
        self.assertEqual(env['HOME'],str(home))
        self.assertEqual(env['TMPDIR'],str(home/'tmp'))
        self.assertEqual(env['WAYLAND_DISPLAY'],'wayland-0')
        self.assertEqual(env['XDG_RUNTIME_DIR'],'/run/user/1000')

if __name__=='__main__':unittest.main()
