"""校验本地 MP4 并读取视频时长，不执行视频或连接网络。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct

from .files import inspect_file


@dataclass(frozen=True, slots=True)
class VideoInfo:
    path: Path
    duration_seconds: int


def _boxes(stream, begin, end):
    position = begin
    count = 0
    while position < end:
        count += 1
        if count > 100_000 or end - position < 8:
            raise ValueError("MP4 数据块格式无效")
        stream.seek(position)
        raw = stream.read(8)
        if len(raw) != 8:
            raise ValueError("MP4 文件已截断")
        size, kind = struct.unpack(">I4s", raw)
        header = 8
        if size == 1:
            raw = stream.read(8)
            if len(raw) != 8:
                raise ValueError("MP4 数据块头已截断")
            size = struct.unpack(">Q", raw)[0]
            header = 16
        elif size == 0:
            size = end - position
        if size < header or size > end - position:
            raise ValueError("MP4 数据块长度无效")
        yield kind, position + header, position + size
        position += size


def _movie_duration(stream, start, end):
    stream.seek(start)
    prefix = stream.read(min(end - start, 32))
    if len(prefix) < 20:
        raise ValueError("MP4 时长信息不完整")
    if prefix[0] == 0:
        timescale, duration = struct.unpack_from(">II", prefix, 12)
    elif prefix[0] == 1 and len(prefix) >= 32:
        timescale, duration = struct.unpack_from(">IQ", prefix, 20)
    else:
        raise ValueError("不支持的 MP4 时长格式")
    if not timescale or not duration:
        raise ValueError("视频时长无效")
    seconds = (duration + timescale - 1) // timescale
    if not 1 <= seconds <= 0x7fffffff:
        raise ValueError("视频时长超出支持范围")
    return seconds


def inspect_video(path) -> VideoInfo:
    file = inspect_file(path)
    duration = None
    video_track = False
    try:
        with file.path.open("rb") as stream:
            top = list(_boxes(stream, 0, file.size))
            if not any(kind == b"ftyp" for kind, _, _ in top):
                raise ValueError("视频必须是 MP4 格式")
            for kind, start, end in top:
                if kind != b"moov":
                    continue
                for child, begin, limit in _boxes(stream, start, end):
                    if child == b"mvhd":
                        duration = _movie_duration(stream, begin, limit)
                    elif child == b"trak":
                        for part, pstart, pend in _boxes(stream, begin, limit):
                            if part != b"mdia":
                                continue
                            for media, mstart, mend in _boxes(stream, pstart, pend):
                                if media == b"hdlr" and mend - mstart >= 12:
                                    stream.seek(mstart + 8)
                                    video_track |= stream.read(4) == b"vide"
    except OSError as error:
        raise ValueError("无法读取视频文件") from error
    if duration is None or not video_track:
        raise ValueError("MP4 缺少有效时长或视频轨道")
    return VideoInfo(file.path, duration)
