"""Windows body tracking and bounded pipe bridge to the Linux avatar renderer."""
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parent))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from facefusion_worker import CameraFrames,status
from wxbot_framework.body_pose import BodyPoseTracker,HeadPoseTracker


def linux_path(path):
    path=Path(path).resolve(strict=True)
    if len(path.drive)!=2 or path.drive[1]!=':':
        raise ValueError('WSL avatar requires local Windows drive paths')
    return '/mnt/'+path.drive[0].lower()+'/'+path.as_posix()[3:]


def run(config,output):
    repository=Path(config['repository']).resolve(strict=True)
    mode=config.get('tracking_mode','body')
    if mode not in ('body','head'):
        raise ValueError('Invalid avatar tracking mode')
    task=repository/'checkpoints/mediapipe'/('face_landmarker.task' if mode=='head' else 'pose_landmarker_full.task')
    if not task.is_file():
        raise RuntimeError('body_pose_model_missing')
    status('checking_models')
    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python
    from mediapipe.tasks.python import vision
    worker=Path(__file__).with_name('lhmpp_worker.py')
    command=['wsl.exe','-d',config['wsl_distribution'],'--',
             config['linux_python_executable'],'-I','-u',linux_path(worker)]
    process=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    ready=threading.Event()
    stopped=threading.Event()
    tracker=[None]
    def diagnostics():
        for line in iter(lambda:process.stderr.readline(4097),b''):
            if len(line)>4096:
                continue
            try:
                data=json.loads(line)
                if data.get('phase')=='avatar_ready':
                    tracker[0]=HeadPoseTracker() if mode=='head' else BodyPoseTracker(data['rest_joints'])
                    ready.set()
                    status('models_ready')
                elif data.get('phase') in ('warming_models','failed'):
                    status(data['phase'],reason=data.get('reason',''))
            except (ValueError,KeyError,TypeError):
                pass
        stopped.set()
    def frames():
        try:
            while True:
                block=process.stdout.read1(65536)
                if not block:
                    break
                output.write(block)
                output.flush()
        except (BrokenPipeError,OSError):
            pass
        finally:
            stopped.set()
    settings=dict(config,repository=linux_path(repository),source_photo=linux_path(config['source_photo']))
    process.stdin.write((json.dumps(settings)+'\n').encode())
    process.stdin.flush()
    threads=[threading.Thread(target=diagnostics,daemon=True),threading.Thread(target=frames,daemon=True)]
    for thread in threads:
        thread.start()
    camera=None
    detector=None
    try:
        ready_deadline=time.monotonic()+180
        while not ready.wait(.1):
            if stopped.is_set() or process.poll() is not None:
                raise RuntimeError('body_renderer_initialization_failed')
            if time.monotonic()>ready_deadline:
                raise RuntimeError('body_renderer_startup_timeout')
        if mode=='head':
            options=vision.FaceLandmarkerOptions(base_options=python.BaseOptions(model_asset_path=str(task)),
                running_mode=vision.RunningMode.VIDEO,num_faces=1,output_facial_transformation_matrixes=True)
            detector=vision.FaceLandmarker.create_from_options(options)
        else:
            options=vision.PoseLandmarkerOptions(base_options=python.BaseOptions(model_asset_path=str(task)),
                running_mode=vision.RunningMode.VIDEO,num_poses=1)
            detector=vision.PoseLandmarker.create_from_options(options)
        recalibrate=threading.Event()
        def commands():
            while True:
                line=sys.stdin.buffer.readline(257)
                if not line:
                    return
                try:
                    if len(line)<=256 and json.loads(line).get('command')=='calibrate':
                        recalibrate.set()
                except (ValueError,AttributeError):
                    pass
        threading.Thread(target=commands,daemon=True,name='AvatarControls').start()
        camera=CameraFrames(config.get('camera_id',0),cv2)
        status('camera_ready')
        sequence=previous=animated=0
        last_report=time.monotonic()
        while not stopped.is_set():
            current=camera.latest(previous)
            if current is None:
                time.sleep(.005)
                continue
            previous,captured,frame=current
            image=mp.Image(image_format=mp.ImageFormat.SRGB,data=cv2.cvtColor(frame,cv2.COLOR_BGR2RGB))
            result=detector.detect_for_video(image,captured//1000000)
            if recalibrate.is_set():
                recalibrate.clear()
                tracker[0].calibrate()
            if mode=='head':
                matrices=result.facial_transformation_matrixes
                pose,root,tracked=tracker[0].update_matrix(matrices[0] if matrices else [])
            elif result.pose_world_landmarks:
                values=[[p.x,p.y,p.z,p.visibility] for p in result.pose_world_landmarks[0]]
                pose,root,tracked=tracker[0].update(values)
            else:
                pose,root,tracked=tracker[0].update([])
            sequence+=1
            animated+=int(tracked)
            packet={'sequence':sequence,'captured_ns':captured,'body_pose':pose,
                    'root_delta':root,'tracked':tracked,'tracking_mode':mode}
            process.stdin.write((json.dumps(packet)+'\n').encode())
            process.stdin.flush()
            now=time.monotonic()
            if now-last_report>=1:
                status('streaming',faces=int(tracked),swapped_frames=animated)
                last_report=now
            time.sleep(max(0,1/config.get('fps',15)-.025))
    finally:
        if camera is not None:
            camera.close()
        if detector is not None:
            detector.close()
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)


def main():
    line=sys.stdin.buffer.readline(16385)
    if not line or len(line)>16384:
        raise RuntimeError('invalid_avatar_configuration')
    config=json.loads(line)
    output=sys.stdout.buffer
    with contextlib.redirect_stdout(sys.stderr):
        run(config,output)


if __name__=='__main__':
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as error:
        status('failed',reason=str(error)[:120] if isinstance(error,RuntimeError) else type(error).__name__)
        raise SystemExit(1)
