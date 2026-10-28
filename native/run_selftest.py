"""版本锁定的原生桥接，复用当前账号的任务队列。"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import json
import argparse
import hashlib
from pathlib import Path
import sys
import time
import uuid

import pefile

ROOT = Path(__file__).resolve().parent.parent
DLL = ROOT / "native/build/selftest_bridge.dll"
TEXT_DLL = ROOT / "native/build_text/text_bridge.dll"
K = c.WinDLL("kernel32", use_last_error=True)
PTR = c.c_void_p

K.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]; K.OpenProcess.restype = w.HANDLE
K.CloseHandle.argtypes = [w.HANDLE]; K.CloseHandle.restype = w.BOOL
K.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
K.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]; K.CreateToolhelp32Snapshot.restype = w.HANDLE
K.GetModuleHandleW.argtypes = [w.LPCWSTR]; K.GetModuleHandleW.restype = w.HMODULE
K.GetProcAddress.argtypes = [w.HMODULE, c.c_char_p]; K.GetProcAddress.restype = PTR
K.GetModuleHandleExW.argtypes = [w.DWORD, PTR, c.POINTER(w.HMODULE)]
K.GetModuleFileNameW.argtypes = [w.HMODULE, w.LPWSTR, w.DWORD]
K.VirtualAllocEx.argtypes = [w.HANDLE, PTR, c.c_size_t, w.DWORD, w.DWORD]; K.VirtualAllocEx.restype = PTR
K.VirtualFreeEx.argtypes = [w.HANDLE, PTR, c.c_size_t, w.DWORD]
K.WriteProcessMemory.argtypes = [w.HANDLE, PTR, PTR, c.c_size_t, c.POINTER(c.c_size_t)]
K.ReadProcessMemory.argtypes = [w.HANDLE, PTR, PTR, c.c_size_t, c.POINTER(c.c_size_t)]
K.CreateRemoteThread.argtypes = [w.HANDLE, PTR, c.c_size_t, PTR, PTR, w.DWORD, c.POINTER(w.DWORD)]
K.CreateRemoteThread.restype = w.HANDLE
K.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]; K.WaitForSingleObject.restype = w.DWORD
K.GetExitCodeThread.argtypes = [w.HANDLE, c.POINTER(w.DWORD)]


class Module(c.Structure):
    _fields_ = [("dwSize", w.DWORD), ("th32ModuleID", w.DWORD), ("th32ProcessID", w.DWORD),
                ("GlblcntUsage", w.DWORD), ("ProccntUsage", w.DWORD), ("modBaseAddr", PTR),
                ("modBaseSize", w.DWORD), ("hModule", w.HMODULE),
                ("szModule", w.WCHAR * 256), ("szExePath", w.WCHAR * 260)]


class Config(c.Structure):
    _fields_ = [("size", w.DWORD), ("pid", w.DWORD), ("manager", c.c_uint64),
                ("log_path", w.WCHAR * 260), ("text", c.c_char * 1024)]


class TextConfig(Config):
    _fields_ = [("target", c.c_char * 128)]

class AtTextConfig(TextConfig):
    _fields_ = [("members", c.c_char * 1024)]


class EmoticonConfig(TextConfig):
    _fields_ = [("account", c.c_char * 128), ("send", c.c_uint32), ("reserved", c.c_uint32)]


class VoiceConfig(TextConfig):
    _fields_ = [("account", c.c_char * 128), ("duration_ms", c.c_uint32), ("send", c.c_uint32)]


class FileConfig(TextConfig):
    _fields_ = [("account", c.c_char * 128), ("send", c.c_uint32), ("reserved", c.c_uint32)]


class GroupManagementConfig(TextConfig):
    _fields_ = [("account", c.c_char * 128), ("members", c.c_char * 1024),
                ("operation", c.c_uint32), ("reserved", c.c_uint32)]


class FriendRequestConfig(TextConfig):
    _fields_ = [("account", c.c_char * 128), ("v3", c.c_char * 8192),
                ("v4", c.c_char * 8192), ("scene", c.c_uint32), ("reserved", c.c_uint32)]


class ImageDownloadConfig(Config):
    _fields_ = [("filekey", c.c_char * 1024), ("aeskey", c.c_char * 65),
                ("image_type", c.c_uint32)]


class FileDownloadConfig(Config):
    _fields_ = [("filekey", c.c_char * 1024), ("aeskey", c.c_char * 65),
                ("expected_size", c.c_uint64), ("account", c.c_char * 128)]


assert c.sizeof(Config) == 1560 and c.sizeof(TextConfig) == 1688 and c.sizeof(AtTextConfig) == 2712
assert c.sizeof(ImageDownloadConfig) == 2656
assert c.sizeof(EmoticonConfig) == 1824 and EmoticonConfig.account.offset == 1688
assert EmoticonConfig.send.offset == 1816 and EmoticonConfig.reserved.offset == 1820
assert c.sizeof(VoiceConfig) == 1824 and VoiceConfig.account.offset == 1688
assert VoiceConfig.duration_ms.offset == 1816 and VoiceConfig.send.offset == 1820
assert c.sizeof(FileConfig) == 1824 and FileConfig.account.offset == 1688
assert FileConfig.send.offset == 1816 and FileConfig.reserved.offset == 1820
assert c.sizeof(FileDownloadConfig) == 2792
assert FileDownloadConfig.expected_size.offset == 2656 and FileDownloadConfig.account.offset == 2664
assert c.sizeof(GroupManagementConfig) == 2848
assert GroupManagementConfig.account.offset == 1688 and GroupManagementConfig.members.offset == 1816
assert GroupManagementConfig.operation.offset == 2840
assert c.sizeof(FriendRequestConfig) == 18208
assert FriendRequestConfig.v3.offset == 1816 and FriendRequestConfig.v4.offset == 10008
assert FriendRequestConfig.scene.offset == 18200


class ProcessEntry(c.Structure):
    _fields_ = [("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("pid", w.DWORD),
                ("heap", c.c_size_t), ("module", w.DWORD), ("threads", w.DWORD),
                ("parent", w.DWORD), ("priority", w.LONG), ("flags", w.DWORD),
                ("exe", w.WCHAR * 260)]


K.Module32FirstW.argtypes = [w.HANDLE, c.POINTER(Module)]
K.Module32NextW.argtypes = [w.HANDLE, c.POINTER(Module)]
K.Process32FirstW.argtypes = [w.HANDLE, c.POINTER(ProcessEntry)]
K.Process32NextW.argtypes = [w.HANDLE, c.POINTER(ProcessEntry)]


def check(result, label):
    if not result:
        raise c.WinError(c.get_last_error(), f"{label} 失败")
    return result


def modules(pid):
    snapshot = K.CreateToolhelp32Snapshot(0x8 | 0x10, pid)
    if snapshot == c.c_void_p(-1).value:
        raise c.WinError(c.get_last_error())
    result = {}
    try:
        entry = Module(); entry.dwSize = c.sizeof(entry)
        check(K.Module32FirstW(snapshot, c.byref(entry)), "读取模块")
        while True:
            result[entry.szModule.lower()] = (int(entry.modBaseAddr), entry.szExePath)
            if not K.Module32NextW(snapshot, c.byref(entry)): break
    finally:
        K.CloseHandle(snapshot)
    return result


def remote_system_function(pid, name):
    address = check(K.GetProcAddress(K.GetModuleHandleW("kernel32.dll"), name), "查找系统函数")
    owner = w.HMODULE()
    check(K.GetModuleHandleExW(0x4 | 0x2, address, c.byref(owner)), "读取函数所属模块")
    path = c.create_unicode_buffer(32768)
    check(K.GetModuleFileNameW(owner, path, len(path)), "读取系统模块名")
    remote_base = modules(pid)[Path(path.value).name.lower()][0]
    return remote_base + address - owner.value


def remote_call(process, address, parameter, timeout=30000):
    thread = check(K.CreateRemoteThread(process, None, 0, address, parameter, 0, None), "创建自测线程")
    try:
        status = K.WaitForSingleObject(thread, timeout)
        if status != 0:
            raise TimeoutError(f"自测线程未完成，等待结果 {status}；不要终止该线程")
        result = w.DWORD()
        check(K.GetExitCodeThread(thread, c.byref(result)), "读取自测结果")
        return result.value
    finally:
        K.CloseHandle(thread)


def wechat_candidates():
    snapshot = K.CreateToolhelp32Snapshot(2, 0)
    if snapshot == c.c_void_p(-1).value: raise c.WinError(c.get_last_error())
    candidates = []
    try:
        entry = ProcessEntry(); entry.dwSize = c.sizeof(entry)
        check(K.Process32FirstW(snapshot, c.byref(entry)), "枚举微信进程")
        while True:
            if entry.exe.lower() == "weixin.exe":
                try:
                    if "weixin.dll" in modules(entry.pid): candidates.append(entry.pid)
                except OSError:
                    pass
            if not K.Process32NextW(snapshot, c.byref(entry)): break
    finally:
        K.CloseHandle(snapshot)
    return candidates


def find_wechat():
    candidates = wechat_candidates()
    if len(candidates) != 1:
        raise RuntimeError(f"需要唯一的微信主进程，当前候选为 {candidates}；可指定 --pid")
    return candidates[0]


def compatible_core(path, baseline):
    """Match an exact whitelisted build; never infer offsets from its version label."""
    entries = baseline.get('profiles', [baseline])
    if not isinstance(entries, list) or not entries or any(not isinstance(item, dict) for item in entries):
        return None
    data = Path(path).read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    return next((entry for entry in entries if entry.get('sha256') == digest and
                 type(entry.get('size')) is int and entry['size'] > 0 and entry['size'] == len(data)), None)


def run(mode, *, pid=None, text="WXNATIVE_TEST_3", keep_loaded=False, preserve_report=True,
        bridge_path=None, target="filehelper", mentions=None, image_path=None, image_probe=False,
        download_image=None, emoticon_path=None, emoticon_probe=False, account=None,
        voice_path=None, voice_duration_ms=None, voice_probe=False,
        file_path=None, file_probe=False, download_file=None,
        group_operation=None, group_members=None, group_probe=False,
        friend_request=None, friend_probe=False):
    dll = Path(bridge_path or DLL).resolve()
    if dll not in (DLL.resolve(), TEXT_DLL.resolve()):
        raise ValueError("不支持的桥接 DLL 路径")
    generic = dll == TEXT_DLL.resolve()
    accepting_friend = mode == "--accept-friend"
    if accepting_friend:
        if (not generic or type(friend_probe) is not bool or
                image_path is not None or image_probe or download_image is not None or
                file_path is not None or file_probe or download_file is not None or
                voice_path is not None or voice_probe or voice_duration_ms is not None or
                emoticon_path is not None or emoticon_probe or mentions is not None or
                group_operation is not None or group_members is not None or group_probe):
            raise ValueError("好友申请操作不能与其他操作混用")
        from wxbot_framework.account import validate_account
        from wxbot_framework.friend_requests import FriendRequest, parse_friend_request
        account = validate_account(account)
        if not isinstance(friend_request, FriendRequest):
            raise ValueError("好友通过需要已解析的当前申请")
        checked = parse_friend_request(friend_request.raw_xml)
        if checked != friend_request or checked.opcode != 2:
            raise ValueError("只支持当前连接收到的明确好友申请")
        if not 0 <= checked.scene <= 255:
            raise ValueError("此版本好友通过接口只支持 0–255 的申请场景值")
        if any(not 1 <= len(value.encode("utf-8")) <= 8191
               for value in (checked.encrypted_username, checked.ticket)):
            raise ValueError("好友申请凭据超过原生接口支持范围")
        target, text = "", "wxbot.friend.accept"
    elif friend_request is not None or friend_probe:
        raise ValueError("好友申请参数仅用于好友申请操作")
    managing_group = mode == "--manage-group"
    if managing_group:
        if (not generic or group_operation not in ("invite", "create") or type(group_probe) is not bool or
                image_path is not None or image_probe or download_image is not None or
                file_path is not None or file_probe or download_file is not None or
                voice_path is not None or voice_probe or voice_duration_ms is not None or
                emoticon_path is not None or emoticon_probe or mentions is not None):
            raise ValueError("群管理需要明确操作和好友列表，不能混用媒体发送参数")
        from wxbot_framework.account import validate_account
        from wxbot_framework.group_management import validate_group_id, validate_group_members
        account = validate_account(account)
        group_members = validate_group_members(group_members, creating=group_operation == "create", self_wxid=account)
        target = validate_group_id(target) if group_operation == "invite" else ""
        text = "wxbot.group.management"
    elif group_operation is not None or group_members is not None or group_probe:
        raise ValueError("群管理参数仅用于群管理操作")
    downloading_file = mode == "--download-file"
    if downloading_file:
        if (not generic or not isinstance(download_file, dict) or download_image is not None or
                file_path is not None or file_probe or image_path is not None or image_probe or
                voice_path is not None or voice_probe or voice_duration_ms is not None or
                emoticon_path is not None or emoticon_probe or mentions is not None):
            raise ValueError("文件下载需要通用桥接和完整下载参数，不能混用发送参数")
        from wxbot_framework.account import validate_account
        from wxbot_framework.files import MAX_FILE_BYTES
        account = validate_account(account)
        destination = Path(download_file["destination"]).resolve()
        if destination.exists() or destination.is_symlink() or not destination.parent.is_dir():
            raise ValueError("文件下载路径必须是现有目录内的全新文件")
        filekey, aeskey = download_file["filekey"], download_file["aeskey"]
        if (not isinstance(filekey, str) or not 1 <= len(filekey.encode("utf-8")) <= 1023 or
                any(ord(ch) < 32 or ord(ch) == 127 for ch in filekey)):
            raise ValueError("文件 CDN filekey 无效")
        if (not isinstance(aeskey, str) or len(aeskey) not in (32, 64) or
                any(ch not in "0123456789abcdefABCDEF" for ch in aeskey)):
            raise ValueError("文件 CDN AES 参数无效")
        expected_size = download_file.get("expected_size")
        if type(expected_size) is not int or not 1 <= expected_size <= MAX_FILE_BYTES:
            raise ValueError("文件下载需要非零且在系统支持范围内的声明大小")
        text = str(destination)
    elif download_file is not None:
        raise ValueError("文件下载参数仅用于文件下载操作")
    if type(file_probe) is not bool:
        raise ValueError("文件检查标志必须是布尔值")
    if file_path is not None:
        if (not generic or mode != "--dispatch" or mentions is not None or
                image_path is not None or image_probe or emoticon_path is not None or emoticon_probe or
                voice_path is not None or voice_duration_ms is not None or voice_probe or download_image is not None):
            raise ValueError("文件附件仅用于通用文件发送或检查，不能混用其他发送参数")
        from wxbot_framework.account import validate_account
        from wxbot_framework.files import inspect_file
        account = validate_account(account)
        text = str(inspect_file(file_path).path)
    elif file_probe:
        raise ValueError("文件检查参数需要 file_path")
    downloading = mode == "--download-image"
    if downloading:
        if (not generic or not isinstance(download_image, dict) or image_path is not None or
                emoticon_path is not None or mentions is not None or voice_path is not None or
                voice_duration_ms is not None or voice_probe):
            raise ValueError("图片下载需要通用桥接和完整下载参数")
        destination = Path(download_image["destination"]).resolve()
        if destination.exists() or not destination.parent.is_dir():
            raise ValueError("图片下载路径必须是现有目录内的全新文件")
        filekey, aeskey = download_image["filekey"], download_image["aeskey"]
        if (not isinstance(filekey, str) or not 1 <= len(filekey.encode("utf-8")) <= 1023 or
                any(ord(ch) < 32 for ch in filekey) or "\0" in filekey):
            raise ValueError("图片 CDN filekey 无效")
        if (not isinstance(aeskey, str) or len(aeskey) not in (32, 64) or
                any(ch not in "0123456789abcdefABCDEF" for ch in aeskey)):
            raise ValueError("图片 CDN AES 参数无效")
        image_type = download_image.get("image_type", 2)
        if type(image_type) is not int or image_type not in (1, 2, 3):
            raise ValueError("图片下载类型无效")
        text = str(destination)
    elif download_image is not None:
        raise ValueError("下载参数仅用于图片下载操作")
    if type(voice_probe) is not bool:
        raise ValueError("语音检查标志必须是布尔值")
    if voice_path is not None:
        if (not generic or mode != "--dispatch" or mentions is not None or
                image_path is not None or image_probe or emoticon_path is not None or emoticon_probe):
            raise ValueError("语音文件仅用于通用语音发送或检查，不能混用其他发送参数")
        from wxbot_framework.account import validate_account
        from wxbot_framework.voices import inspect_voice_file, TENCENT_SILK_HEADER
        account = validate_account(account)
        voice = inspect_voice_file(voice_path)
        if (type(voice_duration_ms) is not int or not 0 < voice_duration_ms <= 60000 or
                voice_duration_ms % 20 or voice.duration_ms != voice_duration_ms):
            raise ValueError("语音毫秒时长必须与 SILK 文件一致，且不超过 60 秒")
        with voice.path.open("rb") as source:
            if source.read(len(TENCENT_SILK_HEADER)) != TENCENT_SILK_HEADER:
                raise ValueError("原生语音入口需要 prepare_voice 生成的 Tencent SILK 文件")
        text = str(voice.path)
    elif voice_probe or voice_duration_ms is not None:
        raise ValueError("语音检查和时长参数需要 voice_path")
    if type(emoticon_probe) is not bool:
        raise ValueError("表情路径检查标志必须是布尔值")
    if emoticon_path is not None:
        if (not generic or mode != "--dispatch" or mentions is not None or
                image_path is not None or image_probe or voice_path is not None):
            raise ValueError("表情文件仅用于通用表情发送或路径检查，不能与其他发送参数混用")
        from wxbot_framework.account import validate_account
        account = validate_account(account)
        from wxbot_framework.emoticons import validate_emoticon_path
        text = str(validate_emoticon_path(emoticon_path))
    elif emoticon_probe or (account is not None and voice_path is None and file_path is None and
                           not downloading_file and not managing_group and not accepting_friend):
        raise ValueError("表情路径检查和账号参数需要 emoticon_path")
    if image_path is not None:
        if not generic or mode != "--dispatch" or mentions is not None:
            raise ValueError("图片文件仅用于通用图片发送或路径检查")
        from wxbot_framework.images import validate_image_path
        text = str(validate_image_path(image_path))
    elif image_probe:
        raise ValueError("图片路径检查需要 image_path")
    if generic and mode not in ("--dispatch", "--listen", "--remove", "--probe-at", "--download-image", "--download-file", "--manage-group", "--accept-friend"):
        raise ValueError("通用桥接只支持文本、图片与表情、监听和卸载")
    if not generic and target != "filehelper":
        raise ValueError("旧自测桥接仅支持文件传输助手")
    if generic and not managing_group and not accepting_friend:
        from wxbot_framework.text import validate_target
        validate_target(target)
    if mentions is not None:
        if not generic or mode != "--dispatch":
            raise ValueError("艾特成员列表仅用于通用文本发送")
        from wxbot_framework.text import validate_mentions
        mentions = validate_mentions(target, mentions)
    if mode not in ("--arm", "--dispatch", "--receive", "--listen", "--remove", "--probe-at", "--download-image", "--download-file", "--manage-group", "--accept-friend"):
        raise RuntimeError("不支持的原生桥接操作")
    if mode == "--probe-at" and not generic:
        raise ValueError("艾特探针仅用于通用桥接")
    if keep_loaded and mode not in ("--dispatch", "--download-image", "--download-file", "--manage-group", "--accept-friend"):
        raise ValueError("keep_loaded 只用于接收服务内的发送")
    if c.sizeof(PTR) != 8: raise RuntimeError("需要 64 位 Python")
    encoded = text.encode("utf-8")
    if not encoded or len(encoded) > 1023 or b"\0" in encoded:
        raise ValueError("文本或图片路径应为 1–1023 个 UTF-8 字节，且不能包含 NUL")
    pid = pid or find_wechat()
    process = check(K.OpenProcess(0x43a, False, pid), "打开已验证的微信进程")
    allocations = []
    pending_thread = False
    try:
        path = c.create_unicode_buffer(32768); length = w.DWORD(len(path))
        check(K.QueryFullProcessImageNameW(process, 0, path, c.byref(length)), "核对客户端路径")
        client = Path(path.value)
        if client.name.lower() != "weixin.exe": raise RuntimeError("目标不是微信")
        version = pefile.PE(str(client)).VS_FIXEDFILEINFO[0]
        from wxbot_framework.native_profiles import CORE_PROFILES
        supported_versions = set()
        for profile in CORE_PROFILES:
            major, minor, build, patch = map(int, profile.version.split('.'))
            supported_versions.add(((major << 16) | minor, (build << 16) | patch))
        if (version.FileVersionMS, version.FileVersionLS) not in supported_versions:
            raise RuntimeError("客户端版本已改变")
        loaded_modules = modules(pid)
        core = loaded_modules.get("weixin.dll")
        if not core: raise RuntimeError("微信核心模块尚未加载")
        baseline = json.loads((ROOT / "native/compatibility.json").read_text(encoding="utf-8"))
        if compatible_core(core[1], baseline) is None:
            raise RuntimeError("微信核心 DLL 指纹不匹配，拒绝加载")
        if mode == "--remove" and dll.name.lower() not in loaded_modules:
            return {"mode": mode, "pid": pid, "code": 0, "dll_unloaded": True, "already_unloaded": True}
        other = DLL if generic else TEXT_DLL
        if mode != "--remove" and other.name.lower() in loaded_modules:
            raise RuntimeError("另一版本的桥接仍在运行，请先在旧 main.py 中输入 quit")
        pe = pefile.PE(str(dll))
        name = b"WxInspectFriendRequest" if accepting_friend else b"WxInspectGroupManagement" if managing_group else b"WxDownloadFile" if downloading_file else b"WxDownloadImage" if downloading else b"WxArmMentionProbe" if mode == "--probe-at" else {
            "--arm":b"WxArmSelfTest", "--dispatch":b"WxDispatchSelfTest", "--receive":b"WxArmReceiveTest",
            "--listen":b"WxStartFileHelperReceiver", "--remove":b"WxRemoveSelfTest"}[mode]
        if generic:
            name = {"--dispatch": b"WxSendText", "--listen": b"WxStartTextReceiver",
                    "--remove": b"WxRemoveSelfTest", "--probe-at": b"WxArmMentionProbe",
                    "--download-image": b"WxDownloadImage", "--download-file": b"WxDownloadFile",
                    "--manage-group": b"WxInspectGroupManagement", "--accept-friend": b"WxInspectFriendRequest"}[mode]
            if accepting_friend:
                name = b"WxInspectFriendRequest" if friend_probe else b"WxAcceptFriendRequest"
            if managing_group:
                name = (b"WxInspectGroupManagement" if group_probe else
                        b"WxInviteGroupMembers" if group_operation == "invite" else b"WxCreateGroup")
            if mentions is not None:
                name = b"WxSendAtText"
            if image_path is not None:
                name = b"WxInspectImagePath" if image_probe else b"WxSendImage"
            if emoticon_path is not None:
                name = b"WxInspectEmoticonPath" if emoticon_probe else b"WxSendEmoticon"
            if voice_path is not None:
                name = b"WxInspectVoicePath" if voice_probe else b"WxSendVoice"
            if file_path is not None:
                name = b"WxInspectFilePath" if file_probe else b"WxSendFile"
        export = next(item.address for item in pe.DIRECTORY_ENTRY_EXPORT.symbols if item.name == name)

        def allocate(data):
            pointer = check(K.VirtualAllocEx(process, None, len(data), 0x3000, 0x04), "分配自测参数")
            allocations.append(pointer)
            buffer = c.create_string_buffer(data); written = c.c_size_t()
            check(K.WriteProcessMemory(process, pointer, buffer, len(data), c.byref(written)), "写入自测参数")
            if written.value != len(data): raise RuntimeError("参数写入不完整")
            return pointer

        if mode in ("--arm", "--dispatch", "--receive", "--listen", "--probe-at", "--download-image", "--download-file", "--manage-group", "--accept-friend"):
            existing = dll.name.lower() in loaded_modules
            if existing and not keep_loaded: raise RuntimeError("桥接 DLL 已加载，请先清理")
            if not existing:
                if keep_loaded: raise RuntimeError("接收服务的桥接 DLL 已卸载")
                dll_parameter = allocate((str(dll) + "\0").encode("utf-16-le"))
                remote_call(process, remote_system_function(pid, b"LoadLibraryW"), dll_parameter)
        loaded = modules(pid).get(dll.name.lower())
        if not loaded or Path(loaded[1]).resolve() != dll: raise RuntimeError("桥接 DLL 未正确加载")
        report_path = ROOT / f".runtime/native_requests/{uuid.uuid4().hex}.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        if len(str(report_path)) >= 260: raise RuntimeError("报告路径超过原生接口长度")
        config = (FriendRequestConfig() if accepting_friend else GroupManagementConfig() if managing_group else FileDownloadConfig() if downloading_file else FileConfig() if file_path is not None else VoiceConfig() if voice_path is not None else EmoticonConfig() if emoticon_path is not None else ImageDownloadConfig() if downloading else
                  AtTextConfig() if mentions is not None else TextConfig() if generic else Config())
        config.size, config.pid, config.manager = c.sizeof(config), pid, 0
        config.log_path, config.text = str(report_path), encoded
        if generic and not downloading and not downloading_file:
            config.target = target.encode("ascii")
        if downloading:
            config.filekey = filekey.encode("utf-8")
            config.aeskey = aeskey.encode("ascii")
            config.image_type = download_image.get("image_type", 2)
        if downloading_file:
            config.filekey = filekey.encode("utf-8")
            config.aeskey = aeskey.encode("ascii")
            config.expected_size = expected_size
            config.account = account.encode("ascii")
        if managing_group:
            config.account = account.encode("ascii")
            config.members = ",".join(group_members).encode("ascii")
            config.operation = 1 if group_operation == "invite" else 2
        if accepting_friend:
            config.account = account.encode("ascii")
            config.v3 = friend_request.encrypted_username.encode("utf-8")
            config.v4 = friend_request.ticket.encode("utf-8")
            config.scene = friend_request.scene
        if mentions is not None:
            config.members = ",".join(mentions).encode("ascii")
        if emoticon_path is not None:
            config.account = account.encode("ascii")
            config.send = 0 if emoticon_probe else 1
        if voice_path is not None:
            config.account = account.encode("ascii")
            config.duration_ms = voice_duration_ms
            config.send = 0 if voice_probe else 1
        if file_path is not None:
            config.account = account.encode("ascii")
            config.send = 0 if file_probe else 1
        config_parameter = allocate(bytes(config))
        exit_code = remote_call(process, loaded[0] + export, config_parameter)
        if mode == "--remove" and exit_code == 0:
            remote_call(process, remote_system_function(pid, b"FreeLibrary"), loaded[0])
        # Failed initialization or a partially scheduled job must still pass
        # the DLL's cleanup guard before FreeLibrary is allowed.
        result = {"mode":mode,"pid":pid,"code":exit_code,"target":target,"report_path":str(report_path)}
        if mode == "--remove":
            result["dll_unloaded"] = dll.name.lower() not in modules(pid)
        if mode in ("--dispatch", "--download-image", "--download-file", "--manage-group", "--accept-friend"):
            deadline = time.monotonic() + (120 if downloading or downloading_file or managing_group or accepting_friend or emoticon_path is not None or voice_path is not None or file_path is not None else 15)
            while exit_code == 0 and not report_path.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            if report_path.exists():
                # The completion closes the file before its callback lease is
                # released. Cleanup below waits for that lease before unload.
                for attempt in range(20):
                    try:
                        result["report"] = json.loads(report_path.read_text(encoding="utf-8")); break
                    except (json.JSONDecodeError, PermissionError): time.sleep(0.05)
            result["report_path"] = str(report_path)
            cleanup_name = b"WxWaitForTasks" if keep_loaded else b"WxRemoveSelfTest"
            cleanup_export = next(item.address for item in pe.DIRECTORY_ENTRY_EXPORT.symbols if item.name == cleanup_name)
            cleanup_code = remote_call(process, loaded[0] + cleanup_export, None)
            result["cleanup_code"] = cleanup_code
            if cleanup_code == 0 and not keep_loaded:
                remote_call(process, remote_system_function(pid, b"FreeLibrary"), loaded[0])
                result["dll_unloaded"] = dll.name.lower() not in modules(pid)
            else:
                result["dll_unloaded"] = False
            result["success"] = (exit_code == 0 and result.get("report", {}).get("completion_success") is True
                                 and (True if managing_group or accepting_friend else (result["report"].get("file_path") == text) if downloading_file else
                                      (result["report"].get("image_path") == text) if downloading else
                                      (result["report"].get("target") == target and result["report"].get("text") == text))
                                 and cleanup_code == 0 and (keep_loaded or result.get("dll_unloaded") is True))
            if emoticon_path is not None:
                report = result.get("report", {})
                result["success"] = (result["success"] and report.get("message_type") == 47 and
                    report.get("emoticon_path") == text and report.get("sent") is (not emoticon_probe) and
                    report.get("operation") == ("emoticon_probe" if emoticon_probe else "emoticon_send"))
            if voice_path is not None:
                report = result.get("report", {})
                result["success"] = (result["success"] and report.get("message_type") == 34 and
                    report.get("voice_path") == text and report.get("duration_ms") == voice_duration_ms and
                    report.get("sent") is (not voice_probe) and
                    report.get("operation") == ("voice_probe" if voice_probe else "voice_send"))
            if file_path is not None:
                report = result.get("report", {})
                result["success"] = (result["success"] and report.get("message_type") == 49 and
                    report.get("app_type") == 6 and report.get("file_path") == text and
                    report.get("sent") is (not file_probe) and
                    report.get("operation") == ("file_probe" if file_probe else "file_send"))
            if downloading_file:
                report = result.get("report", {})
                result["success"] = (result["success"] and report.get("operation") == "file_download" and
                                     report.get("file_size") == expected_size)
            if managing_group:
                report = result.get("report", {})
                operation = "group_probe" if group_probe else "group_" + group_operation
                result["success"] = (result["success"] and report.get("operation") == operation and
                    report.get("mode") == config.operation and report.get("sent") is (not group_probe) and
                    report.get("member_count") == len(group_members))
                if result["success"] and group_operation == "invite":
                    result["success"] = report.get("room_id") == target
                elif result["success"] and not group_probe:
                    try:
                        validate_group_id(report.get("room_id"))
                    except ValueError:
                        result["success"] = False
            if accepting_friend:
                report = result.get("report", {})
                result["success"] = (result["success"] and
                    report.get("operation") == ("friend_probe" if friend_probe else "friend_accept") and
                    report.get("accepted") is (not friend_probe) and
                    report.get("sent") is (not friend_probe) and report.get("scene") == friend_request.scene)
            if mentions is not None:
                result["success"] = result["success"] and result["report"].get("at_members") == ",".join(mentions)
            if not preserve_report and result["success"]:
                # 长期运行只保留内存中的完成结果；删除本次已结束的临时报告。
                try:
                    report_path.unlink(missing_ok=True)
                    result["report_retained"] = False
                except OSError as error:
                    # 临时文件被占用不应把已经成功的发送变成失败。
                    result["report_retained"] = True
                    result["report_cleanup_error"] = str(error)
        return result
    except (TimeoutError, KeyboardInterrupt):
        # A timed-out remote thread may still be using these buffers. Keep them
        # allocated in the client until it exits instead of freeing live input.
        pending_thread = True
        raise
    finally:
        if not pending_thread:
            for pointer in allocations: K.VirtualFreeEx(process, pointer, 0, 0x8000)
        K.CloseHandle(process)


if __name__ == "__main__":
    # 直接从 native 目录启动时，也要能导入共享的构建配置。
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    parser = argparse.ArgumentParser(description="通过微信原生任务队列给文件传输助手发送文本")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--arm", action="store_true")
    action.add_argument("--dispatch", action="store_true")
    action.add_argument("--remove", action="store_true")
    action.add_argument("--receive", action="store_true")
    action.add_argument("--listen", action="store_true", help="启用文件传输助手接收队列，结束后需 --remove")
    parser.add_argument("--pid", type=int)
    parser.add_argument("--text", default="WXNATIVE_TEST_3")
    options = parser.parse_args()
    mode = "--arm" if options.arm else "--dispatch" if options.dispatch else "--receive" if options.receive else "--listen" if options.listen else "--remove"
    outcome = run(mode, pid=options.pid, text=options.text)
    print(json.dumps(outcome, ensure_ascii=False), flush=True)
    sys.exit(0 if outcome.get("success", outcome["code"] == 0) else 1)
