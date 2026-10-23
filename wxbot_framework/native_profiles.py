"""已核对的微信核心指纹与只读账号/缓存地址配置。

指纹用于加载原生桥接前的版本校验；内存选择仅用于尚未注入时的账号发现，
必须同时匹配全局对象、虚表、getter 和字段访问指令，不按版本字符串猜偏移。
"""
from __future__ import annotations

from dataclasses import dataclass
import struct
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class CoreProfile:
    version: str
    size: int
    sha256: str
    rvas: Mapping[int, int]
    emoticon_cache_mask: bytes

    def rva(self, legacy_rva: int) -> int:
        """解析已验证的旧地址；未登记地址必须显式补充，不能假定未改变。"""
        return self.rvas[legacy_rva]


_LEGACY_RVAS = (
    0xB912690, 0x8D804E8, 0x8DFE2F8, 0x469A0, 0x36F930,
    0x36C7B0, 0x8F9BF98, 0x8FA2538, 0x8F9C228, 0x8FA3CB8, 0x8FA3C28,
)
_UPDATED_RVAS = (
    0xBB92FA0, 0x8F514E8, 0x8FD1468, 0x469A0, 0x3855C0,
    0x382440, 0x9171CF8, 0x9178258, 0x9171F88, 0x91799D8, 0x9179948,
)

CORE_PROFILES = (
    CoreProfile("4.1.15.13", 201552944,
                "10f8e995453e2da46d4f2b5080cd6da1f13cc5147746adc119ceae38cb039de5",
                MappingProxyType(dict(zip(_LEGACY_RVAS, _LEGACY_RVAS))),
                bytes.fromhex("c9629058e9481a7d100824e418f6f17b2ceb0ce2d7e96e9dc00d3e8249539b96")),
    CoreProfile("4.1.15.56", 204305968,
                "e0428a7b691853de496532523c2c09aa095f90941c37c9f4cfb3f137f3c66556",
                MappingProxyType(dict(zip(_LEGACY_RVAS, _UPDATED_RVAS))),
                bytes.fromhex("2f209b4bd7379861af92bbbdd82d6d20b08ed928264de62da2c3dbd9b5997580")),
)


def profile_for_fingerprint(size: int, sha256: str) -> CoreProfile | None:
    """只接受大小和 SHA256 同时匹配的已确认构建。"""
    if type(size) is not int or not isinstance(sha256, str):
        return None
    return next((profile for profile in CORE_PROFILES
                 if profile.size == size and profile.sha256 == sha256.lower()), None)


def select_memory_profile(read_memory, core_base: int) -> CoreProfile | None:
    """只读验证已知的账号对象链；未知、未就绪或出现歧义时返回 None。

    不缓存 PID/地址，不读取个人资料。调用方仍须双读自身实际使用的结构，
    并在任何注入/原生调用前独立核对完整 DLL 指纹。
    """
    def read(address, length):
        if not 0x10000 <= address < 0x800000000000 or address + length >= 0x800000000000:
            raise ValueError("核心对象指针未就绪")
        value = read_memory(address, length)
        if len(value) != length:
            raise ValueError("核心对象读取不完整")
        return value

    def ptr(address):
        return struct.unpack("<Q", read(address, 8))[0]

    matches = []
    for profile in CORE_PROFILES:
        rva = profile.rva
        try:
            app = ptr(core_base + rva(0xB912690))
            app_table = ptr(app)
            if app_table != core_base + rva(0x8D804E8):
                continue
            if ptr(app_table + 0x60) != core_base + rva(0x469A0):
                continue
            context = ptr(app + 0x68)
            table = ptr(context)
            if table != core_base + rva(0x8DFE2F8):
                continue
            if ptr(table + 0x18) != core_base + rva(0x36F930):
                continue
            if read(core_base + rva(0x36F930), 5) != b"\x48\x8d\x41\x48\xc3":
                continue
            if (ptr(core_base + rva(0xB912690)) != app or ptr(app) != app_table or
                    ptr(app + 0x68) != context or ptr(context) != table):
                continue
            matches.append(profile)
        except (OSError, ValueError, struct.error):
            continue
    return matches[0] if len(matches) == 1 else None
