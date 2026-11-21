"""A pinned, isolated FaceFusion producer for the framework's video-frame API."""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
from dataclasses import dataclass
import json
from pathlib import Path
import struct
import subprocess
import threading
import time
import uuid

FRAME_HEADER = struct.Struct('<4sIIIIQQI')
MAX_PIXELS = 2073600


def frame_clock_ns():
    # Python 3.12 and 3.14 use different Windows monotonic-clock backends.
    # GetTickCount64 provides the same boot-relative epoch to both processes.
    kernel = c.WinDLL('kernel32')
    kernel.GetTickCount64.restype = c.c_uint64
    return kernel.GetTickCount64() * 1000000


@dataclass(frozen=True)
class AIFrame:
    width: int
    height: int
    sequence: int
    captured_ns: int
    inference_us: int
    pixels: bytes
    preview_jpeg: bytes = b''


def read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        block = stream.read(size - len(data))
        if not block:
            if data:
                raise RuntimeError('AI 视频帧传输中断')
            return None
        data.extend(block)
    return bytes(data)


def read_frame(stream, width, height, last_sequence):
    header = read_exact(stream, FRAME_HEADER.size)
    if header is None:
        return None
    magic, actual_width, actual_height, size, jpeg_size, sequence, captured, inference = FRAME_HEADER.unpack(header)
    if (magic != b'FFV1' or actual_width != width or actual_height != height or
            width * height > MAX_PIXELS or size != width * height * 3 // 2 or
            jpeg_size > 524288 or sequence <= last_sequence or not captured):
        raise RuntimeError('AI 视频帧头无效')
    pixels = read_exact(stream, size)
    preview = read_exact(stream, jpeg_size) if jpeg_size else b''
    if pixels is None or preview is None:
        raise RuntimeError('AI 视频帧不完整')
    return AIFrame(width, height, sequence, captured, inference, pixels, preview)


