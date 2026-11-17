import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import unittest

from wxbot_framework.linux_sender import LinuxNativeSender, LinuxSendError
from wxbot_framework.linux_wire import (LocalEventListener, LinuxWireError, acknowledge_text_event,
    encode_media_event, encode_text_event, read_bridge_ready, read_message_event)

ROOT = Path(__file__).resolve().parents[1]


class MediaWireTests(unittest.TestCase):
    def test_media_and_text_frames_preserve_type_and_complete_xml(self):
        image = "<msg><img test='图片'/></msg>" * 2000
        for kind in (1, 3, 34, 49):
            a,b=socket.socketpair()
            try:
                import threading
                data = encode_text_event("wxid_fixture", "wxid_self_fixture", image) if kind == 1 else encode_media_event("wxid_fixture", "wxid_self_fixture", image, msg_type=kind)
                writer=threading.Thread(target=lambda: a.sendall(data));writer.start()
                result=read_message_event(b);writer.join(2)
                self.assertEqual(result["type"],kind)
                self.assertEqual(result["text"],image)
            finally:
                a.close();b.close()

    def test_unknown_media_type_is_rejected(self):
        with self.assertRaises(LinuxWireError):
            encode_media_event("wxid_fixture", "wxid_self_fixture", "xml", msg_type=43)
        a,b=socket.socketpair()
        try:
            frame=encode_media_event("wxid_fixture", "wxid_self_fixture", "xml", msg_type=3)
            a.sendall(frame[:24]+struct.pack("!I",43)+frame[28:])
            with self.assertRaises(LinuxWireError):
                read_message_event(b)
        finally:
            a.close();b.close()


class MediaControllerTests(unittest.TestCase):
    def test_media_metadata_is_scoped_and_does_not_persist_tokens(self):
        import json
        from tools.linux_media_test import MediaTest, ARM, FILE_NAME, image_forward_xml
        class Sender:
            def __init__(self):self.calls=[]
            def send_image(self,chat,path):self.calls.append(("image",chat,str(path)));return True
            def send_file(self,chat,path):self.calls.append(("file",chat,str(path)));return True
            def send_generic(self,data):self.calls.append(("generic",data));return {"success":True}
        with tempfile.TemporaryDirectory(prefix="helper-media-controller-") as temporary:
            sender=Sender();test=MediaTest(sender,temporary)
            image={"sender":"wxid_fixture","receiver":"wxid_self_fixture","type":3,
                   "text":'<msg><img aeskey="SECRET_FIXTURE_KEY" cdnmidimgurl="SECRET_FIXTURE_CDN"/></msg>'}
            test.on_event(image)
            self.assertEqual(test.captures,{})
            test.on_event({"sender":"wxid_fixture","receiver":"wxid_self_fixture","type":1,"text":ARM})
            wrong=dict(image,sender="wxid_other_fixture");test.on_event(wrong)
            self.assertEqual(test.captures,{})
            test.on_event(image)
            self.assertIn('filekey="SECRET_FIXTURE_CDN"',image_forward_xml(test.captures[3]))
            file={"sender":"wxid_fixture","receiver":"wxid_self_fixture","type":49,
                  "text":f'<msg><appmsg><title>{FILE_NAME}</title><type>6</type><appattach><cdnattachurl>SECRET_FIXTURE_CDN</cdnattachurl><aeskey>SECRET_FIXTURE_KEY</aeskey></appattach></appmsg></msg>'}
            test.on_event(file)
            self.assertEqual(set(test.captures),{3,49})
            status=json.dumps(test.status())
            self.assertNotIn("SECRET_FIXTURE",status)
            self.assertNotIn("wxid_",status)
            test.execute("send_image");test.execute("send_file")
            test.execute("forward_image");test.execute("forward_file")
            self.assertEqual(len(sender.calls),4)
            with self.assertRaises(RuntimeError):test.execute("send_image")

    def test_voice_metadata_is_scoped_and_runtime_status_contains_no_voice_identifiers(self):
        import json
        from tools.linux_media_test import MediaTest, ARM
        class Sender:
            def bind_voice_test_scope(self, peer, account):return True
        with tempfile.TemporaryDirectory(prefix="helper-voice-controller-") as temporary:
            test=MediaTest(Sender(),temporary,receive_voice=True)
            event={"sender":"wxid_fixture","receiver":"wxid_self_fixture","type":34,
                   "text":'<msg><voicemsg voicelength="2000" length="4096" clientmsgid="SECRET_FIXTURE" bufid="SECRET_FIXTURE"/></msg>'}
            test.on_event(event);self.assertEqual(test.captures,{})
            test.on_event({"sender":"wxid_fixture","receiver":"wxid_self_fixture","type":1,"text":ARM})
            test.on_event(dict(event,sender="wxid_other_fixture"));self.assertNotIn(34,test.captures)
            test.on_event(event)
            self.assertIn(34,test.captures)
            status=json.dumps(test.status())
            self.assertIn('voice',status);self.assertNotIn('SECRET_FIXTURE',status);self.assertNotIn('wxid_',status)
            test.close()

    def test_xml_entities_and_unexpected_attachments_are_rejected(self):
        from tools.linux_media_test import parse_media
        with self.assertRaises(ValueError):parse_media({"type":3,"text":'<!DOCTYPE msg [<!ENTITY x SYSTEM "file:///etc/passwd">]><msg><img/></msg>'})
        self.assertIsNone(parse_media({"type":49,"text":'<msg><appmsg><type>6</type><title>unrelated.txt</title></appmsg></msg>'}))
        with self.assertRaises(ValueError):parse_media({"type":3,"text":'<msg/>'})


