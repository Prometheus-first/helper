"""Isolated FaceFusion 3.9.2 worker. stdout is a bounded binary frame protocol.

Run with the dedicated FaceFusion Python using -I; helper never imports its
CUDA, NumPy or OpenCV dependencies into the bot process.
"""
from __future__ import annotations

import contextlib
import ctypes
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import threading
import time
import queue

COMMIT = 'decc6c9d8f8e014878624452c6e8fe208726b4ec'
FRAME_HEADER = struct.Struct('<4sIIIIQQI')
DLL_DIRECTORIES = []


def frame_clock_ns():
    kernel = ctypes.WinDLL('kernel32')
    kernel.GetTickCount64.restype = ctypes.c_uint64
    return kernel.GetTickCount64() * 1000000


def status(phase, **values):
    print(json.dumps({'phase': phase, **values}), file=sys.stderr, flush=True)


def resolve_git_executable():
    configured = os.environ.get('WXBOT_GIT_EXECUTABLE', '').strip()
    if configured:
        if Path(configured).is_file():
            return configured
        raise RuntimeError('git_executable_unavailable')
    found = shutil.which('git')
    if found:
        return found
    candidates = []
    if os.name == 'nt':
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r'SOFTWARE\GitForWindows') as key:
                    directory = winreg.QueryValueEx(key, 'InstallPath')[0]
                    if isinstance(directory, str):
                        candidates.append(Path(directory) / 'cmd/git.exe')
            except OSError:
                pass
    for variable, suffix in (('ProgramFiles', 'Git/cmd/git.exe'), ('LOCALAPPDATA', 'Programs/Git/cmd/git.exe')):
        if os.environ.get(variable):
            candidates.append(Path(os.environ[variable]) / suffix)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError('git_executable_unavailable')