class WorkerJob:
    """Windows kills this worker and its children if the bot exits unexpectedly."""
    def __init__(self, process):
        self._lock = threading.Lock()
        class BasicLimits(c.Structure):
            _fields_ = [('process_time', c.c_int64), ('job_time', c.c_int64), ('flags', w.DWORD),
                ('minimum', c.c_size_t), ('maximum', c.c_size_t), ('process_limit', w.DWORD),
                ('affinity', c.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class Limits(c.Structure):
            _fields_ = [('basic', BasicLimits), ('io', c.c_uint64 * 6),
                ('process_memory', c.c_size_t), ('job_memory', c.c_size_t),
                ('peak_process', c.c_size_t), ('peak_job', c.c_size_t)]
        self.kernel = c.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise c.WinError(c.get_last_error())
        limits = Limits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            if not self.kernel.SetInformationJobObject(self.handle, 9, c.byref(limits), c.sizeof(limits)):
                raise c.WinError(c.get_last_error())
            if not self.kernel.AssignProcessToJobObject(self.handle, w.HANDLE(process._handle)):
                raise c.WinError(c.get_last_error())
        except BaseException:
            self.close()
            raise

    def close(self):
        with self._lock:
            handle, self.handle = self.handle, None
        if handle:
            self.kernel.CloseHandle(handle)


class FaceFusionStream:
    provider = 'FaceFusion'
    repository_entry = 'facefusion.py'
    worker_filename = 'facefusion_worker.py'
    supported_models = ('inswapper_128_fp16', 'hyperswap_1a_256', 'hyperswap_1b_256', 'hyperswap_1c_256')

    def __init__(self, *, python_executable, repository, source_photo, width, height,
                 camera_id=0, fps=15, on_frame=lambda frame: True, on_end=lambda: None,
                 session_alive=lambda: True, preview=False, model='inswapper_128_fp16', enhance=False,
                 input_capture=None):
        if (type(width) is not int or type(height) is not int or width % 2 or height % 2 or
                not 16 <= width <= 1920 or not 16 <= height <= 1920 or width * height > MAX_PIXELS or
                type(fps) is not int or not 1 <= fps <= 30 or type(camera_id) is not int or
                not 0 <= camera_id <= 15 or type(preview) is not bool):
            raise ValueError('AI 视频尺寸、帧率或摄像头编号无效')
        if model not in self.supported_models or type(enhance) is not bool:
            raise ValueError('AI 换脸模型或增强选项无效')
        python_executable = Path(python_executable).resolve(strict=True)
        repository = Path(repository).resolve(strict=True)
        source_photo = Path(source_photo).resolve(strict=True)
        if not python_executable.is_file() or not (repository / self.repository_entry).is_file() or not source_photo.is_file():
            raise ValueError('请提供独立 Python、匹配的 AI 仓库和本地人脸照片')
        self.width, self.height = width, height
        self.frames = self.dropped = 0
        self.faces = self.swapped_frames = 0
        self.latency_ms = self.inference_ms = 0.0
        self.error = None
        self.phase = 'starting'
        self.latest_frame = None
        self._preview = preview
        self._input_source = 'wechat' if input_capture is not None else 'camera'
        self._command_lock = threading.Lock()
        self.source_request_id, self.source_status, self.source_error = '', 'applied', ''
        self.video_request_id, self.video_status = '', 'idle'
        self.video_position_ms = self.video_duration_ms = 0
        self._on_frame, self._on_end, self._session_alive = on_frame, on_end, session_alive
        self._stop = threading.Event()
        self._threads = []
        self._job = None
        worker = Path(__file__).resolve().parents[1] / 'native' / self.worker_filename
        self._process = subprocess.Popen([str(python_executable), '-I', '-B', str(worker)],
            cwd=repository, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            self._job = WorkerJob(self._process)
            config = {'repository': str(repository), 'source_photo': str(source_photo), 'width': width,
                      'height': height, 'fps': fps, 'camera_id': camera_id, 'preview': preview,
                      'model': model, 'enhance': enhance}
            config.update(getattr(self, '_worker_options', {}))
            if input_capture is not None:
                config['input_capture'] = input_capture
            encoded = (json.dumps(config) + '\n').encode('utf8')
            if len(encoded) > 16384:
                raise ValueError('AI 视频配置过长')
            self._process.stdin.write(encoded)
            self._process.stdin.flush()
        except BaseException:
            if self._job is not None:
                self._job.close()
            if self._process.poll() is None:
                self._process.terminate()
            self._process.wait(timeout=5)
            for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
                stream.close()
            raise

    def start(self):
        for target, name in ((self._read_frames, 'FaceFusionFrames'),
                             (self._read_status, 'FaceFusionStatus'), (self._monitor, 'FaceFusionLifetime')):
            thread = threading.Thread(target=target, name=name, daemon=True)
            self._threads.append(thread)
            thread.start()

    def switch_source_photo(self, path):
        if self.provider != 'FaceFusion' or self._stop.is_set():
            raise RuntimeError('当前没有可切换照片的换脸源')
        if self.phase not in ('models_ready', 'camera_ready', 'streaming'):
            raise RuntimeError('模型仍在加载，请就绪后再切换照片')
        path = Path(path).resolve(strict=True)
        if not path.is_file():
            raise ValueError('请选择本地人脸照片')
        identifier = uuid.uuid4().hex
        command = (json.dumps({'command': 'source', 'path': str(path), 'request_id': identifier}) + '\n').encode()
        if len(command) > 16384:
            raise ValueError('照片路径过长')
        with self._command_lock:
            self.source_request_id, self.source_status, self.source_error = identifier, 'pending', ''
            self._process.stdin.write(command)
            self._process.stdin.flush()
        return {'request_id': identifier}

    def play_video_clip(self, path):
        if self.provider != 'FaceFusion' or self._stop.is_set() or self.phase != 'streaming':
            raise RuntimeError('请等待换脸模型就绪后再播放视频')
        path = Path(path).resolve(strict=True)
        if not path.is_file():
            raise ValueError('请选择本地视频')
        identifier = uuid.uuid4().hex
        command = (json.dumps({'command':'video', 'path':str(path), 'request_id':identifier}) + '\n').encode()
        if len(command) > 16384:
            raise ValueError('视频路径过长')
        with self._command_lock:
            self.video_request_id, self.video_status = identifier, 'pending'
            self.video_position_ms = self.video_duration_ms = 0
            self._process.stdin.write(command)
            self._process.stdin.flush()
        return {'request_id': identifier}

    def _read_frames(self):
        sequence = 0
        try:
            while not self._stop.is_set() and self._session_alive():
                frame = read_frame(self._process.stdout, self.width, self.height, sequence)
                if frame is None:
                    if not self._stop.is_set():
                        raise RuntimeError(self.provider + ' 工作进程已结束')
                    break
                sequence = frame.sequence
                age = frame_clock_ns() - frame.captured_ns
                self.latency_ms, self.inference_ms = age / 1000000, frame.inference_us / 1000
                if age < 0 or age > 500000000:
                    self.dropped += 1
                    continue
                if self._stop.is_set() or not self._session_alive():
                    break
                if self._on_frame(frame) is not False:
                    self.frames += 1
                    if self._preview:
                        self.latest_frame = frame
                else:
                    self.dropped += 1
        except Exception as error:
            if not self._stop.is_set() and self.error is None:
                self.error = error
        finally:
            self._stop.set()
            try:
                self._on_end()
            finally:
                if self._job is not None:
                    self._job.close()

    def _read_status(self):
        while True:
            line = self._process.stderr.readline(4097)
            if not line:
                break
            if len(line) > 4096:
                continue
            try:
                data = json.loads(line)
                phase = data.get('phase')
                if phase in ('video_started', 'video_finished', 'video_failed', 'video_cancelled'):
                    with self._command_lock:
                        if data.get('request_id') == self.video_request_id:
                            self.video_status = {'video_started':'playing', 'video_finished':'finished',
                                'video_failed':'failed', 'video_cancelled':'cancelled'}[phase]
                if data.get('video_request_id') and data.get('video_request_id') == self.video_request_id:
                    for name in ('video_position_ms', 'video_duration_ms'):
                        value = data.get(name)
                        if type(value) is int and 0 <= value <= 2147483647:
                            setattr(self, name, value)
                if phase in ('source_changed', 'source_change_failed'):
                    with self._command_lock:
                        if data.get('request_id') == self.source_request_id:
                            self.source_status = 'applied' if phase == 'source_changed' else 'failed'
                            self.source_error = '' if phase == 'source_changed' else '照片未通过人脸或内容检查，已保留原照片'
                if phase in ('checking_models', 'warming_models', 'models_ready', 'camera_ready', 'streaming', 'failed'):
                    self.phase = phase
                if phase == 'failed':
                    reason = str(data.get('reason', 'worker_failed'))[:120]
                    if reason == 'git_executable_unavailable':
                        reason = '找不到 Git，请安装 Git 并加入 PATH，或设置 WXBOT_GIT_EXECUTABLE 后重启管理台'
                    self.error = RuntimeError(self.provider + ': ' + reason)
                for name in ('faces', 'swapped_frames'):
                    value = data.get(name)
                    if type(value) is int and 0 <= value <= 0x7fffffff:
                        setattr(self, name, value)
            except (ValueError, TypeError, AttributeError):
                pass  # Upstream progress and diagnostic lines are not persisted.

    def _monitor(self):
        while not self._stop.wait(.2):
            if not self._session_alive():
                self._stop.set()
                if self._job is not None:
                    self._job.close()
                break

    def statistics(self):
        phase = self.phase if not self._stop.is_set() or self.error is not None else 'stopped'
        return {'phase': phase, 'frames': self.frames, 'dropped': self.dropped,
                'input_source': self._input_source,
                'source_request_id': self.source_request_id, 'source_status': self.source_status,
                'source_error': self.source_error,
                'video_request_id': self.video_request_id, 'video_status': self.video_status,
                'video_position_ms': self.video_position_ms, 'video_duration_ms': self.video_duration_ms,
                'faces': self.faces, 'swapped_frames': self.swapped_frames,
                'latency_ms': round(self.latency_ms, 2), 'inference_ms': round(self.inference_ms, 2),
                'failed': self.error is not None, 'error': str(self.error) if self.error else '',
                'running': not self._stop.is_set()}

    def stop(self):
        self._stop.set()
        if self._job is not None:
            self._job.close()
        if self._process.poll() is None:
            self._process.terminate()
        self._process.wait(timeout=5)
        for thread in self._threads:
            thread.join(timeout=5)
        if any(thread.is_alive() for thread in self._threads):
            raise RuntimeError('AI 视频工作线程尚未结束，保留资源等待清理重试')
        for stream in (self._process.stdin, self._process.stdout, self._process.stderr):
            stream.close()
        self.latest_frame = None