class CachedFileTests(unittest.TestCase):
    def test_type74_is_recognized_and_cached_file_matches_account_size_and_hash(self):
        import hashlib
        from tools.linux_media_test import parse_media, FILE_NAME
        from wxbot_framework.linux_media import find_cached_file
        content = b"controlled file fixture"
        md5 = hashlib.md5(content).hexdigest()
        xml = f'<msg><appmsg><title>{FILE_NAME}</title><type>74</type><appattach><totallen>{len(content)}</totallen><fileuploadtoken>PRIVATE_FIXTURE</fileuploadtoken></appattach><md5>{md5}</md5></appmsg></msg>'
        capture = parse_media({"type": 49, "text": xml})
        self.assertEqual(capture["app_type"], 74)
        with tempfile.TemporaryDirectory(prefix="helper-cache-fixture-") as temporary:
            root = Path(temporary)
            other = root / "wxid_other" / "msg/file/2026-11" / FILE_NAME
            other.parent.mkdir(parents=True);other.write_bytes(content)
            prefix_other = root / "wxid_fixture_someone_else" / "msg/file/2026-11" / FILE_NAME
            prefix_other.parent.mkdir(parents=True);prefix_other.write_bytes(content)
            self.assertIsNone(find_cached_file(capture, "wxid_fixture", storage_root=root))
            path = root / "wxid_fixture_ab12" / "msg/file/2026-11" / FILE_NAME
            path.parent.mkdir(parents=True);path.write_bytes(b"wrong checksum data!!!")
            self.assertIsNone(find_cached_file(capture, "wxid_fixture", storage_root=root))
            path.write_bytes(content)
            self.assertEqual(find_cached_file(capture, "wxid_fixture", storage_root=root), path)
            path.unlink();path.symlink_to(other)
            self.assertIsNone(find_cached_file(capture, "wxid_fixture", storage_root=root))

    def test_untrusted_filename_or_missing_hash_is_rejected(self):
        from wxbot_framework.linux_media import find_cached_file
        capture = {"title":"../outside.txt", "md5":"a"*32, "attributes":{"totallen":"1"}}
        with self.assertRaises(ValueError):find_cached_file(capture,"wxid_fixture")
        capture["title"] = "ok.txt";capture["md5"] = ""
        with self.assertRaises(ValueError):find_cached_file(capture,"wxid_fixture")
        with self.assertRaises(ValueError):find_cached_file(capture,"../account")


