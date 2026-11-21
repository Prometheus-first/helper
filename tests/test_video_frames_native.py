"""Test frame publication and slot ownership in this process only."""
import ctypes as c
from pathlib import Path
import sys
import unittest
import io
import tempfile
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == 'win32', 'Windows shared video frames')
class VideoFrameWriterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / 'native'))

    def test_dimensions_and_incomplete_frames_are_rejected(self):
        from video_source import VideoFrameWriter
        for dimensions in ((15, 16), (17, 16), (1922, 16), (1920, 1920), (True, 16)):
            with self.subTest(dimensions=dimensions), self.assertRaises(ValueError):
                VideoFrameWriter(*dimensions)
        writer = VideoFrameWriter(16, 16)
        try:
            with self.assertRaises(ValueError):
                writer.submit(b'incomplete')
        finally:
            writer.close()
            writer.close()

    def test_complete_publication_and_reading_slots_are_never_overwritten(self):
        from video_source import VideoFrameWriter
        writer = VideoFrameWriter(16, 16)
        try:
            payload = bytes(range(256)) + bytes(range(128))
            self.assertTrue(writer.submit(payload))
            state = c.c_int32.from_address(writer.address + 128)
            self.assertEqual(state.value, 2)
            self.assertEqual(writer.mapping[192:192 + len(payload)], payload)
            self.assertGreater(writer.header.heartbeat, 0)
            # Simulate all consumers holding a slot. A producer must drop,
            # leaving every byte in those slots unchanged.
            states = [c.c_int32.from_address(writer.address + 128 + i * writer.slot_bytes) for i in range(3)]
            for state in states:
                state.value = 3
            before = writer.mapping[:]
            self.assertFalse(writer.submit(b'X' * writer.payload_bytes))
            self.assertEqual(writer.mapping[:], before)
            for state in states:
                state.value = 0
            self.assertTrue(writer.submit(b'Y' * writer.payload_bytes))
        finally:
            writer.close()

    def test_mutated_mapping_layout_and_closed_source_are_rejected(self):
        from video_source import VideoFrameWriter
        writer = VideoFrameWriter(16, 16)
        try:
            writer.header.width = 0xffffffff
            with self.assertRaises(RuntimeError):
                writer.submit(b'Z' * writer.payload_bytes)
            self.assertEqual(c.c_int32.from_address(writer.address + 128).value, 0)
            writer.header.width = 16
            writer.disarm()
            with self.assertRaises(RuntimeError):
                writer.submit(b'Z' * writer.payload_bytes)
        finally:
            writer.close()

    def test_decoder_eof_and_partial_frame_never_leave_a_stale_active_source(self):
        from video_source import VideoFrameWriter, VideoFilePlayback
        for partial in (False, True):
            with self.subTest(partial=partial), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'local.mp4'
                path.write_bytes(b'local test placeholder; decoder is simulated')
                writer = VideoFrameWriter(16, 16)
                process = Mock()
                process.stdout = io.BytesIO(b'Y' * (writer.payload_bytes - int(partial)))
                process.poll.return_value = 0
                process.wait.return_value = 0
                try:
                    with patch('video_source.shutil.which', return_value='ffmpeg'), \
                            patch('video_source.subprocess.Popen', return_value=process):
                        playback = VideoFilePlayback(path, writer, fps=30)
                        playback.start()
                        playback._thread.join(2)
                        self.assertFalse(playback._thread.is_alive())
                        self.assertEqual(playback.frames, 0 if partial else 1)
                        self.assertEqual(playback.error is not None, partial)
                        self.assertEqual(writer.header.closed, 1)
                        playback.stop()
                finally:
                    writer.close()

    def test_capture_reader_selects_latest_complete_frame_and_consumes_it_once(self):
        from video_source import VideoFrameWriter
        from video_capture import ReadControl
        writer = VideoFrameWriter(16, 16)
        try:
            # Hold the first two slots so three distinct sequences coexist.
            first = c.c_int32.from_address(writer.address + 128)
            second = c.c_int32.from_address(writer.address + 128 + writer.slot_bytes)
            self.assertTrue(writer.submit(b'A' * writer.payload_bytes))
            first.value = 3
            self.assertTrue(writer.submit(b'B' * writer.payload_bytes))
            second.value = 3
            self.assertTrue(writer.submit(b'C' * writer.payload_bytes))
            first.value = second.value = 2
            storage = c.create_string_buffer(writer.payload_bytes)
            control = ReadControl(size=c.sizeof(ReadControl), abi=1, mapping=writer.address,
                mapping_bytes=writer.mapping_bytes, pixels=c.addressof(storage), pixel_bytes=writer.payload_bytes)
            read = writer._bridge.WxReadVideoFrame
            read.argtypes, read.restype = [c.c_void_p], c.c_uint32
            self.assertEqual(read(c.byref(control)), 0)
            self.assertEqual(control.sequence, 3)
            self.assertGreater(control.captured_tick, 0)
            self.assertEqual(storage.raw, b'C' * writer.payload_bytes)
            self.assertEqual(read(c.byref(control)), 259)
            writer.disarm()
            self.assertEqual(read(c.byref(control)), 13)
        finally:
            writer.close()

    def test_capture_reader_never_reads_an_owned_or_expired_slot(self):
        from video_source import VideoFrameWriter
        from video_capture import ReadControl
        writer = VideoFrameWriter(16, 16)
        try:
            self.assertTrue(writer.submit(b'A' * writer.payload_bytes))
            storage = c.create_string_buffer(b'Z' * writer.payload_bytes)
            control = ReadControl(size=56, abi=1, mapping=writer.address,
                mapping_bytes=writer.mapping_bytes, pixels=c.addressof(storage), pixel_bytes=writer.payload_bytes)
            read = writer._bridge.WxReadVideoFrame
            read.argtypes, read.restype = [c.c_void_p], c.c_uint32
            slot_state = c.c_int32.from_address(writer.address + 128)
            slot_state.value = 1
            self.assertEqual(read(c.byref(control)), 259)
            self.assertEqual(storage.raw[:-1], b'Z' * writer.payload_bytes)
            slot_state.value = 2
            c.c_uint64.from_address(writer.address + 128 + 16).value = 1
            self.assertEqual(read(c.byref(control)), 259)
            self.assertEqual(slot_state.value, 0)
            control.pixel_bytes -= 1
            self.assertEqual(read(c.byref(control)), 87)
        finally:
            writer.close()

    def test_facefusion_call_binds_capture_and_releases_it_after_worker_exit(self):
        from text_service import NativeTextService
        service = NativeTextService(pid=123)
        service.router.set_account('wxid_test_video')
        writer, capture, worker = Mock(), Mock(), Mock()
        capture.capture_descriptor.return_value = {'local': 'test capture descriptor'}
        service.start_call_video_frames = Mock(return_value=writer)
        events = Mock()
        events.attach_mock(worker.stop, 'worker_stop')
        events.attach_mock(capture.close, 'capture_close')
        with patch('video_source.VideoFrameWriter', return_value=capture), \
                patch('video_source.bind_video_capture') as bind, \
                patch('video_source.unbind_video_capture') as unbind, \
                patch('wxbot_framework.facefusion_stream.FaceFusionStream', return_value=worker) as stream:
            events.attach_mock(unbind, 'unbind')
            service.start_call_video_facefusion('source.png', python_executable='python.exe',
                repository='facefusion', width=720, height=1280)
            bind.assert_called_once_with(123, capture, 'wxid_test_video')
            self.assertEqual(stream.call_args.kwargs['input_capture'], capture.capture_descriptor.return_value)
            service.stop_call_video()
            self.assertEqual([call[0] for call in events.mock_calls], ['worker_stop', 'unbind', 'capture_close'])
            self.assertIsNone(service._call_video_capture)

    def test_failed_capture_binding_closes_mapping_without_starting_ai(self):
        from text_service import NativeTextService
        service = NativeTextService(pid=123)
        service.start_call_video_frames = Mock()
        capture = Mock()
        with patch('video_source.VideoFrameWriter', return_value=capture), \
                patch('video_source.bind_video_capture', side_effect=RuntimeError('account changed')), \
                patch('video_source.unbind_video_capture') as unbind, \
                patch('wxbot_framework.facefusion_stream.FaceFusionStream') as stream:
            with self.assertRaisesRegex(RuntimeError, 'account changed'):
                service.start_call_video_facefusion('source.png', python_executable='python.exe',
                    repository='facefusion', width=720, height=1280)
            stream.assert_not_called()
            unbind.assert_called_once_with(123)
            capture.close.assert_called_once()
            self.assertIsNone(service._call_video_capture)


if __name__ == '__main__':
    unittest.main()