def initialize(config):
    root = Path(config['repository']).resolve(strict=True)
    commit = subprocess.check_output([resolve_git_executable(), '-C', str(root), 'rev-parse', 'HEAD'],
        creationflags=subprocess.CREATE_NO_WINDOW, stderr=subprocess.DEVNULL, text=True).strip()
    if commit != COMMIT:
        raise RuntimeError('unsupported_facefusion_revision')
    os.chdir(root)
    os.environ['OMP_NUM_THREADS'] = '1'
    os.environ['GRADIO_ANALYTICS_ENABLED'] = 'False'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    sys.path.insert(0, str(root))
    import imageio_ffmpeg
    binary_directory = root / '.assets' / 'bin'
    binary_directory.mkdir(parents=True, exist_ok=True)
    ffmpeg = binary_directory / 'ffmpeg.exe'
    if not ffmpeg.exists():
        shutil.copyfile(imageio_ffmpeg.get_ffmpeg_exe(), ffmpeg)
    os.environ['PATH'] = str(binary_directory) + os.pathsep + os.environ.get('PATH', '')
    nvidia_root = Path(sys.prefix) / 'Lib' / 'site-packages' / 'nvidia'
    directories = [path for path in nvidia_root.glob('*/bin') if path.is_dir()]
    DLL_DIRECTORIES.extend(os.add_dll_directory(str(path)) for path in directories)
    os.environ['PATH'] = os.pathsep.join(map(str, directories)) + os.pathsep + os.environ['PATH']
    import onnxruntime
    onnxruntime.preload_dlls(directory='')
    if 'CUDAExecutionProvider' not in onnxruntime.get_available_providers():
        raise RuntimeError('cuda_provider_unavailable')
    from facefusion import core, logger, metadata, state_manager
    from facefusion.args import apply_args
    from facefusion.program import create_program
    from facefusion.processors.modules.face_swapper import core as swapper
    from facefusion.vision import read_static_images
    if metadata.get('version') != '3.9.2':
        raise RuntimeError('unsupported_facefusion_version')
    model = config.get('model', 'inswapper_128_fp16')
    if model not in ('inswapper_128_fp16', 'hyperswap_1a_256', 'hyperswap_1b_256', 'hyperswap_1c_256'):
        raise RuntimeError('unsupported_face_swapper_model')
    processors = ['face_swapper', 'face_enhancer'] if config.get('enhance') else ['face_swapper']
    arguments = ['run', '--processors', *processors, '-s', config['source_photo'],
        '--face-swapper-model', model, '--face-swapper-pixel-boost',
        '128x128' if model == 'inswapper_128_fp16' else '256x256',
        '--face-enhancer-model', 'gpen_bfr_256', '--face-enhancer-blend', '60',
        '--face-detector-model', 'yolo_face', '--face-detector-size', '640x640',
        '--face-selector-mode', 'one', '--face-selector-order', 'large-small',
        '--face-mask-types', 'box', '--execution-providers', 'cuda',
        '--execution-thread-count', '1', '--video-memory-strategy', 'tolerant', '--log-level', 'error']
    # Upstream configures model-dependent choices while constructing its parser.
    previous_argv = sys.argv
    try:
        sys.argv = [previous_argv[0], *arguments]
        args = vars(create_program().parse_args(arguments))
    finally:
        sys.argv = previous_argv
    apply_args(args, state_manager.init_item)
    logger.init('error')
    status('checking_models')
    if not core.common_pre_check() or not swapper.pre_check():
        raise RuntimeError('model_validation_failed')
    if config.get('enhance'):
        from facefusion.processors.modules.face_enhancer import core as enhancer
        if not enhancer.pre_check():
            raise RuntimeError('enhancer_validation_failed')
    if not swapper.pre_process('stream'):
        raise RuntimeError('source_face_not_detected')
    source = read_static_images([config['source_photo']])
    from facefusion.content_analyser import analyse_image
    if analyse_image(config['source_photo']):
        raise RuntimeError('source_content_rejected')
    from facefusion.streamer import process_stream_frame
    # Warm the actual swapper and verify CUDA sessions rather than accepting
    # an installed-provider list as evidence that inference uses the GPU.
    status('warming_models')
    process_stream_frame(source, source[0])
    pool = swapper.get_inference_pool()
    session = pool.get('face_swapper')
    if session is None or session.get_providers()[0] != 'CUDAExecutionProvider':
        raise RuntimeError('swapper_fell_back_to_cpu')
    status('models_ready', provider='CUDAExecutionProvider', model=model)
    return source, process_stream_frame


class CameraFrames:
    """Continuously drain capture; inference reads only the latest frame."""
    def __init__(self, camera_id, cv2):
        self._cv2 = cv2
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._latest = None
        self._sequence = 0
        self.capture = cv2.VideoCapture(camera_id, cv2.CAP_MSMF)
        if not self.capture.isOpened():
            self.capture.release()
            self.capture = cv2.VideoCapture(camera_id, cv2.CAP_DSHOW)
        if not self.capture.isOpened():
            raise RuntimeError('camera_unavailable')
        self.capture.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.capture.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        self.capture.set(cv2.CAP_PROP_FPS, 30)
        self._thread = threading.Thread(target=self._run, daemon=True, name='FaceFusionCamera')
        self._thread.start()

    def _run(self):
        failures = 0
        while not self._stop.is_set():
            ok, frame = self.capture.read()
            timestamp = frame_clock_ns()
            if not ok:
                failures += 1
                if failures >= 30:
                    self._stop.set()
                self._stop.wait(.02)
                continue
            failures = 0
            with self._lock:
                self._sequence += 1
                self._latest = (self._sequence, timestamp, frame)

    def latest(self, after):
        with self._lock:
            if self._latest is not None and self._latest[0] > after:
                return self._latest
        if self._stop.is_set():
            raise RuntimeError('camera_capture_ended')
        return None

    def close(self):
        self._stop.set()
        self.capture.release()
        self._thread.join(2)