class CachedImageTests(unittest.TestCase):
    @staticmethod
    def fixture():
        import hashlib,random,zlib
        from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes
        generator = random.Random(1701)
        def chunk(kind,data):
            return struct.pack("!I",len(data))+kind+data+struct.pack("!I",zlib.crc32(kind+data)&0xffffffff)
        rows = b"".join(b"\x00"+generator.randbytes(64*3) for _ in range(32))
        body = b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",struct.pack("!IIBBBBB",64,32,8,2,0,0,0))+chunk(b"IDAT",zlib.compress(rows))+chunk(b"IEND",b"")
        key = bytes(range(16));prefix=body[:1024];middle=body[1024:1088];tail=body[1088:];padding=16-len(prefix)%16
        encryptor=Cipher(algorithms.AES(key),modes.ECB()).encryptor()
        encrypted=encryptor.update(prefix+bytes([padding])*padding)+encryptor.finalize()
        data=b"\x07\x08V2\x08\x07"+struct.pack("<II",len(prefix),len(tail))+b"\x01"+encrypted+middle+bytes(x^93 for x in tail)
        return body,key,data,hashlib.md5(body).hexdigest()

    def test_v2_decryption_requires_complete_checksum_and_rejects_corrupt_data(self):
        from wxbot_framework.linux_media import decode_cached_image
        body,key,encrypted,md5=self.fixture()
        self.assertEqual(decode_cached_image(encrypted,key,md5),body)
        self.assertEqual(decode_cached_image(body,key,md5),body)
        self.assertIsNone(decode_cached_image(encrypted,b"wrong-fixturekey",md5))
        self.assertIsNone(decode_cached_image(encrypted[:-1],key,md5))
        self.assertIsNone(decode_cached_image(encrypted,key,"0"*32))
        corrupt=bytearray(encrypted);corrupt[30]^=1
        self.assertIsNone(decode_cached_image(bytes(corrupt),key,md5))
        with self.assertRaises(ValueError):decode_cached_image(encrypted,key,"invalid-md5")

    def test_cached_image_is_scoped_to_account_and_key_is_not_persisted(self):
        from unittest.mock import patch
        from wxbot_framework.linux_media import read_cached_image
        body,key,encrypted,md5=self.fixture()
        capture={"attributes":{"md5":md5,"length":str(len(body)),"hdlength":"0"}}
        with tempfile.TemporaryDirectory(prefix="helper-v2-fixture-") as temporary:
            root=Path(temporary);other=root/"wxid_other/msg/attach/image.dat";other.parent.mkdir(parents=True);other.write_bytes(encrypted)
            with patch("wxbot_framework.linux_media._read_cache_key",return_value=key) as get_key:
                self.assertIsNone(read_cached_image(capture,"wxid_fixture",123,storage_root=root));get_key.assert_not_called()
                path=root/"wxid_fixture_ab12/msg/attach/image.dat";path.parent.mkdir(parents=True);path.write_bytes(encrypted)
                self.assertEqual(read_cached_image(capture,"wxid_fixture",123,storage_root=root),body);get_key.assert_called_once_with(123)
            self.assertEqual(sorted(p.name for p in path.parent.iterdir()),["image.dat"])
            path.unlink();path.symlink_to(other)
            with patch("wxbot_framework.linux_media._read_cache_key") as get_key:
                self.assertIsNone(read_cached_image(capture,"wxid_fixture",123,storage_root=root));get_key.assert_not_called()

    def test_unknown_process_executable_is_refused_before_key_read(self):
        from wxbot_framework.linux_media import _read_cache_key
        with self.assertRaisesRegex(RuntimeError,"版本指纹"):_read_cache_key(os.getpid())


