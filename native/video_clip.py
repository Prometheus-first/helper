"""One-shot local video timeline. It does not own the AI model or frame writer."""
import math
import time


class VideoClip:
    def __init__(self, path, cv2, *, clock=time.monotonic):
        self.cv2, self.clock = cv2, clock
        self.capture = cv2.VideoCapture(str(path), cv2.CAP_FFMPEG)
        if not self.capture.isOpened():
            self.capture.release()
            raise RuntimeError('video_open_failed')
        fps = self.capture.get(cv2.CAP_PROP_FPS)
        self.fps = fps if math.isfinite(fps) and 1 <= fps <= 120 else 25
        count = self.capture.get(cv2.CAP_PROP_FRAME_COUNT)
        self.duration = count / self.fps if math.isfinite(count) and count > 0 else 0
        self.started = clock()
        self.position, self.frame = -1, None

    def progress(self):
        elapsed = max(0, self.clock() - self.started)
        return min(elapsed, self.duration) if self.duration else elapsed, self.duration

    def next_frame(self):
        target = max(0, int((self.clock() - self.started) * self.fps))
        # Keep a slow consumer bounded; decode only the current time position.
        if target > self.position + 120:
            if self.capture.set(self.cv2.CAP_PROP_POS_FRAMES, target):
                self.position = target - 1
            else:
                target = self.position + 120
        while self.position < target:
            ok, frame = self.capture.read()
            if not ok or frame is None:
                if self.position < 0:
                    raise RuntimeError('video_empty')
                return None
            self.position += 1
            self.frame = frame
        return self.frame

    def close(self):
        self.capture.release()
        self.frame = None


def letterbox(frame, width, height, cv2):
    import numpy as np
    scale = min(width / frame.shape[1], height / frame.shape[0])
    size = max(1, round(frame.shape[1] * scale)), max(1, round(frame.shape[0] * scale))
    image = cv2.resize(frame, size)
    result = np.zeros((height, width, 3), dtype=np.uint8)
    x, y = (width - size[0]) // 2, (height - size[1]) // 2
    result[y:y + size[1], x:x + size[0]] = image
    return result
