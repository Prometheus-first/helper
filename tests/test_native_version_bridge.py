"""原生双版本迁移回归：精确指纹拒绝与旧 RVA 残留，不连接微信。"""
from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
# Independent frozen migration contract from instruction/CFG + caller/constructor
# comparison. Do not derive expected offsets from the production dispatch table.
VERIFIED_RVAS = {
    0x7a60: 0x7a60,
    0x45d10: 0x45d10,
    0x47220: 0x47220,
    0x47310: 0x47310,
    0x857a0: 0x857a0,
    0x88ad0: 0x88ad0,
    0x8edb0: 0x8edb0,
    0x9b5d0: 0x9b5e0,
    0x9f4b0: 0x9f4c0,
    0x10e390: 0x113db0,
    0x2fb010: 0x30f450,
    0x356dd0: 0x36ca60,
    0x36c740: 0x3823d0,
    0x389830: 0x39f520,
    0x4e6360: 0x4fb6e0,
    0x5309c0: 0x545d40,
    0x57fe90: 0x595210,
    0x6db150: 0x6ef640,
    0x6f4c70: 0x709160,
    0x6f4d90: 0x709280,
    0x754330: 0x768820,
    0x7544b0: 0x7689a0,
    0xe5c720: 0xe83e20,
    0xe7a8e0: 0xea1fe0,
    0x16da4f0: 0x16eea90,
    0x16f3390: 0x17073a0,
    0x178f100: 0x17a3c20,
    0x17b9010: 0x17cde20,
    0x1984610: 0x1999b70,
    0x198e2e0: 0x19a3840,
    0x1a26440: 0x1a3bb90,
    0x1bfb1c0: 0x1c12a00,
    0x229e790: 0x230b9a0,
    0x22d8390: 0x23455a0,
    0x2ba5330: 0x2c15960,
    0x2ba7940: 0x2c17f60,
    0x2bab360: 0x2c1b980,
    0x3866510: 0x38bdda0,
    0x3d9d990: 0x3e22d70,
    0x5240720: 0x5380ef0,
    0x54b02f0: 0x55f0380,
    0x6be0020: 0x6cd09b0,
    0x74ab89c: 0x754bcdc,
    0x74ab8e0: 0x754bd20,
    0x8d804e8: 0x8f514e8,
    0x8d9f958: 0x8f71468,
    0x8dfb4b8: 0x8fce618,
    0x8dfe2f8: 0x8fd1468,
    0x8e53588: 0x9026b48,
    0x8e53718: 0x9026cd8,
    0x90daff8: 0x92b2d08,
    0x910b378: 0x92e30f8,
    0x97ca198: 0x99cb6f8,
    0x9af9078: 0x9cff818,
    0xb912690: 0xbb92fa0,
}
VERIFIED_BUILDS = {
    "4.1.15.13": (201552944, "10f8e995453e2da46d4f2b5080cd6da1f13cc5147746adc119ceae38cb039de5",
                   0x6AB0A378, 0xC0AE000),
    "4.1.15.56": (204305968, "e0428a7b691853de496532523c2c09aa095f90941c37c9f4cfb3f137f3c66556",
                   0x6AD083C8, 0xC34F000),
}


def source(name):
    return (ROOT / "native" / name).read_text(encoding="utf-8-sig")


def code_without_comments(text):
    return re.sub(r"/\*.*?\*/|//[^\n]*", "", text, flags=re.S)


def pure_compatibility_function():
    """Run the actual pure verifier without importing Win32/injection helpers."""
    node = next(item for item in ast.parse(source("run_selftest.py")).body
                if isinstance(item, ast.FunctionDef) and item.name == "compatible_core")
    namespace = {"Path": Path, "hashlib": hashlib}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "compatible_core-only", "exec"), namespace)
    return namespace["compatible_core"]


