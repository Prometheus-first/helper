import os
import socket
import struct
import threading
import unittest

from wxbot_framework.linux_messages import LinuxTextAdapter
from wxbot_framework.linux_wire import LocalEventListener, LinuxWireError, encode_text_event, read_bridge_ready, read_text_event


class LinuxWireTests(unittest.TestCase):
    def test_ready_requires_complete_valid_handshake(self):
        for payload, error in ((b"NOPE", LinuxWireError), (b"HL", EOFError)):
            with self.subTest(payload=payload):
                left, right = socket.socketpair()
                try:
                    right.sendall(payload)
                    right.shutdown(socket.SHUT_WR)
                    with self.assertRaises(error):
                        read_bridge_ready(left)
                finally:
                    left.close()
                    right.close()
        left, right = socket.socketpair()
        try:
            right.sendall(b"HL")
            right.sendall(b"R1")
            read_bridge_ready(left)
        finally:
            left.close()
            right.close()

    def test_fragmented_long_unicode_frame_reaches_router_without_truncation(self):
        left, right = socket.socketpair()
        text = "长消息🙂\n" * 30000 + "末尾"
        frame = encode_text_event("wxid_friend_fixture", "wxid_self_fixture", text, sequence=42)
        def send_fragments():
            try:
                for pos in range(0, len(frame), 997):
                    right.sendall(frame[pos:pos + 997])
            finally:
                right.close()
        thread = threading.Thread(target=send_fragments)
        thread.start()
        try:
            event = read_text_event(left)
            msg = LinuxTextAdapter("wxid_self_fixture", "s").convert(event)
            self.assertEqual(msg.text, text)
            self.assertEqual(msg.sequence, 42)
        finally:
            left.close()
            thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_invalid_header_truncated_frame_and_utf8_are_rejected(self):
        for payload, error in [
            (struct.pack("!4sQIII", b"NOPE", 1, 1, 1, 1), LinuxWireError),
            (struct.pack("!4sQIII", b"HLT1", 0, 1, 1, 1), LinuxWireError),
            (struct.pack("!4sQIII", b"HLT1", 1, 1, 1, 2) + b"abx", EOFError),
            (struct.pack("!4sQIII", b"HLT1", 1, 1, 1, 1) + b"ab\xff", LinuxWireError),
        ]:
            with self.subTest(payload=payload):
                left, right = socket.socketpair()
                try:
                    right.sendall(payload)
                    right.shutdown(socket.SHUT_WR)
                    with self.assertRaises(error):
                        read_text_event(left)
                finally:
                    left.close()
                    right.close()

    @unittest.skipUnless(hasattr(socket, "SO_PEERCRED"), "Linux-only peer credentials")
    def test_listener_permissions_peer_identity_and_cleanup(self):
        with LocalEventListener(os.getpid()) as listener:
            path = listener.path
            directory = path.parent
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            client.connect(os.fspath(path))
            accepted = listener.accept(timeout=1)
            client.close()
            accepted.close()
        self.assertFalse(path.exists())
        self.assertFalse(directory.exists())

    @unittest.skipUnless(hasattr(socket, "SO_PEERCRED"), "Linux-only peer credentials")
    def test_listener_rejects_other_process_id(self):
        with LocalEventListener(os.getpid() + 100000) as listener:
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                client.connect(os.fspath(listener.path))
                with self.assertRaises(PermissionError):
                    listener.accept(timeout=1)
            finally:
                client.close()

    @unittest.skipUnless(hasattr(socket, "SO_PEERCRED"), "Linux-only peer credentials")
    def test_late_pid_binding_fails_closed_and_cannot_switch(self):
        with LocalEventListener() as listener:
            with self.assertRaises(RuntimeError):
                listener.accept(timeout=0)
            listener.bind_pid(os.getpid())
            listener.bind_pid(os.getpid())
            with self.assertRaises(RuntimeError):
                listener.bind_pid(os.getpid() + 1)
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                client.connect(os.fspath(listener.path))
                accepted = listener.accept(timeout=1)
                accepted.close()
            finally:
                client.close()
    def test_encoder_rejects_invalid_inputs(self):
        for args in [("", "wxid_self", "hi"), ("wxid_friend", "wxid_self", ""), ("wxid_friend", "wxid_self", b"hi")]:
            with self.subTest(args=args), self.assertRaises(LinuxWireError):
                encode_text_event(*args)
        with self.assertRaises(LinuxWireError):
            encode_text_event("wxid_friend", "wxid_self", "hi", sequence=True)
        with self.assertRaises(LinuxWireError):
            encode_text_event("wxid_friend", "wxid_self", "hi", sequence=0)


if __name__ == "__main__":
    unittest.main()
