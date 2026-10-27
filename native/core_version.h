#pragma once
// Canonical RVAs name the verified 4.1.15.13 targets. Select only a complete,
// hash-verified build, then resolve every call, vtable check and hook cleanup.
struct CoreBuild {
    LONG id;
    uint64_t bytes;
    uint32_t timestamp, image_size;
    unsigned char sha256[32];
};
static constexpr CoreBuild core_builds[] = {
    {1, 201552944ull, 0x6ab0a378, 0xc0ae000, {0x10,0xf8,0xe9,0x95,0x45,0x3e,0x2d,0xa4,0x6d,0x4f,0x2b,0x50,0x80,0xcd,0x6d,0xa1,0xf1,0x3c,0xc5,0x14,0x77,0x46,0xad,0xc1,0x19,0xce,0xae,0x38,0xcb,0x03,0x9d,0xe5}},
    {2, 204305968ull, 0x6ad083c8, 0xc34f000, {0xe0,0x42,0x8a,0x7b,0x69,0x18,0x53,0xde,0x49,0x65,0x32,0x52,0x3c,0x2c,0x09,0xaa,0x09,0x5f,0x90,0x94,0x1c,0x37,0xc9,0xf4,0xcf,0xb3,0xf1,0x37,0xf3,0xc6,0x65,0x56}}
};
static volatile LONG core_profile_id = 0;
static bool core_size_supported(uint64_t bytes) {
    for (const auto& build: core_builds) if (build.bytes == bytes) return true;
    return false;
}
static bool core_select_build(uint64_t bytes, const unsigned char* digest,
                              uint32_t timestamp, uint32_t image_size) {
    for (const auto& build: core_builds) {
        if (build.bytes == bytes && build.timestamp == timestamp && build.image_size == image_size &&
                std::equal(build.sha256, build.sha256+32, digest)) {
            InterlockedExchange(&core_profile_id, build.id);
            return true;
        }
    }
    return false;
}
static uintptr_t core_rva(uintptr_t legacy) {
    const LONG profile = InterlockedCompareExchange(&core_profile_id, 0, 0);
    if (profile == 1) return legacy;
    if (profile != 2) return 0;
    switch (legacy) {
        case 0x7a60: return 0x7a60;
        case 0x45d10: return 0x45d10;
        case 0x47220: return 0x47220;
        case 0x47310: return 0x47310;
        case 0x857a0: return 0x857a0;
        case 0x88ad0: return 0x88ad0;
        case 0x8edb0: return 0x8edb0;
        case 0x9b5d0: return 0x9b5e0;
        case 0x9f4b0: return 0x9f4c0;
        case 0x10e390: return 0x113db0;
        case 0x2fb010: return 0x30f450;
        case 0x356dd0: return 0x36ca60;
        case 0x36c740: return 0x3823d0;
        case 0x389830: return 0x39f520;
        case 0x4e6360: return 0x4fb6e0;
        case 0x5309c0: return 0x545d40;
        case 0x57fe90: return 0x595210;
        case 0x6db150: return 0x6ef640;
        case 0x6f4c70: return 0x709160;
        case 0x6f4d90: return 0x709280;
        case 0x754330: return 0x768820;
        case 0x7544b0: return 0x7689a0;
        case 0xe5c720: return 0xe83e20;
        case 0xe7a8e0: return 0xea1fe0;
        case 0x16da4f0: return 0x16eea90;
        case 0x16f3390: return 0x17073a0;
        case 0x178f100: return 0x17a3c20;
        case 0x17b9010: return 0x17cde20;
        case 0x1984610: return 0x1999b70;
        case 0x198e2e0: return 0x19a3840;
        case 0x1a26440: return 0x1a3bb90;
        case 0x1bfb1c0: return 0x1c12a00;
        case 0x229e790: return 0x230b9a0;
        case 0x22d8390: return 0x23455a0;
        case 0x2ba5330: return 0x2c15960;
        case 0x2ba7940: return 0x2c17f60;
        case 0x2bab360: return 0x2c1b980;
        case 0x3866510: return 0x38bdda0;
        case 0x3d9d990: return 0x3e22d70;
        case 0x5240720: return 0x5380ef0;
        case 0x54b02f0: return 0x55f0380;
        case 0x6be0020: return 0x6cd09b0;
        case 0x74ab89c: return 0x754bcdc;
        case 0x74ab8e0: return 0x754bd20;
        case 0x8d804e8: return 0x8f514e8;
        case 0x8d9f958: return 0x8f71468;
        case 0x8dfb4b8: return 0x8fce618;
        case 0x8dfe2f8: return 0x8fd1468;
        case 0x8e53588: return 0x9026b48;
        case 0x8e53718: return 0x9026cd8;
        case 0x90daff8: return 0x92b2d08;
        case 0x910b378: return 0x92e30f8;
        case 0x97ca198: return 0x99cb6f8;
        case 0x9af9078: return 0x9cff818;
        case 0xb912690: return 0xbb92fa0;

        default: return 0; // No fallback to a stale RVA in an unknown mapping.
    }
}
static uintptr_t core_address(uintptr_t base, uintptr_t legacy) {
    const auto rva = core_rva(legacy);
    return rva ? base + rva : 0;
}

// SNS interaction was verified only in 4.1.15.56. These are native profile-2
// RVAs, not legacy targets; never reuse them in profile 1 or an unknown image.
static uintptr_t core_sns_address(uintptr_t base, uintptr_t rva) {
    if (!base || InterlockedCompareExchange(&core_profile_id, 0, 0) != 2) return 0;
    static constexpr uintptr_t verified[] = {
        0x5737070, 0x8f52418, 0x9a31818, 0xb9ba4b0, 0x95e00d8, 0x9195c08,
        0x754cd50, 0x754cd70, 0xbb94b20, 0x108aee0, 0x49f2f0,
        0x30640f0, 0x3065ac0, 0x43b1800, 0x914e2e8, 0x914e378
    };
    for (auto candidate : verified) if (candidate == rva) return base + rva;
    return 0;
}