class NativeVersionBridgeTests(unittest.TestCase):
    def test_public_compatibility_metadata_pins_both_exact_builds(self):
        baseline = json.loads(source("compatibility.json"))
        rows = baseline["profiles"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["wechat_version"] for row in rows}, set(VERIFIED_BUILDS))
        for row in rows:
            size, digest, _, _ = VERIFIED_BUILDS[row["wechat_version"]]
            self.assertIs(type(row["size"]), int)
            self.assertEqual((row["size"], row["sha256"], row["architecture"]), (size, digest, "x64"))

    def test_native_fingerprint_table_matches_independent_artifact_identity(self):
        text = source("core_version.h")
        rows = re.findall(r"\{([12]),\s*(\d+)ull,\s*(0x[0-9a-f]+),\s*(0x[0-9a-f]+),\s*\{([^}]+)\}\}", text)
        self.assertEqual(len(rows), 2)
        for build_id, size, stamp, image_size, digest_bytes in rows:
            expected = VERIFIED_BUILDS["4.1.15.13" if build_id == "1" else "4.1.15.56"]
            digest = bytes(int(value, 16) for value in re.findall(r"0x[0-9a-f]+", digest_bytes)).hex()
            self.assertEqual((int(size), digest, int(stamp, 16), int(image_size, 16)), expected)

    def test_hash_and_size_must_belong_to_the_same_actual_bytes(self):
        verify = pure_compatibility_function()
        payload = b"synthetic-known-core-build\x00\xff"
        row = {"size": len(payload), "sha256": hashlib.sha256(payload).hexdigest()}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Weixin.dll"
            path.write_bytes(payload)
            self.assertIs(verify(path, {"profiles": [row]}), row)
            for bad in ({**row, "size": len(payload) + 1},
                        {**row, "sha256": "f" * 64},
                        {**row, "size": True}, {**row, "size": str(len(payload))},
                        {"sha256": row["sha256"]}):
                with self.subTest(profile=bad):
                    self.assertIsNone(verify(path, {"profiles": [bad]}))
            path.write_bytes(payload[:-1] + b"!")
            self.assertIsNone(verify(path, {"profiles": [row]}))
            path.write_bytes(payload + b"changed-size")
            self.assertIsNone(verify(path, {"profiles": [row]}))

    def test_invalid_profile_container_cannot_read_any_candidate_file(self):
        verify = pure_compatibility_function()
        for baseline in ({"profiles": []}, {"profiles": {}}, {"profiles": [None]},
                         {"profiles": [1]}, {"profiles": ["4.1.15.56"]}):
            with self.subTest(baseline=baseline), patch.object(Path, "read_bytes", side_effect=AssertionError("unexpected file read")):
                self.assertIsNone(verify("unused.dll", baseline))

    def test_native_mapping_is_exact_verified_set_and_has_no_duplicate_case(self):
        rows = re.findall(r"case\s+(0x[0-9a-f]+):\s*return\s+(0x[0-9a-f]+);", source("core_version.h"))
        self.assertEqual(len(rows), len(VERIFIED_RVAS))
        self.assertEqual(len({old for old, _ in rows}), len(rows))
        self.assertEqual({int(old, 16): int(new, 16) for old, new in rows}, VERIFIED_RVAS)
        self.assertEqual(len(VERIFIED_RVAS), 55)

    def test_all_production_module_relative_references_use_verified_profile_resolver(self):
        used = set()
        for path in sorted((ROOT / "native").iterdir()):
            if path.suffix not in (".h", ".cpp") or path.name == "core_version.h":
                continue
            text = code_without_comments(path.read_text(encoding="utf-8-sig"))
            # This also catches raw new-version constants: no direct code/vtable
            # module addition is allowed outside the common checked resolver.
            self.assertIsNone(re.search(r"\b(?:core_base|base)\s*\+\s*0x[0-9a-f]+", text), path.name)
            for old in re.findall(r"core_(?:address\([^,]+,\s*|rva\()(0x[0-9a-f]+)\)", text):
                used.add(int(old, 16))
            self.assertIsNone(re.search(r"vtable_rva\s*!=\s*0x[0-9a-f]+", text), path.name)
        self.assertEqual(used, set(VERIFIED_RVAS))

    def test_hook_cleanup_uses_the_same_canonical_targets_as_install(self):
        text = code_without_comments(source("selftest_bridge.cpp"))
        removal = text.split("DWORD WINAPI WxRemoveSelfTest(void*)", 1)[1].split("BOOL WINAPI DllMain", 1)[0]
        targets = {int(value, 16) for value in re.findall(r"core_address\(core_base,\s*(0x[0-9a-f]+)\)", removal)}
        self.assertEqual(targets, {0x178F100, 0x17B9010, 0x229E790, 0x3866510, 0x6F4C70, 0x5240720})
        install = "\n".join(code_without_comments(source(name)) for name in
                            ("selftest_bridge.cpp", "text_bridge.cpp", "friend_receive.h", "image_download.h"))
        for legacy in targets:
            self.assertGreaterEqual(len(re.findall(r"core_address\(core_base,\s*" + hex(legacy) + r"\)", install)), 2)
        self.assertNotRegex(removal, r"core_profile_id\s*=|InterlockedExchange\([^,]*core_profile_id")

    def test_unknown_native_profile_or_unmapped_new_target_has_no_legacy_fallback(self):
        text = code_without_comments(source("core_version.h"))
        self.assertRegex(text, r"if\s*\(profile\s*!=\s*2\)\s*return\s+0;")
        self.assertRegex(text, r"default:\s*return\s+0;")
        self.assertRegex(text, r"return\s+rva\s*\?\s*base\s*\+\s*rva\s*:\s*0;")
        fingerprint = source("selftest_bridge.cpp").split("static bool core_fingerprint", 1)[1].split('extern "C"', 1)[0]
        self.assertIn("core_image_identity(core, &timestamp, &image_size)", fingerprint)
        self.assertIn("core_select_build(static_cast<uint64_t>(size.QuadPart), digest, timestamp, image_size)", fingerprint)


