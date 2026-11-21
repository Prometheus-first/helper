"""Portrait animation using the same bounded, session-owned AI frame transport."""
from .facefusion_stream import FaceFusionStream
import json
import threading


class LivePortraitStream(FaceFusionStream):
    provider = 'FasterLivePortrait'
    repository_entry = 'src/pipelines/faster_live_portrait_pipeline.py'
    worker_filename = 'liveportrait_worker.py'
    supported_models = ('liveportrait',)

    def __init__(self, **options):
        self._control_lock = threading.Lock()
        super().__init__(model='liveportrait', **options)

    def calibrate(self):
        with self._control_lock:
            if self._stop.is_set():
                return False
            try:
                self._process.stdin.write((json.dumps({'command': 'calibrate'}) + '\n').encode())
                self._process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                return False
        return True