class SourceCommands:
    """One pending selection; source changes run on the inference thread."""
    def __init__(self):
        self.pending = queue.Queue(maxsize=1)
        self.video, self.video_request_id = None, ''
        threading.Thread(target=self._read, daemon=True, name='FaceFusionCommands').start()

    def _read(self):
        while True:
            line = sys.stdin.buffer.readline(16385)
            if not line or len(line) > 16384:
                return
            try:
                command = json.loads(line)
                if (command.get('command') not in ('source', 'video') or not isinstance(command.get('path'), str) or
                        not isinstance(command.get('request_id'), str) or len(command['request_id']) != 32):
                    continue
                try:
                    self.pending.get_nowait()
                except queue.Empty:
                    pass
                self.pending.put_nowait(command)
            except (ValueError, AttributeError, queue.Full):
                continue

    def apply(self, source):
        try:
            command = self.pending.get_nowait()
        except queue.Empty:
            return source
        self.finish_video('video_cancelled')
        if command.get('command', 'source') == 'video':
            try:
                import cv2
                from video_clip import VideoClip
                self.video = VideoClip(Path(command['path']).resolve(strict=True), cv2)
                self.video_request_id = command['request_id']
                status('video_started', request_id=command['request_id'])
            except Exception:
                status('video_failed', request_id=command['request_id'])
            return source
        from facefusion import state_manager, face_store
        from facefusion.vision import read_static_images, read_static_image
        from facefusion.content_analyser import analyse_image
        from facefusion.processors.modules.face_swapper import core as swapper
        previous = state_manager.get_item('source_paths')
        try:
            path = Path(command['path']).resolve(strict=True)
            if not path.is_file():
                raise ValueError('missing_source')
            if analyse_image(str(path)):
                raise ValueError('source_content_rejected')
            state_manager.set_item('source_paths', [str(path)])
            face_store.clear_faces()
            read_static_image.cache_clear()
            if not swapper.pre_process('stream'):
                raise ValueError('source_face_not_detected')
            result = read_static_images([str(path)])
            status('source_changed', request_id=command['request_id'])
            return result
        except Exception:
            state_manager.set_item('source_paths', previous)
            face_store.clear_faces()
            read_static_image.cache_clear()
            status('source_change_failed', request_id=command['request_id'])
            return source

    def finish_video(self, phase):
        video = getattr(self, 'video', None)
        if video is not None:
            video.close()
            self.video = None
            status(phase, request_id=self.video_request_id)


