import json
from pathlib import Path
import shutil
import tempfile
import time
import unittest

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
    def chats(self): return []
    def stop(self): self.process.running=False

class LinuxSessionTests(unittest.TestCase):
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

    def test_changed_account_fails_closed_and_preserves_original_workspace(self):
        first=self.sessions[0];original=first.panel.workspace
        first.accept_profile({'logged_in':True,'wxid':'wxid_other','name':'other'})
        self.assertFalse(first.profile['logged_in'])
        self.assertEqual(first.login_state,'error')
        self.assertEqual(first.panel.workspace,original)
        with self.assertRaises(PanelError):first.lease([])
        with self.assertRaises(PanelError):first.validate_request('/api/config',{'expected_account':'wxid_other'})

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
