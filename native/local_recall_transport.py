"""在当前桥接上设置本机保留开关，不加载或卸载 DLL。"""
import ctypes as c
from pathlib import Path
if __package__:
    from .moments_transport import _Channel, _core_profile, _exports, _native, TEXT_DLL
else:
    from moments_transport import _Channel, _core_profile, _exports, _native, TEXT_DLL


class Config(c.Structure):
    _fields_ = [('size', c.c_uint32), ('pid', c.c_uint32), ('enabled', c.c_uint32),
                ('reserved', c.c_uint32), ('account', c.c_char * 128)]


assert c.sizeof(Config) == 144


def set_local_recall(pid, account, enabled, *, bridge_path=TEXT_DLL):
    from wxbot_framework.account import validate_account, read_current_account
    account = validate_account(account)
    if type(pid) is not int or not 0 < pid <= 0xffffffff or type(enabled) is not bool:
        raise ValueError("本机防撤回参数无效")
    bridge = Path(bridge_path).resolve()
    loaded = _native().modules(pid)
    module, core = loaded.get(bridge.name.lower()), loaded.get('weixin.dll')
    if not module or Path(module[1]).resolve() != bridge or not core:
        raise RuntimeError("本机防撤回需要当前机器人持有原生桥接")
    profile = _core_profile(core[1])
    if profile is None or profile.version != '4.2.0.12':
        raise RuntimeError("本机防撤回目前只适配微信 4.2.0.12")
    exports, _ = _exports(bridge)
    entry = exports.get(b'WxSetLocalRecall')
    if entry is None:
        raise RuntimeError("请停止机器人并更新桥接 DLL，以加载本机防撤回接口")
    channel = _Channel(pid)
    try:
        channel.verify_client()
        if read_current_account(channel.read_memory, core[0]) != account:
            raise RuntimeError("微信账号已变化，取消本机防撤回设置")
        config = Config(size=c.sizeof(Config), pid=pid, enabled=int(enabled), account=account.encode('ascii'))
        parameter = channel.allocate(bytes(config))
        code = channel.call(module[0] + entry, parameter)
        if code:
            raise RuntimeError("本机防撤回设置失败，原生状态码=" + str(code))
        return True
    finally:
        channel.close()
