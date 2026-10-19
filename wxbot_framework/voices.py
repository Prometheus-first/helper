"""本地语音校验及发送快照；不连接微信、网络或大模型。

支持微信使用的 20 ms/包 SILK v3 封装以及单声道 16-bit PCM WAV。
此处检查封装结构，并不解码验证 SILK 载荷；通用 SILK 的多帧包不在支持范围。
编码接口和固定包长依据 pysilk 上游 _silk.pyx；Tencent 头/无尾标及标准
-1 尾标依据 silk-v3-decoder/silk/test/Encoder.c。标准头仅接受完整尾标，
发送快照会转换为 Tencent 头并去掉尾标。
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from dataclasses import dataclass
import importlib
from io import BytesIO
import os
from pathlib import Path
import stat
import struct
import tempfile
from typing import Iterator

MAX_VOICE_BYTES = 20 * 1024 * 1024
MAX_VOICE_DURATION_MS = 60_000
MAX_VOICE_PATH_BYTES = 1023
SILK_PACKET_MS = 20
TENCENT_SILK_HEADER = b"\x02#!SILK_V3"
STANDARD_SILK_HEADER = b"#!SILK_V3"
_SAMPLE_RATES = (8000, 12000, 16000, 24000)


@dataclass(frozen=True, slots=True)
class VoiceInfo:
    path: Path
    duration_ms: int


@dataclass(frozen=True, slots=True)
class _VoiceData:
    duration_ms: int
    silk: bytes | None = None
    pcm: bytes | None = None
    sample_rate: int = 0


def _path_value(path) -> Path:
    try:
        value = os.fspath(path)
    except TypeError as error:
        raise ValueError("语音路径必须是本地文件路径") from error
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise ValueError("语音路径必须非空，且不能包含 NUL")
    if value.startswith(("\\\\", "//")) or "://" in value:
        raise ValueError("语音路径必须是本地文件路径，不支持网络路径或 URL")
    return Path(value)


def _check_local_path(path: Path) -> None:
    try:
        length = len(str(path).encode("utf-8"))
    except UnicodeError as error:
        raise ValueError("语音路径必须可以编码为 UTF-8") from error
    if length > MAX_VOICE_PATH_BYTES:
        raise ValueError("语音路径不能超过 1023 个 UTF-8 字节")
    if str(path).startswith(("\\\\", "//")):
        raise ValueError("语音路径必须是本地文件路径")
    if os.name == "nt" and path.anchor:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetDriveTypeW.argtypes = [ctypes.c_wchar_p]
        kernel.GetDriveTypeW.restype = ctypes.c_uint
        if kernel.GetDriveTypeW(path.anchor) == 4:  # DRIVE_REMOTE，包括映射网络盘。
            raise ValueError("语音路径不能位于网络驱动器")


def _read_voice(path) -> tuple[Path, bytes]:
    try:
        resolved = _path_value(path).resolve(strict=True)
        _check_local_path(resolved)
        information = resolved.stat()
        if not stat.S_ISREG(information.st_mode):
            raise ValueError("语音路径必须指向普通文件")
        if not 0 < information.st_size <= MAX_VOICE_BYTES:
            raise ValueError("语音文件必须非空，且不能超过 20 MiB")
        with resolved.open("rb") as source:
            opened = os.fstat(source.fileno())
            if not stat.S_ISREG(opened.st_mode):
                raise ValueError("语音路径必须指向普通文件")
            if not 0 < opened.st_size <= MAX_VOICE_BYTES:
                raise ValueError("语音文件必须非空，且不能超过 20 MiB")
            data = source.read(MAX_VOICE_BYTES + 1)
            current = os.fstat(source.fileno())
        if not 0 < len(data) <= MAX_VOICE_BYTES:
            raise ValueError("语音文件必须非空，且不能超过 20 MiB")
        if (len(data) != opened.st_size or current.st_size != opened.st_size or
                current.st_mtime_ns != opened.st_mtime_ns):
            raise ValueError("语音文件读取期间发生变化，请重试")
        return resolved, data
    except (OSError, RuntimeError) as error:
        raise ValueError("语音文件不存在或无法读取") from error


def _inspect_silk(data: bytes) -> _VoiceData:
    tencent = data.startswith(TENCENT_SILK_HEADER)
    header = TENCENT_SILK_HEADER if tencent else STANDARD_SILK_HEADER
    position = len(header)
    packets = 0
    terminated = False
    while position < len(data):
        if len(data) - position < 2:
            raise ValueError("SILK 语音的包长度字段被截断")
        size = struct.unpack_from("<h", data, position)[0]
        position += 2
        if size == -1:
            if tencent or position != len(data):
                raise ValueError("SILK 尾标仅能位于标准 SILK 文件末尾")
            terminated = True
            break
        if size <= 0:
            raise ValueError("SILK 语音包含无效或空数据包")
        if size > len(data) - position:
            raise ValueError("SILK 语音的数据包被截断")
        position += size
        packets += 1
        if packets * SILK_PACKET_MS > MAX_VOICE_DURATION_MS:
            raise ValueError("语音时长不能超过 60 秒")
    if not packets:
        raise ValueError("SILK 语音没有有效数据包")
    if not tencent and not terminated:
        raise ValueError("标准 SILK 语音缺少 -1 结束标记")
    # 保存经过校验的同一份字节，避免稍后复制源文件时读到已变化的内容。
    silk = data if tencent else TENCENT_SILK_HEADER + data[len(header):-2]
    return _VoiceData(packets * SILK_PACKET_MS, silk=silk)


def _inspect_wav(data: bytes) -> _VoiceData:
    if len(data) < 12 or struct.unpack_from("<I", data, 4)[0] + 8 != len(data):
        raise ValueError("WAV 文件长度无效或已截断")
    position = 12
    sample_rate = 0
    pcm = None
    while position < len(data):
        if len(data) - position < 8:
            raise ValueError("WAV 数据块头被截断")
        name, size = struct.unpack_from("<4sI", data, position)
        position += 8
        end = position + size
        padded_end = end + (size & 1)
        if padded_end > len(data):
            raise ValueError("WAV 数据块被截断")
        if name == b"fmt ":
            if sample_rate or size < 16:
                raise ValueError("WAV 格式信息无效")
            encoding, channels, rate, byte_rate, alignment, bits = struct.unpack_from(
                "<HHIIHH", data, position)
            if (encoding != 1 or channels != 1 or bits != 16 or
                    rate not in _SAMPLE_RATES or alignment != 2 or byte_rate != rate * 2):
                raise ValueError("WAV 仅支持单声道 16-bit PCM，采样率为 8/12/16/24 kHz")
            sample_rate = rate
        elif name == b"data":
            if pcm is not None or not size or size % 2:
                raise ValueError("WAV 音频数据必须非空且按 16-bit 样本对齐")
            pcm = data[position:end]
        position = padded_end
    if not sample_rate or pcm is None:
        raise ValueError("WAV 缺少格式信息或音频数据")
    samples = len(pcm) // 2
    if samples > sample_rate * 60:
        raise ValueError("语音时长不能超过 60 秒")
    duration_ms = (samples * 1000 + sample_rate - 1) // sample_rate
    return _VoiceData(duration_ms, pcm=pcm, sample_rate=sample_rate)


def _inspect_data(data: bytes) -> _VoiceData:
    if data.startswith((TENCENT_SILK_HEADER, STANDARD_SILK_HEADER)):
        return _inspect_silk(data)
    if data.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return _inspect_wav(data)
    raise ValueError("语音格式仅支持 SILK v3 或单声道 16-bit PCM WAV，暂不支持 MP3")


def inspect_voice_file(path) -> VoiceInfo:
    """按内容检查文件；WAV 时长向上取整为毫秒，SILK 按支持的 20 ms 包计时。"""
    resolved, data = _read_voice(path)
    return VoiceInfo(resolved, _inspect_data(data).duration_ms)


def _load_encoder():
    try:
        encoder = importlib.import_module("pysilk")
    except (ImportError, OSError) as error:
        raise RuntimeError("发送 WAV 需要可选依赖，请运行：python -m pip install silk-python") from error
    if not callable(getattr(encoder, "encode", None)):
        raise RuntimeError("已安装的 pysilk 不是语音编码器，请安装 silk-python")
    return encoder


@contextmanager
def prepare_voice(path, directory) -> Iterator[VoiceInfo]:
    """生成只在上下文内有效的 Tencent SILK 快照，退出时仅删除本次临时目录。

    WAV 最后一帧补静音至 20 ms，避免上游编码器丢弃不足一帧的 PCM。
    不会安装依赖，也不会改动源文件或调用模型。
    """
    _, data = _read_voice(path)
    voice = _inspect_data(data)
    encoder = _load_encoder() if voice.pcm is not None else None
    try:
        root = _path_value(directory).resolve()
        _check_local_path(root)
        root.mkdir(parents=True, exist_ok=True)
        if not root.is_dir():
            raise ValueError("语音临时目录必须是本地目录")
    except (OSError, RuntimeError) as error:
        raise ValueError("无法创建语音临时目录") from error
    with tempfile.TemporaryDirectory(prefix="voice-", dir=root) as temporary:
        owned = Path(temporary).resolve(strict=True)
        if not owned.is_relative_to(root):
            raise ValueError("语音临时目录超出预期范围")
        snapshot = owned / "voice.silk"
        _check_local_path(snapshot)
        if voice.silk is not None:
            with snapshot.open("xb") as output:
                output.write(voice.silk)
            result = VoiceInfo(snapshot, voice.duration_ms)
        else:
            # pysilk 读取整帧 PCM；在内存中补足最后一帧，不创建额外 PCM 文件。
            frame_bytes = voice.sample_rate // 50 * 2
            padded = voice.pcm + b"\0" * (-len(voice.pcm) % frame_bytes)
            try:
                with snapshot.open("xb") as output:
                    encoder.encode(BytesIO(padded), output, voice.sample_rate, 24000, tencent=True)
            except Exception as error:
                raise RuntimeError("WAV 转换为 SILK 语音失败") from error
            result = inspect_voice_file(snapshot)
            with snapshot.open("rb") as source:
                header = source.read(len(TENCENT_SILK_HEADER))
            if (header != TENCENT_SILK_HEADER or
                    result.duration_ms != len(padded) // frame_bytes * SILK_PACKET_MS):
                raise ValueError("编码器返回的 SILK 格式或语音时长与输入不符")
        yield result
