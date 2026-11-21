"""Pinned FasterLivePortrait worker; webcam and portrait pixels stay in memory."""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

# -I intentionally excludes both the caller's cwd and user site-packages.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from facefusion_worker import CameraFrames, FRAME_HEADER, DLL_DIRECTORIES, status

COMMIT = '548b5ccc7d92abfaaaeceba7ff1828d657856ec5'


def initialize(config):
    root = Path(config['repository']).resolve(strict=True)
    commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'],
        creationflags=subprocess.CREATE_NO_WINDOW, stderr=subprocess.DEVNULL, text=True).strip()
    if commit != COMMIT:
        raise RuntimeError('unsupported_liveportrait_revision')
    os.chdir(root)
    os.environ['OMP_NUM_THREADS'] = '1'
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    sys.path.insert(0, str(root))
    directories = [p for p in (Path(sys.prefix) / 'Lib/site-packages/nvidia').glob('*/bin') if p.is_dir()]
    DLL_DIRECTORIES.extend(os.add_dll_directory(str(p)) for p in directories)
    os.environ['PATH'] = os.pathsep.join(map(str, directories)) + os.pathsep + os.environ.get('PATH', '')
    import torch
    import onnxruntime
    if not torch.cuda.is_available():
        raise RuntimeError('pytorch_cuda_unavailable')
    onnxruntime.preload_dlls(directory='')
    from omegaconf import OmegaConf
    from src.pipelines.faster_live_portrait_pipeline import FasterLivePortraitPipeline
    from src import models
    from liveportrait_gpu import TorchGridWarp

    class HelperWarpingSpadeModel(models.WarpingSpadeModel):
        def __init__(self, **options):
            self.device = torch.cuda.current_device()
            self.predict_type = 'ort'
            self.predictor = TorchGridWarp(options['model_path'])
            self.context = self.inputs = self.outputs = self.tensors = None

    models.HelperWarpingSpadeModel = HelperWarpingSpadeModel
    cfg = OmegaConf.load(root / 'configs/onnx_infer.yaml')
    # The webcam is an uncropped landscape image. Track and align its face
    # before motion extraction; never squeeze the entire webcam into 256x256.
    cfg.infer_params.flag_crop_driving_video = True
    cfg.infer_params.flag_relative_motion = True
    cfg.infer_params.flag_pasteback = True
    cfg.infer_params.flag_stitching = True
    cfg.infer_params.driving_multiplier = .8
    cfg.models.warping_spade.name = 'HelperWarpingSpadeModel'
    status('checking_models')
    pipe = FasterLivePortraitPipeline(cfg=cfg, is_animal=False)
    for model in pipe.model_dict.values():
        predictors = getattr(model, 'predictor', None)
        if predictors is not None and hasattr(predictors, 'onnx_model') and predictors.onnx_model.get_providers()[0] != 'CUDAExecutionProvider':
            raise RuntimeError('portrait_model_fell_back_to_cpu')
    status('warming_models')
    if not pipe.prepare_source(config['source_photo'], realtime=True):
        raise RuntimeError('source_face_not_detected')
    # Keep the image composition fixed. Large relative camera motion bends
    # hair and earrings; limit pose and expression deltas before rendering.
    import numpy as np
    from src.utils.utils import get_rotation_matrix
    original_run = pipe._run
    smooth = {}
    def limited_run(src_info, current, initial, rotation, initial_rotation, realtime,
                    eye_ratio, lip_ratio, background, **options):
        limited = dict(current)
        for name, bound, gain in [('pitch', 16, .5), ('yaw', 24, .5), ('roll', 12, .5), ('exp', .025, .55)]:
            delta = np.clip(current[name] - initial[name], -bound, bound) * gain
            previous = smooth.get(name)
            delta = delta if previous is None else previous * .4 + delta * .6
            smooth[name] = delta
            limited[name] = initial[name] + delta
        limited['t'] = initial['t'].copy()
        limited['scale'] = initial['scale'].copy()
        rotation = get_rotation_matrix(limited['pitch'], limited['yaw'], limited['roll'])
        return original_run(src_info, limited, initial, rotation, initial_rotation, False,
                            eye_ratio, lip_ratio, background, **options)
    pipe._run = limited_run
    pipe.helper_motion_smoothing = smooth
    status('models_ready')
    return pipe


