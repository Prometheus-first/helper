"""受控视频接收：异步 CDN 下载、完整性校验和按账号隔离保存。"""
from __future__ import annotations

import queue

from .file_receiver import FileReceiver
from .files import _check_local_path
from .video_metadata import VideoMetadataError, parse_video_metadata, select_video_download
from .videos import inspect_video


class VideoReceiver(FileReceiver):
    """沿用文件接收器的队列和关闭语义；下载器不应保留视频凭据。"""

    def _failed(self, data, message):
        data.update(video_status='failed', video_path='', video_error=message)
        self._deliver(data)

    @staticmethod
    def _snapshot(data):
        snapshot = dict(data)
        raw = snapshot.pop('raw_content', None) or snapshot.get('content', '')
        snapshot.update(msg='', content='', video_status='pending', video_path='',
                        video_error='', video_quality='unknown', video_size=None,
                        video_metadata={}, monitor_only=False)
        return snapshot, raw

    def submit(self, data: dict) -> bool:
        if not isinstance(data, dict):
            raise ValueError('视频事件必须为字典')
        if not self._active():
            return False
        snapshot, raw = self._snapshot(data)
        try:
            metadata = parse_video_metadata(raw)
            key, aes, size, md5, quality = select_video_download(metadata)
            if not key or not aes or not size or not md5:
                raise VideoMetadataError('视频缺少完整下载参数')
        except (VideoMetadataError, ValueError):
            self._failed(snapshot, '视频元数据无效或缺少完整下载参数')
            return False
        snapshot.update(video_size=size, video_quality=quality,
                        video_metadata={'duration':metadata.duration, 'md5':md5})
        with self._enqueue_lock:
            if self._stop.is_set():
                return False
            try:
                self._queue.put_nowait((snapshot, metadata))
            except queue.Full:
                full = True
            else:
                full = False
        if full:
            self._failed(snapshot, '视频接收队列已满')
            return False
        return True

    def _download(self, metadata, data):
        workspace = self._new_workspace()
        try:
            destination = workspace / 'video.mp4'
            _check_local_path(destination)
            if destination.exists() or destination.is_symlink():
                raise ValueError('视频下载目标必须尚不存在')
            if not self._active():
                return None
            if self._downloader(metadata, destination) is not True:
                raise ValueError('视频下载未成功')
            if not self._active():
                return None
            inspect_video(destination)
            _, _, size, md5, _ = select_video_download(metadata)
            return self.store.save_download(destination, name='video.mp4', sequence=data.get('sequence'),
                                            expected_size=size, expected_md5=md5, is_current=self._active)
        finally:
            self._remove_workspace(workspace)

    def _process(self, data, metadata):
        if not self._active():
            return
        try:
            result = self._download(metadata, data)
            if result is None:
                return
            path, size = result
            if not self._active():
                self._remove_saved(path)
                return
            data.update(video_status='ready', video_path=str(path), video_size=size, video_error='')
            if not self._deliver(data):
                self._remove_saved(path)
        except Exception:
            self._failed(data, '视频下载、校验或保存失败')