@unittest.skipUnless(sys.platform == "win32", "Win32 loading preflight, fully mocked process I/O")
class NativeUnknownBuildPreflightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "native"))
        import run_selftest
        cls.native = run_selftest

    def test_unknown_core_is_rejected_before_allocating_or_loading_any_bridge(self):
        native = self.native
        with tempfile.TemporaryDirectory() as directory:
            unknown = Path(directory) / "Weixin.dll"
            unknown.write_bytes(b"unknown core, never a permitted target")
            def client_path(process, flags, output, length):
                output.value = str(Path(directory) / "Weixin.exe")
                return 1
            version = SimpleNamespace(FileVersionMS=(4 << 16) | 1, FileVersionLS=(15 << 16) | 56)
            with patch.object(native.K, "OpenProcess", return_value=123), \
                 patch.object(native.K, "QueryFullProcessImageNameW", side_effect=client_path), \
                 patch.object(native.K, "CloseHandle", return_value=1) as closed, \
                 patch.object(native.K, "VirtualAllocEx", side_effect=AssertionError("must not allocate")) as allocated, \
                 patch.object(native.K, "WriteProcessMemory", side_effect=AssertionError("must not write")) as wrote, \
                 patch.object(native, "remote_call", side_effect=AssertionError("must not execute/inject")) as called, \
                 patch.object(native, "modules", return_value={"weixin.dll": (0x180000000, str(unknown))}), \
                 patch.object(native.pefile, "PE", return_value=SimpleNamespace(VS_FIXEDFILEINFO=[version])):
                with self.assertRaisesRegex(RuntimeError, "指纹不匹配"):
                    native.run("--listen", pid=999999, bridge_path=native.TEXT_DLL)
                allocated.assert_not_called()
                wrote.assert_not_called()
                called.assert_not_called()
                closed.assert_called_once_with(123)


if __name__ == "__main__":
    unittest.main()