def run(config, output):
    source, process_frame = initialize(config)
    if config.get('prepare_only'):
        return
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    commands = SourceCommands()
    import cv2
    from facefusion.content_analyser import analyse_stream
    from facefusion import face_store
    from facefusion.vision import fit_cover_frame
    width, height = config['width'], config['height']
    fps = config.get('fps', 15)
    if (type(width) is not int or type(height) is not int or width % 2 or height % 2 or
            not 16 <= width <= 1920 or not 16 <= height <= 1920 or width * height > 2073600 or
            type(fps) is not int or not 1 <= fps <= 30):
        raise RuntimeError('invalid_video_dimensions')
    if config.get('input_capture') is not None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from video_capture import CapturedFrames
        camera = CapturedFrames(config['input_capture'], cv2)
    else:
        camera = CameraFrames(config.get('camera_id', 0), cv2)
    status('camera_ready')
    last_input = sequence = 0
    swapped_frames = 0
    source_faces = [(image, face_store.get_faces(image)) for image in source]
    deadline = time.monotonic()
    last_report = deadline
    try:
        while True:
            replacement = commands.apply(source)
            if replacement is not source:
                source = replacement
                source_faces = [(image, face_store.get_faces(image)) for image in source]
            started = time.monotonic_ns()
            video_frame = None
            if commands.video is not None:
                try:
                    video_frame = commands.video.next_frame()
                    if video_frame is None:
                        commands.finish_video('video_finished')
                    elif analyse_stream(video_frame, fps):
                        commands.finish_video('video_failed')
                        video_frame = None
                except Exception:
                    commands.finish_video('video_failed')
                    video_frame = None
            if video_frame is not None:
                from video_clip import letterbox
                result = letterbox(video_frame, width, height, cv2)
                captured, face_count = frame_clock_ns(), 0
            else:
                current = camera.latest(last_input)
                if current is None:
                    time.sleep(.005)
                    continue
                last_input, captured, frame = current
                # Native WeChat capture can be 720x1280 although its encoder is
                # much smaller. Avoid warping full-resolution masks every frame.
                edge = max(frame.shape[:2])
                if edge > 640:
                    factor = 640 / edge
                    frame = cv2.resize(frame, (max(1, round(frame.shape[1] * factor)),
                        max(1, round(frame.shape[0] * factor))), interpolation=cv2.INTER_AREA)
                if analyse_stream(frame, fps):
                    raise RuntimeError('stream_content_rejected')
                result = process_frame(source, frame)
                face_count = len(face_store.get_faces(frame) or [])
                if face_count:
                    swapped_frames += 1
            # Upstream's static-image cache would otherwise grow for every
            # live frame. Retain only the fixed source identity between frames.
            face_store.clear_faces()
            for source_image, cached_faces in source_faces:
                if cached_faces:
                    face_store.set_faces(source_image, cached_faces)
            inference_us = min(0xffffffff, (time.monotonic_ns() - started) // 1000)
            result = fit_cover_frame(result, (width, height))
            # A visible test label travels with the pixels to preview and peer.
            scale = max(.45, width / 1000)
            label = 'HELPER VIDEO TEST' if video_frame is not None else 'AI FACE SWAP TEST'
            cv2.putText(result, label, (12, 28), cv2.FONT_HERSHEY_SIMPLEX,
                        scale, (0, 0, 0), 4, cv2.LINE_AA)
            cv2.putText(result, label, (12, 28), cv2.FONT_HERSHEY_SIMPLEX,
                        scale, (0, 255, 255), 1, cv2.LINE_AA)
            pixels = cv2.cvtColor(result, cv2.COLOR_BGR2YUV_I420).tobytes()
            jpeg = b''
            if config.get('preview'):
                encoded, preview = cv2.imencode('.jpg', result, [cv2.IMWRITE_JPEG_QUALITY, 85])
                if encoded and preview.nbytes <= 524288:
                    jpeg = preview.tobytes()
            sequence += 1
            output.write(FRAME_HEADER.pack(b'FFV1', width, height, len(pixels), len(jpeg), sequence, captured, inference_us))
            output.write(pixels)
            output.write(jpeg)
            output.flush()
            now = time.monotonic()
            if now - last_report >= 1:
                position, duration = commands.video.progress() if commands.video is not None else (0, 0)
                status('streaming', frames=sequence, inference_ms=round(inference_us / 1000, 2),
                       faces=face_count, swapped_frames=swapped_frames,
                       video_request_id=commands.video_request_id,
                       video_position_ms=min(2147483647, round(position * 1000)),
                       video_duration_ms=min(2147483647, round(duration * 1000)))
                last_report = now
            deadline += 1 / fps
            time.sleep(max(0, deadline - time.monotonic()))
            if time.monotonic() - deadline > 1:
                deadline = time.monotonic()
    finally:
        commands.finish_video('video_cancelled')
        camera.close()


def main():
    line = sys.stdin.buffer.readline(16385)
    if len(line) > 16384:
        raise RuntimeError('configuration_too_large')
    config = json.loads(line)
    output = sys.stdout.buffer
    with contextlib.redirect_stdout(sys.stderr):
        run(config, output)


if __name__ == '__main__':
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as error:
        reason = str(error) if isinstance(error, RuntimeError) else type(error).__name__
        status('failed', reason=reason[:120])
        raise SystemExit(1)
