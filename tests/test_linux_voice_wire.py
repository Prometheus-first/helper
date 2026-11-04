import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import unittest

from wxbot_framework.linux_wire import (LinuxWireError, LocalEventListener,
    read_bridge_ready, read_message_event, acknowledge_text_event)

ROOT = Path(__file__).resolve().parents[1]

class VoiceWireTests(unittest.TestCase):
    def test_oversize_and_wrong_type_rejected_before_payload_read(self):
        for kind, audio, xml, sender in ((34,262145,1,1),(1,15,1,1),(34,15,16385,1),(34,15,1,128)):
            left,right=socket.socketpair()
            try:
                right.sendall(struct.pack('!4sQIIIIQI',b'HLV1',1,sender,1,xml,kind,5,audio))
                with self.assertRaises(LinuxWireError):read_message_event(left)
            finally:left.close();right.close()

@unittest.skipUnless(os.name=='posix' and shutil.which('gcc'),'requires Linux and GCC')
class NativeVoiceWireTests(unittest.TestCase):
    def test_owned_binary_copy_survives_source_release_and_invalid_buffers_degrade(self):
        with tempfile.TemporaryDirectory(prefix='helper-native-voice-') as tmp:
            root=Path(tmp);fixture=root/'fixture';bridge=root/'bridge.so'
            subprocess.run(['gcc','-O2','-rdynamic','-fno-builtin','-DHELPER_FIXTURE_VOICE',
                '-o',str(fixture),str(ROOT/'tests/fixtures/linux_events_fixture.c')],check=True)
            subprocess.run(['gcc','-O2','-Wall','-Wextra','-Werror','-Wno-unused-function','-fPIC','-shared','-pthread',
                '-DHELPER_FIXTURE_TEST','-o',str(bridge),str(ROOT/'native/linux/text_bridge.c'),'-ldl'],check=True)
            with LocalEventListener() as listener:
                env=dict(os.environ,LD_PRELOAD=str(bridge),HELPER_LINUX_EVENT_SOCKET=str(listener.path),
                    HELPER_LINUX_CONTROLLER_PID=str(os.getpid()),HELPER_LINUX_PANEL_MEDIA='1')
                child=subprocess.Popen([str(fixture)],env=env)
                listener.bind_pid(child.pid)
                try:
                    with listener.accept(timeout=4) as conn:
                        conn.settimeout(4);read_bridge_ready(conn);events=[]
                        for _ in range(4):
                            event=read_message_event(conn);events.append(event)
                            acknowledge_text_event(conn,event['sequence'])
                    self.assertEqual(events[0]['voice_bytes'],b'\x02#!SILK_V3\x03\0a\0b')
                    self.assertEqual(events[0]['native_message_id'],'115')
                    self.assertEqual([e['voice_bytes'] for e in events[1:3]],[b'',b''])
                    self.assertEqual(events[-1]['type'],1)
                    self.assertEqual(child.wait(timeout=4),0)
                finally:
                    if child.poll() is None:child.terminate();child.wait(timeout=4)