def run(config, output):
    import cv2
    pipe = initialize(config)
    if config.get('prepare_only'):
        return
    width, height, fps = config['width'], config['height'], config.get('fps', 15)
    if (type(width) is not int or type(height) is not int or width % 2 or height % 2 or
            not 16 <= width <= 1920 or not 16 <= height <= 1920 or width * height > 2073600 or
            type(fps) is not int or not 1 <= fps <= 30):
        raise RuntimeError('invalid_video_dimensions')
    camera = CameraFrames(config.get('camera_id', 0), cv2)
    status('camera_ready')
    last_input = sequence = animated = 0
    deadline = last_report = time.monotonic()
    next_detection = 0
    has_face = False
    recalibrate = threading.Event()
    def commands():
        import json
        for line in sys.stdin.buffer:
            if len(line) <= 256:
                try:
                    if json.loads(line).get('command') == 'calibrate':
                        recalibrate.set()
                except (ValueError, AttributeError):
                    pass
    threading.Thread(target=commands, name='PortraitControls', daemon=True).start()
    try:
        while True:
            current = camera.latest(last_input)
            if current is None:
                time.sleep(.005)
                continue
            last_input, captured, frame = current
            started = time.monotonic_ns()
            if recalibrate.is_set():
                recalibrate.clear()
                pipe.R_d_0 = None
                pipe.src_lmk_pre = None
                pipe.helper_motion_smoothing.clear()
                next_detection = 0
            if time.monotonic() >= next_detection:
                has_face = bool(pipe.model_dict['face_analysis'].predict(frame))
                next_detection = time.monotonic() + .75
                if not has_face:
                    pipe.src_lmk_pre = None
                    pipe.R_d_0 = None
            result = pipe.src_imgs[0]
            if has_face:
                _, crop, generated, _ = pipe.run(frame, pipe.src_imgs[0], pipe.src_infos[0])
                if generated is not None:
                    result = generated
                    animated += 1
            inference_us = min(0xffffffff, (time.monotonic_ns() - started) // 1000)
            # Letterbox the full source composition instead of cutting off
            # hair and clothing to fit the native portrait video dimensions.
            result = cv2.cvtColor(result, cv2.COLOR_RGB2BGR)
            h, w = result.shape[:2]
            scale = min(width / w, height / h)
            resized = cv2.resize(result, (max(1, round(w * scale)), max(1, round(h * scale))))
            import numpy as np
            canvas = np.zeros((height, width, 3), dtype=np.uint8)
            y, x = (height - resized.shape[0]) // 2, (width - resized.shape[1]) // 2
            canvas[y:y + resized.shape[0], x:x + resized.shape[1]] = resized
            cv2.putText(canvas, 'AI PORTRAIT TEST', (12, 28), cv2.FONT_HERSHEY_SIMPLEX, .65,
                        (0, 0, 0), 4, cv2.LINE_AA)
            cv2.putText(canvas, 'AI PORTRAIT TEST', (12, 28), cv2.FONT_HERSHEY_SIMPLEX, .65,
                        (0, 255, 255), 1, cv2.LINE_AA)
            pixels = cv2.cvtColor(canvas, cv2.COLOR_BGR2YUV_I420).tobytes()
            jpeg = b''
            if config.get('preview'):
                ok, data = cv2.imencode('.jpg', canvas, [cv2.IMWRITE_JPEG_QUALITY, 90])
                if ok and data.nbytes <= 524288:
                    jpeg = data.tobytes()
            sequence += 1
            output.write(FRAME_HEADER.pack(b'FFV1', width, height, len(pixels), len(jpeg), sequence, captured, inference_us))
            output.write(pixels)
            output.write(jpeg)
            output.flush()
            now = time.monotonic()
            if now - last_report >= 1:
                status('streaming', frames=sequence, faces=int(has_face), swapped_frames=animated)
                last_report = now
            deadline += 1 / fps
            time.sleep(max(0, deadline - time.monotonic()))
            if time.monotonic() - deadline > 1:
                deadline = time.monotonic()
    finally:
        camera.close()


def main():
    import json
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