class AutoImageDownloadTests(unittest.TestCase):
    def test_native_callback_is_not_reported_as_complete_without_verified_file(self):
        from wxbot_framework.linux_image_downloader import LinuxImageDownloader
        from wxbot_framework.image_metadata import ImageMetadata
        from unittest.mock import Mock,patch
        sender=Mock(image_account="wxid_self_fixture",expected_pid=123)
        sender.wait_image_context.return_value=True;sender.download_message_image.return_value=True
        meta=ImageMetadata(md5="a"*32,length=100)
        with tempfile.TemporaryDirectory(prefix="helper-auto-download-") as temporary, patch("wxbot_framework.linux_image_downloader.read_cached_image",return_value=None):
            with self.assertRaises(TimeoutError):LinuxImageDownloader(sender,timeout=.12)(meta,Path(temporary)/"output")
            sender.download_message_image.assert_called_once_with(meta.md5)

    def test_only_complete_original_checksum_is_saved(self):
        from wxbot_framework.linux_image_downloader import LinuxImageDownloader
        from wxbot_framework.image_metadata import ImageMetadata
        from unittest.mock import Mock,patch
        body,_key,_encrypted,md5=CachedImageTests.fixture()
        sender=Mock(image_account="wxid_self_fixture",expected_pid=123)
        sender.wait_image_context.return_value=True;sender.download_message_image.return_value=True
        meta=ImageMetadata(md5=md5,length=10,hdlength=len(body))
        with tempfile.TemporaryDirectory(prefix="helper-auto-download-") as temporary, patch("wxbot_framework.linux_image_downloader.read_cached_image",side_effect=[None,body[:10],body]):
            target=Path(temporary)/"output"
            self.assertTrue(LinuxImageDownloader(sender,timeout=.5)(meta,target))
            self.assertEqual(target.read_bytes(),body)
            self.assertEqual(target.stat().st_mode&0o777,0o600)
            sender.download_message_image.assert_called_once_with(md5)
            with self.assertRaises(ValueError):LinuxImageDownloader(sender)(meta,target)

    def test_cancel_or_old_session_does_not_request_context_or_download(self):
        from wxbot_framework.linux_image_downloader import LinuxImageDownloader
        from wxbot_framework.image_metadata import ImageMetadata
        from unittest.mock import Mock
        meta=ImageMetadata(md5="a"*32,length=100);sender=Mock()
        with tempfile.TemporaryDirectory(prefix="helper-auto-download-") as temporary:
            download=LinuxImageDownloader(sender,is_current=lambda:False)
            self.assertFalse(download(meta,Path(temporary)/"output"));sender.wait_image_context.assert_not_called()
            download=LinuxImageDownloader(sender);download.close()
            self.assertFalse(download(meta,Path(temporary)/"output"));sender.download_message_image.assert_not_called()


