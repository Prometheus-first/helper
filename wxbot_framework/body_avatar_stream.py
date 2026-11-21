"""Windows camera / WSL LHM++ avatar with the existing session-owned transport."""
from .facefusion_stream import FaceFusionStream
import re
import threading
import json


class BodyAvatarStream(FaceFusionStream):
    provider = 'LHM++ Body Avatar'
    repository_entry = 'core/models/modeling_humana4o_lrm.py'
    worker_filename = 'body_avatar_worker.py'
    supported_models = ('lhmpp_700m',)

    def __init__(self, *, linux_python_executable, wsl_distribution='Ubuntu', tracking_mode='body', **options):
        if (not isinstance(linux_python_executable,str) or not linux_python_executable.startswith('/') or
                len(linux_python_executable)>1024 or any(ord(c)<32 for c in linux_python_executable) or
                not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',wsl_distribution)):
            raise ValueError('Invalid WSL avatar runtime')
        if tracking_mode not in ('body','head'):
            raise ValueError('Invalid avatar tracking mode')
        self.tracking_mode=tracking_mode
        self._control_lock=threading.Lock()
        if tracking_mode=='head':
            self.provider='LHM++ Head Tracking'
        self._worker_options={'linux_python_executable':linux_python_executable,
                              'wsl_distribution':wsl_distribution,'tracking_mode':tracking_mode}
        super().__init__(model='lhmpp_700m',**options)

    def statistics(self):
        result=super().statistics()
        result['tracking_mode']=self.tracking_mode
        result['body_tracking']=bool(result['faces']) and self.tracking_mode=='body'
        result['head_tracking']=bool(result['faces']) and self.tracking_mode=='head'
        result['tracked_frames']=result['swapped_frames']
        return result

    def calibrate(self):
        with self._control_lock:
            if self._stop.is_set():
                return False
            try:
                self._process.stdin.write((json.dumps({'command':'calibrate'})+'\n').encode())
                self._process.stdin.flush()
            except (BrokenPipeError,OSError,ValueError):
                return False
        return True
