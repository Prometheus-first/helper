"""Experimental video controls on the owned Windows bridge."""
from __future__ import annotations

import ctypes as c
from pathlib import Path

import pefile

from run_selftest import K, TEXT_DLL, check, modules, remote_call


class VideoProbeControl(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in ('size', 'abi', 'operation', 'armed')] + [
        (name, c.c_uint64) for name in ('callbacks', 'invalid_metadata',
                                      'replaced_frames', 'first_tick', 'last_tick')] + [
        ('input_width', c.c_int32), ('input_height', c.c_int32), ('input_format', c.c_uint32),
        ('output_width', c.c_int32), ('output_height', c.c_int32), ('output_format', c.c_uint32),
        ('input_strides', c.c_int32 * 3), ('output_strides', c.c_int32 * 3)]


assert c.sizeof(VideoProbeControl) == 104


class VideoQuality(c.Structure):
    _fields_ = [(name, c.c_uint32) for name in ('size', 'abi', 'width', 'height', 'format',
                                               'bitrate_bps', 'fps_cap', 'native_status')] + [
        ('encoder_calls', c.c_uint64)]
    _fields_ += [('fallback', c.c_uint32), ('reserved', c.c_uint32)]


def call_video_quality(pid):
    control = VideoQuality(size=c.sizeof(VideoQuality), abi=1)
    _call_video_control(pid, b'WxVideoQuality', control)
    return {name: getattr(control, name) for name, _ in control._fields_}


def call_video_probe(pid, operation):
    if operation not in ('start', 'statistics', 'stop'):
        raise ValueError('未知视频探针操作')
    control = VideoProbeControl(size=c.sizeof(VideoProbeControl), abi=1,
                                operation=('start', 'statistics', 'stop').index(operation) + 1)
    _call_video_control(pid, b'WxVideoProbe', control)
    return {name: list(getattr(control, name)) if name.endswith('_strides') else getattr(control, name)
            for name, _ in control._fields_}


def _call_video_control(pid, export_name, control):
    loaded = modules(pid).get(TEXT_DLL.name.lower())
    if not loaded or Path(loaded[1]).resolve() != TEXT_DLL.resolve():
        raise RuntimeError('当前项目的文本桥接未加载')
    with pefile.PE(str(TEXT_DLL)) as pe:
        export = next((item.address for item in pe.DIRECTORY_ENTRY_EXPORT.symbols
                       if item.name == export_name), None)
    if export is None:
        raise RuntimeError('当前桥接没有视频探针接口，请重新编译')
    process = check(K.OpenProcess(0x43a, False, pid), '打开视频控制通道')
    buffer = None
    pending = False
    try:
        buffer = check(K.VirtualAllocEx(process, None, c.sizeof(control), 0x3000, 0x04),
                       '分配视频控制结构')
        count = c.c_size_t()
        check(K.WriteProcessMemory(process, buffer, c.byref(control), c.sizeof(control),
                                   c.byref(count)), '写入视频控制结构')
        if count.value != c.sizeof(control):
            raise RuntimeError('视频控制写入不完整')
        try:
            code = remote_call(process, loaded[0] + export, buffer)
        except TimeoutError:
            pending = True
            raise
        if code:
            raise RuntimeError(f'视频探针操作失败：{code}')
        check(K.ReadProcessMemory(process, buffer, c.byref(control), c.sizeof(control),
                                  c.byref(count)), '读取视频统计')
        if count.value != c.sizeof(control) or control.size != c.sizeof(control) or control.abi != 1:
            raise RuntimeError('视频控制结构不匹配')
    finally:
        # A timed-out remote thread may still own its argument.
        if buffer and not pending:
            K.VirtualFreeEx(process, buffer, 0, 0x8000)
        K.CloseHandle(process)
