"""Ephemeral loopback-only preview; camera frames stay in memory."""
import json
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import uuid


class VideoPreview:
    def __init__(self, source):
        self.source = source
        self._stop = threading.Event()
        self._prefix = '/' + uuid.uuid4().hex + '/'
        preview = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                path = self.path.split('?', 1)[0]
                if not path.startswith(preview._prefix):
                    self.send_error(404)
                    return
                route = path[len(preview._prefix):]
                if route == '':
                    body = ('''<!doctype html><meta charset="utf-8"><title>AI 人像本机预览</title>
<style>body{background:#101820;color:#edf5ff;font:17px system-ui;text-align:center;margin:18px}
img{max-width:95vw;max-height:78vh;object-fit:contain;background:#20364c;border-radius:12px}p{margin:10px}</style>
<h2>AI 人像 · 本机实时预览</h2><p>摄像头 → __PROVIDER__ CUDA → 框架 I420 帧</p>
<p>__CONTROLS__</p><img src="stream.mjpg" alt="等待模型和摄像头就绪"><p id="status">正在初始化</p>
<script>setInterval(async()=>{try{let s=await(await fetch('status')).json();
document.getElementById('status').textContent=`${s.phase} | 输出 ${s.frames} 帧 | 推理 ${s.inference_ms} ms | 延迟 ${s.latency_ms} ms | 丢弃 ${s.dropped}`;}catch{}},1000)</script>''').replace('__PROVIDER__', html.escape(source.provider)).replace('__CONTROLS__',
    '''<button onclick="fetch('calibrate',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})">正面自然表情时点此重新校准</button>''' if hasattr(source, 'calibrate') else '').encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(body)
                elif route == 'status':
                    body = json.dumps(source.statistics()).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    self.wfile.write(body)
                elif route == 'stream.mjpg':
                    self.send_response(200)
                    self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
                    self.send_header('Cache-Control', 'no-store')
                    self.end_headers()
                    sequence = 0
                    try:
                        while not preview._stop.is_set() and source.statistics()['running']:
                            frame = source.latest_frame
                            if frame is not None and frame.sequence != sequence and frame.preview_jpeg:
                                sequence = frame.sequence
                                self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ' +
                                    str(len(frame.preview_jpeg)).encode() + b'\r\n\r\n' + frame.preview_jpeg + b'\r\n')
                                self.wfile.flush()
                            time.sleep(.015)
                    except ConnectionError:
                        pass
                else:
                    self.send_error(404)

            def do_POST(self):
                expected_origin = f'http://127.0.0.1:{preview.server.server_port}'
                if (self.path != preview._prefix + 'calibrate' or
                        self.headers.get('Origin') not in (None, expected_origin) or
                        self.headers.get('Content-Type') != 'application/json' or
                        self.headers.get('Content-Length') != '2' or not hasattr(source, 'calibrate')):
                    self.send_error(403)
                    return
                self.rfile.read(2)
                accepted = source.calibrate()
                self.send_response(200 if accepted else 409)
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(b'{}')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = f'http://127.0.0.1:{self.server.server_port}{self._prefix}'
        self._thread = threading.Thread(target=self.server.serve_forever, name='VideoLocalPreview', daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self.server.shutdown()
        self.server.server_close()
        self._thread.join(3)
