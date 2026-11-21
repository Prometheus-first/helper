"""AI process protocol boundaries; no models, camera, GPU or WeChat required."""
import io
import struct
import unittest
import subprocess
import sys
import json
import queue
import threading
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wxbot_framework.facefusion_stream import FRAME_HEADER, WorkerJob, read_frame


def packet(*, magic=b'FFV1', width=16, height=16, size=384, jpeg_size=0, sequence=1, captured=100):
    return FRAME_HEADER.pack(magic, width, height, size, jpeg_size, sequence, captured, 20000)


class FaceFusionProtocolTests(unittest.TestCase):
    def test_complete_frame_and_optional_preview_are_delivered_once(self):
        stream = io.BytesIO(packet(jpeg_size=4) + b'Y' * 384 + b'JPEG')
        frame = read_frame(stream, 16, 16, 0)
        self.assertEqual((frame.width, frame.height, frame.sequence), (16, 16, 1))
        self.assertEqual(frame.pixels, b'Y' * 384)
        self.assertEqual(frame.preview_jpeg, b'JPEG')
        self.assertIsNone(read_frame(stream, 16, 16, 1))

    def test_truncated_header_or_payload_is_never_delivered(self):
        for body in (packet()[:5], packet() + b'Y' * 383, packet(jpeg_size=4) + b'Y' * 384):
            with self.subTest(length=len(body)), self.assertRaises(RuntimeError):
                read_frame(io.BytesIO(body), 16, 16, 0)

    def test_oversize_wrong_dimensions_and_stale_sequence_fail_before_payload_read(self):
        class HeaderOnly:
            def __init__(self, header):
                self.header = header
            def read(self, size):
                if self.header is None:
                    raise AssertionError('invalid header must not trigger a payload read')
                header, self.header = self.header, None
                return header
        for options in ({'magic': b'BAD!'}, {'size': 0xffffffff}, {'width': 1920},
                        {'jpeg_size': 524289}, {'sequence': 0}, {'captured': 0}):
            with self.subTest(options=options), self.assertRaises(RuntimeError):
                read_frame(HeaderOnly(packet(**options)), 16, 16, 0)

    def test_partial_pipe_reads_are_joined_without_assuming_a_frame_per_read(self):
        class SlowPipe(io.BytesIO):
            def read(self, size):
                return super().read(min(7, size))
        frame = read_frame(SlowPipe(packet() + b'Y' * 384), 16, 16, 0)
        self.assertEqual(len(frame.pixels), 384)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows worker lifetime')
    def test_closing_the_owned_job_terminates_the_worker(self):
        process = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(30)'],
            creationflags=subprocess.CREATE_NO_WINDOW)
        job = None
        try:
            job = WorkerJob(process)
            self.assertIsNone(process.poll())
            job.close()
            self.assertIsNotNone(process.wait(timeout=3))
            job.close()
        finally:
            if job is not None:
                job.close()
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()


class SourceSwitchTests(unittest.TestCase):
    def test_photo_switch_reuses_processor_and_invalid_photo_restores_source(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'native'))
        from facefusion_worker import SourceCommands
        for accepted in (True, False):
            with self.subTest(accepted=accepted), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'source.jpg'; path.touch()
                settings = {'source_paths':['old.jpg']}
                state = SimpleNamespace(get_item=settings.get, set_item=lambda key,value:settings.update({key:value}))
                store = SimpleNamespace(clear_faces=Mock())
                old, new = [object()], [object()]
                vision = SimpleNamespace(read_static_images=Mock(return_value=new), read_static_image=SimpleNamespace(cache_clear=Mock()))
                swapper = SimpleNamespace(pre_process=Mock(return_value=accepted))
                modules = {'facefusion':SimpleNamespace(state_manager=state,face_store=store),
                    'facefusion.vision':vision,'facefusion.content_analyser':SimpleNamespace(analyse_image=lambda path:False),
                    'facefusion.processors.modules.face_swapper':SimpleNamespace(core=swapper)}
                commands = SourceCommands.__new__(SourceCommands); commands.pending = queue.Queue(1)
                commands.pending.put({'path':str(path),'request_id':'a'*32})
                with patch.dict(sys.modules, modules), patch('facefusion_worker.status') as status:
                    result = commands.apply(old)
                self.assertIs(result, new if accepted else old)
                self.assertEqual(settings['source_paths'], [str(path.resolve())] if accepted else ['old.jpg'])
                self.assertEqual(status.call_args.args[0], 'source_changed' if accepted else 'source_change_failed')

    def test_source_acknowledgement_only_updates_matching_request(self):
        from wxbot_framework.facefusion_stream import FaceFusionStream
        stream = FaceFusionStream.__new__(FaceFusionStream)
        stream._stop = threading.Event(); stream._command_lock = threading.Lock()
        stream.source_request_id, stream.source_status, stream.source_error = 'current', 'pending', ''
        stream.phase = 'streaming'; stream.error = None
        replies = [{'phase':'source_change_failed','request_id':'old'}, {'phase':'source_changed','request_id':'current'}]
        stream._process = SimpleNamespace(stderr=io.BytesIO(('\n'.join(map(json.dumps,replies))+'\n').encode()))
        stream._read_status()
        self.assertEqual(stream.source_status, 'applied')
        self.assertEqual(stream.phase, 'streaming')
        self.assertIsNone(stream.error)