@unittest.skipUnless(os.name == "posix" and hasattr(socket,"SO_PEERCRED") and shutil.which("gcc"),"requires Linux/GCC")
class NativeMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(prefix="helper-media-tests-");cls.work=Path(cls.tmp.name)
        cls.receiver=cls.work/"receiver";cls.receiver_bridge=cls.work/"receive.so"
        cls.sender=cls.work/"sender";cls.sender_bridge=cls.work/"send.so"
        subprocess.run(["gcc","-O2","-rdynamic","-fno-builtin",str(ROOT/"tests/fixtures/linux_media_receive_fixture.c"),"-o",str(cls.receiver)],check=True)
        subprocess.run(["gcc","-O2","-rdynamic","-pthread",str(ROOT/"tests/fixtures/linux_send_fixture.c"),"-o",str(cls.sender)],check=True)
        for source,flag,out in (("text_bridge.c","-DHELPER_FIXTURE_TEST",cls.receiver_bridge),("send_bridge.c","-DSDK_FIXTURE",cls.sender_bridge)):
            subprocess.run(["gcc","-O2","-fPIC","-shared","-pthread",flag,str(ROOT/"native/linux"/source),"-ldl","-o",str(out)],check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_receive_arms_one_peer_limits_samples_and_allows_explicit_rearm(self):
        self.check_receive_scope("wxid_friend_fixture")

    def test_receive_arms_legacy_personal_identifier(self):
        self.check_receive_scope("legacy.friend-01")

    def check_receive_scope(self, peer):
        with LocalEventListener() as listener:
            env=os.environ.copy();env.update(LD_PRELOAD=str(self.receiver_bridge),HELPER_LINUX_EVENT_SOCKET=str(listener.path),
                HELPER_LINUX_CONTROLLER_PID=str(os.getpid()),HELPER_LINUX_TEST_MARKERS="1",
                HELPER_LINUX_TEST_MEDIA="1",HELPER_LINUX_MEDIA_ARM_TEXT="WX_LINUX_MEDIA_ARM_11",
                HELPER_FIXTURE_MEDIA_PEER=peer)
            child=subprocess.Popen([str(self.receiver)],env=env)
            listener.bind_pid(child.pid)
            try:
                with listener.accept(timeout=3) as conn:
                    conn.settimeout(3);read_bridge_ready(conn);events=[]
                    for _ in range(5):
                        message=read_message_event(conn);events.append(message);acknowledge_text_event(conn,message["sequence"])
                    self.assertEqual([m["type"] for m in events],[1,3,49,1,3])
                    self.assertTrue(all(m["sender"]==peer for m in events))
                    self.assertIn("fixture-image-second-round",events[-1]["text"])
                    self.assertNotIn("wrong-peer",str(events))
                    self.assertNotIn("quota-",str(events))
                self.assertEqual(child.wait(timeout=3),0)
            finally:
                if child.poll() is None:child.kill();child.wait()

    def test_panel_mode_monitors_all_supported_types_without_test_arm_or_quota(self):
        with LocalEventListener() as listener:
            env=os.environ.copy()
            for key in ('HELPER_LINUX_TEST_MARKERS','HELPER_LINUX_TEST_MEDIA','HELPER_LINUX_TEST_VOICE'):
                env.pop(key,None)
            env.update(LD_PRELOAD=str(self.receiver_bridge),HELPER_LINUX_EVENT_SOCKET=str(listener.path),
                HELPER_LINUX_CONTROLLER_PID=str(os.getpid()),HELPER_LINUX_PANEL_MEDIA='1')
            child=subprocess.Popen([str(self.receiver)],env=env);listener.bind_pid(child.pid)
            try:
                with listener.accept(timeout=3) as conn:
                    conn.settimeout(3);read_bridge_ready(conn);events=[]
                    for _ in range(15):
                        event=read_message_event(conn);events.append(event);acknowledge_text_event(conn,event['sequence'])
                    self.assertEqual(len(events),15)
                    self.assertEqual({e['type'] for e in events},{1,3,34,49})
                    self.assertEqual(events[0]['type'],3)
                    self.assertEqual(sum(e['type']==34 for e in events),3)
                self.assertEqual(child.wait(timeout=3),0)
            finally:
                if child.poll() is None:child.kill();child.wait()

    def test_media_sampling_is_disabled_by_default(self):
        with LocalEventListener() as listener:
            env=os.environ.copy();env.update(LD_PRELOAD=str(self.receiver_bridge),HELPER_LINUX_EVENT_SOCKET=str(listener.path),
                HELPER_LINUX_CONTROLLER_PID=str(os.getpid()),HELPER_LINUX_TEST_MARKERS="1")
            env.pop("HELPER_LINUX_TEST_MEDIA",None)
            child=subprocess.Popen([str(self.receiver)],env=env);listener.bind_pid(child.pid)
            try:
                with listener.accept(timeout=3) as conn:
                    conn.settimeout(.3);read_bridge_ready(conn)
                    with self.assertRaises(socket.timeout):read_message_event(conn)
                self.assertEqual(child.wait(timeout=3),0)
            finally:
                if child.poll() is None:child.kill();child.wait()

    def test_profile_and_native_send_reject_a_changed_account(self):
        from wxbot_framework.linux_sender import LinuxSendError
        profile=self.work/'fixture-profile.txt';profile.write_text('wxid_fixture_a')
        sender=LinuxNativeSender();env=os.environ.copy();env.update(sender.environment(),LD_PRELOAD=str(self.sender_bridge),HELPER_FIXTURE_PROFILE_FILE=str(profile))
        child=subprocess.Popen([str(self.sender)],env=env);sender.bind_pid(child.pid)
        try:
            sender.wait_ready(process=child)
            self.assertEqual(sender.current_profile()['wxid'],'wxid_fixture_a')
            self.assertTrue(sender.bind_account('wxid_fixture_a'))
            self.assertTrue(sender.send_text('filehelper','fixture hello'))
            profile.write_text('wxid_fixture_b')
            self.assertEqual(sender.current_profile()['wxid'],'wxid_fixture_b')
            with self.assertRaises(LinuxSendError):sender.send_text('filehelper','stale account')
            self.assertTrue(sender.bind_account('wxid_fixture_b'))
            self.assertTrue(sender.send_text('filehelper','new account'))
            profile.write_text('')
            self.assertFalse(sender.current_profile()['logged_in'])
            with self.assertRaises(LinuxSendError):sender.send_text('filehelper','logged out')
        finally:
            sender.close()
            if child.poll() is None:child.terminate();child.wait(timeout=3)

    def test_message_download_uses_scoped_retained_context_and_coroutine(self):
        self.check_message_download_scope("wxid_friend_fixture")

    def test_message_download_accepts_legacy_personal_identifier(self):
        self.check_message_download_scope("legacy.friend-01")

    def check_message_download_scope(self, peer):
        sender=LinuxNativeSender();env=os.environ.copy();env.update(sender.environment(),LD_PRELOAD=str(self.sender_bridge))
        profile=self.work/'image-profile.txt';profile.write_text('wxid_self_fixture');env['HELPER_FIXTURE_PROFILE_FILE']=str(profile)
        child=subprocess.Popen([str(self.sender)],env=env);sender.bind_pid(child.pid)
        try:
            sender.wait_ready(process=child)
            self.assertIn("image_context",sender.features)
            self.assertFalse(sender.image_context_available("a"*32))
            for invalid in ("filehelper", "gh_fixture", "123@chatroom", "weixin", "bad peer"):
                with self.assertRaises(LinuxSendError):
                    sender._result(sender._request(b"A", (invalid + "\nwxid_self_fixture").encode()))
            with self.assertRaises(LinuxSendError):sender.bind_image_scope(peer,"wxid_wrong_account")
            self.assertTrue(sender.bind_image_scope(peer,"wxid_self_fixture"))
            self.assertTrue(sender.image_context_available("a"*32))
            self.assertFalse(sender.image_context_available("b"*32))
            self.assertFalse(sender.image_context_available("c"*32))
            self.assertTrue(sender.download_message_image("a"*32))
            with self.assertRaises(LinuxSendError):sender.download_message_image("b"*32)
            with self.assertRaises(ValueError):sender.image_context_available("not-a-checksum")
            self.assertIsNone(child.poll())
        finally:
            sender.close()
            if child.poll() is None:child.terminate();child.wait(timeout=3)

    def test_completed_late_callback_does_not_leave_sender_permanently_busy(self):
        import time
        from wxbot_framework.linux_sender import LinuxSendIndeterminate
        bridge=self.work/"late-callback.so"
        subprocess.run(["gcc","-O2","-fPIC","-shared","-pthread","-DSDK_FIXTURE","-DHELPER_FIXTURE_WAIT_MS=20",
                        str(ROOT/"native/linux/send_bridge.c"),"-ldl","-o",str(bridge)],check=True)
        sender=LinuxNativeSender();env=os.environ.copy();env.update(sender.environment(),LD_PRELOAD=str(bridge))
        child=subprocess.Popen([str(self.sender)],env=env);sender.bind_pid(child.pid)
        try:
            sender.wait_ready(process=child)
            with self.assertRaises(LinuxSendIndeterminate):sender.send_text("filehelper","LATE_CALLBACK_FIXTURE")
            time.sleep(.25)
            self.assertTrue(sender.ensure_context())
            self.assertIsNone(child.poll())
        finally:
            sender.close()
            if child.poll() is None:child.terminate();child.wait(timeout=3)

    def test_voice_context_requires_explicit_scope_and_download_uses_native_coroutine(self):
        sender=LinuxNativeSender();env=os.environ.copy();env.update(sender.environment(),LD_PRELOAD=str(self.sender_bridge))
        profile=self.work/'voice-profile.txt';profile.write_text('wxid_self_fixture');env['HELPER_FIXTURE_PROFILE_FILE']=str(profile)
        child=subprocess.Popen([str(self.sender)],env=env);sender.bind_pid(child.pid)
        try:
            sender.wait_ready(process=child)
            self.assertFalse(sender.voice_test_context_available())
            with self.assertRaises(LinuxSendError):sender.download_test_voice()
            sender.bind_voice_test_scope("wxid_friend_fixture","wxid_self_fixture")
            self.assertTrue(sender.voice_test_context_available())
            self.assertTrue(sender.download_test_voice())
            sender.bind_image_scope("wxid_friend_fixture","wxid_self_fixture")
            self.assertFalse(sender.voice_test_context_available())
            self.assertIsNone(child.poll())
        finally:
            sender.close()
            if child.poll() is None:child.terminate();child.wait(timeout=3)

    def test_voice_sampling_requires_flag_and_is_limited_to_first_scoped_voice(self):
        with LocalEventListener() as listener:
            env=os.environ.copy();env.update(LD_PRELOAD=str(self.receiver_bridge),HELPER_LINUX_EVENT_SOCKET=str(listener.path),
                HELPER_LINUX_CONTROLLER_PID=str(os.getpid()),HELPER_LINUX_TEST_MARKERS="1",
                HELPER_LINUX_TEST_MEDIA="1",HELPER_LINUX_TEST_VOICE="1",HELPER_LINUX_MEDIA_ARM_TEXT="WX_LINUX_MEDIA_ARM_11")
            child=subprocess.Popen([str(self.receiver)],env=env);listener.bind_pid(child.pid)
            try:
                with listener.accept(timeout=3) as conn:
                    conn.settimeout(3);read_bridge_ready(conn);events=[]
                    for _ in range(6):
                        event=read_message_event(conn);events.append(event);acknowledge_text_event(conn,event["sequence"])
                    self.assertEqual([e["type"] for e in events],[1,3,49,34,1,3])
                    self.assertTrue(all(e["sender"]=="wxid_friend_fixture" for e in events))
                    self.assertIn('voicelength="2000"',events[3]["text"])
                self.assertEqual(child.wait(timeout=3),0)
            finally:
                if child.poll() is None:child.kill();child.wait()

    def test_local_media_paths_and_generic_messages_use_native_coroutine(self):
        sender=LinuxNativeSender();env=os.environ.copy();env.update(sender.environment(),LD_PRELOAD=str(self.sender_bridge))
        child=subprocess.Popen([str(self.sender)],env=env);sender.bind_pid(child.pid)
        try:
            sender.wait_ready(process=child)
            self.assertIn("local_media",sender.features)
            path=self.work/"含空格 image.png";path.write_bytes(b"fixture-image")
            self.assertTrue(sender.send_image("filehelper",path))
            self.assertTrue(sender.send_file("filehelper",path))
            result=sender.send_generic({"chat_id":"filehelper","message_type":3,"content_xml":"<msg/>"})
            self.assertTrue(result["success"])
            with self.assertRaises(LinuxSendError):sender.send_file("wxid_unlisted",path)
            with self.assertRaises(FileNotFoundError):sender.send_file("filehelper",self.work/"missing")
            empty=self.work/"empty";empty.touch()
            with self.assertRaises(ValueError):sender.send_file("filehelper",empty)
            response=sender._request(b'I',b'filehelper\n00')
            with self.assertRaises(LinuxSendError):sender._result(response,sending=True)
            self.assertIsNone(child.poll())
        finally:
            sender.close()
            if child.poll() is None:child.terminate();child.wait(timeout=3)
            sender.cleanup_retained_files()


if __name__=="__main__":unittest.main()
