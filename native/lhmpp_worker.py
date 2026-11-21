"""Linux rendering child. Receives only body poses; raw webcam frames stay on Windows."""
import contextlib
import json
import os
from pathlib import Path
import struct
import sys
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parent))
from lhmpp_engine import BodyAvatar

HEADER=struct.Struct('<4sIIIIQQI')


def status(phase, **values):
    print(json.dumps({'phase':phase,**values}),file=sys.stderr,flush=True)


def main():
    line=sys.stdin.buffer.readline(16385)
    if not line or len(line)>16384:
        raise RuntimeError('invalid_avatar_configuration')
    config=json.loads(line)
    # Preserve one binary pipe descriptor, then redirect even native C/C++
    # stdout diagnostics away from the frame protocol.
    output=os.fdopen(os.dup(sys.stdout.fileno()),'wb',buffering=0)
    os.dup2(sys.stderr.fileno(),sys.stdout.fileno())
    width,height=config['width'],config['height']
    if (type(width) is not int or type(height) is not int or width%2 or height%2 or
            not 16<=width<=1920 or not 16<=height<=1920 or width*height>2073600):
        raise ValueError('Invalid frame dimensions')
    latest=[None]
    lock=threading.Lock()
    def poses():
        while True:
            line=sys.stdin.buffer.readline(16385)
            if not line:
                # The Windows job owns the pipe. Parent crash/stop must also
                # release CUDA in Linux, whose processes are outside that job.
                os._exit(0)
            if len(line)>16384:
                os._exit(1)
            try:
                packet=json.loads(line)
                if (type(packet.get('captured_ns')) is not int or packet['captured_ns']<=0 or
                        type(packet.get('sequence')) is not int or packet['sequence']<=0):
                    continue
                with lock:
                    latest[0]=packet
            except (ValueError,TypeError,KeyError):
                continue
    threading.Thread(target=poses,daemon=True,name='AvatarPoses').start()
    with contextlib.redirect_stdout(sys.stderr):
        status('warming_models')
        avatar=BodyAvatar(config['repository'],config['source_photo'])
        status('avatar_ready',rest_joints=avatar.rest_joints,linux_pid=os.getpid())
        import cv2
        import numpy as np
        sequence=last_input=0
        while True:
            with lock:
                packet=latest[0]
            if packet is None or packet['sequence']==last_input:
                time.sleep(.005)
                continue
            last_input=packet['sequence']
            frame,elapsed=avatar.render(packet['body_pose'],packet['root_delta'])
            head_mode=packet.get('tracking_mode')=='head'
            if head_mode:
                fh,fw=frame.shape[:2]
                frame=frame[round(fh*.02):round(fh*.68),round(fw*.2):round(fw*.8)]
            h,w=frame.shape[:2]
            factor=min(width/w,height/h)
            resized=cv2.resize(frame,(round(w*factor),round(h*factor)))
            canvas=np.full((height,width,3),240,np.uint8)
            y,x=(height-resized.shape[0])//2,(width-resized.shape[1])//2
            canvas[y:y+resized.shape[0],x:x+resized.shape[1]]=cv2.cvtColor(resized,cv2.COLOR_RGB2BGR)
            label=('AI AVATAR / HEAD TRACKING' if head_mode else 'AI BODY AVATAR TEST') if packet.get('tracked') else (
                'AI AVATAR / WAITING FOR FACE' if head_mode else 'AI AVATAR / WAITING FOR BODY')
            cv2.putText(canvas,label,(12,28),cv2.FONT_HERSHEY_SIMPLEX,.6,(0,0,0),3,cv2.LINE_AA)
            cv2.putText(canvas,label,(12,28),cv2.FONT_HERSHEY_SIMPLEX,.6,(0,170,210),1,cv2.LINE_AA)
            pixels=cv2.cvtColor(canvas,cv2.COLOR_BGR2YUV_I420).tobytes()
            jpeg=b''
            if config.get('preview'):
                ok,data=cv2.imencode('.jpg',canvas,[cv2.IMWRITE_JPEG_QUALITY,90])
                if ok and data.nbytes<=524288:
                    jpeg=data.tobytes()
            sequence+=1
            output.write(HEADER.pack(b'FFV1',width,height,len(pixels),len(jpeg),sequence,
                                    packet['captured_ns'],min(0xffffffff,round(elapsed*1000))))
            output.write(pixels)
            output.write(jpeg)
            output.flush()


if __name__=='__main__':
    try:
        main()
    except BrokenPipeError:
        pass
    except Exception as error:
        status('failed',reason=type(error).__name__)
        raise SystemExit(1)
