// Version-locked local-file image preparation and dispatch.
// Included by text_bridge.cpp, after the common task and string helpers.
// Static evidence: Weixin.dll 4.1.15.13, RVAs 3d9d990 / 0857a0 / 178f100.

struct QtStaticUtf16 {
    int32_t references;
    int32_t length;
    uint32_t allocation;
    uint32_t padding;
    int64_t offset;
    wchar_t characters[1024];
};
static_assert(offsetof(QtStaticUtf16, characters) == 24, "Unexpected Qt 5 array header");

static bool image_file_argument(const TextConfig& config, QtStaticUtf16* utf16) {
    size_t length = strnlen_s(config.base.text, sizeof(config.base.text));
    if (!length || length >= sizeof(config.base.text)) return false;
    int count = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, config.base.text,
                                  static_cast<int>(length), utf16->characters, 1023);
    if (count <= 0) return false;
    utf16->characters[count] = 0;
    utf16->references = -1; // Qt's immutable static-array reference marker.
    utf16->length = count;
    utf16->allocation = utf16->padding = 0;
    utf16->offset = offsetof(QtStaticUtf16, characters);
    DWORD attributes = GetFileAttributesW(utf16->characters);
    return attributes != INVALID_FILE_ATTRIBUTES && !(attributes & FILE_ATTRIBUTE_DIRECTORY);
}

static void image_dispatch_report(const TextConfig& config, const TestResult& result,
                                  bool success, bool probe, const char* native_error,
                                  const char* prepared_path, const char* original_path) {
    FILE* log = nullptr;
    if (_wfopen_s(&log, config.base.log_path, L"wb") == 0 && log) {
        std::fprintf(log, "{\"operation\":\"%s\",\"stage\":%lu,\"exception\":%lu,"
                          "\"native_status\":%u,\"completion_success\":%s,\"target\":",
                     probe ? "image_path_probe" : "image_send", result.stage, result.exception,
                     result.native_status, success ? "true" : "false");
        write_json_string(log, config.target);
        std::fputs(",\"text\":", log);
        write_json_string(log, config.base.text);
        std::fputs(",\"native_error\":", log);
        write_json_string(log, native_error);
        std::fputs(",\"prepared_path\":", log);
        write_json_string(log, prepared_path);
        std::fputs(",\"original_path\":", log);
        write_json_string(log, original_path);
        std::fputs(",\"image_path\":", log);
        write_json_string(log, config.base.text);
        std::fputs("}\n", log);
        std::fclose(log);
    }
}

static void image_free_string(SmallString* text, TestResult* report) {
    __try {
        if (text->capacity > 15 && text->capacity < 1024 * 1024) {
            void* allocation = *reinterpret_cast<void**>(text->data);
            size_t size = static_cast<size_t>(text->capacity + 1);
            if (size >= 4096) {
                allocation = *(reinterpret_cast<void**>(allocation) - 1);
                size += 39;
            }
            using Free = void (__fastcall*)(void*, size_t);
            reinterpret_cast<Free>(core_address(core_base, 0x74ab8e0))(allocation, size);
        }
    } __except (record_exception(GetExceptionInformation(), report)) {
    }
}

static DWORD send_image_on_task(const TextConfig& config, bool probe) {
    SharedMessage app{}, context{}, manager{}, message{};
    SmallString roundtrip{};
    roundtrip.capacity = 15;
    alignas(16) unsigned char result[128]{};
    QtStaticUtf16 utf16{};
    TestResult report{};
    char native_error[1024]{};
    char prepared_path[1024]{}, original_path[1024]{};
    bool success = false;
    __try {
        if (!image_file_argument(config, &utf16)) {
            report.native_status = ERROR_FILE_NOT_FOUND;
        } else {
            QtStaticUtf16* qstring = &utf16;
            using ToUtf8 = SmallString* (__fastcall*)(void*, SmallString*);
            reinterpret_cast<ToUtf8>(core_address(core_base, 0x857a0))(&qstring, &roundtrip);
            const char* bytes = roundtrip.capacity > 15 ?
                *reinterpret_cast<const char* const*>(roundtrip.data) : roundtrip.data;
            if (roundtrip.length != std::strlen(config.base.text) ||
                std::memcmp(bytes, config.base.text, roundtrip.length)) {
                report.native_status = ERROR_INVALID_DATA;
            } else if (probe) {
                report.stage = 21;
                success = true;
            } else {
                using GetApp = SharedMessage* (__fastcall*)(SharedMessage*);
                using GetShared = SharedMessage* (__fastcall*)(void*, SharedMessage*);
                reinterpret_cast<GetApp>(core_address(core_base, 0x47220))(&app);
                if (!app.object) RaiseException(ERROR_NOT_READY, 0, 0, nullptr);
                reinterpret_cast<GetShared>(core_address(core_base, 0x36c740))(app.object, &context);
                if (!context.object) RaiseException(ERROR_NOT_READY, 0, 0, nullptr);
                reinterpret_cast<GetShared>(core_address(core_base, 0x6f4d90))(context.object, &manager);
                if (!manager.object) RaiseException(ERROR_NOT_READY, 0, 0, nullptr);
                using ImageFromFile = SharedMessage* (__fastcall*)(void*, SharedMessage*, void*);
                reinterpret_cast<ImageFromFile>(core_address(core_base, 0x3d9d990))(nullptr, &message, &qstring);
                if (!message.object || !message.control) RaiseException(ERROR_INVALID_DATA, 0, 0, nullptr);
                auto object = static_cast<unsigned char*>(message.object);
                struct WideStringView { wchar_t data[8]; uint64_t length, capacity; };
                const auto prepared = reinterpret_cast<const WideStringView*>(object + 0x120);
                const auto original = reinterpret_cast<const WideStringView*>(object + 0x168);
                if (prepared->length && prepared->length < 1024 && prepared->capacity >= prepared->length) {
                    const wchar_t* chars = prepared->capacity > 7 ?
                        *reinterpret_cast<const wchar_t* const*>(prepared->data) : prepared->data;
                    WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, chars, static_cast<int>(prepared->length),
                                        prepared_path, sizeof(prepared_path) - 1, nullptr, nullptr);
                }
                if (original->length && original->length < 1024 && original->capacity >= original->length) {
                    const wchar_t* chars = original->capacity > 7 ?
                        *reinterpret_cast<const wchar_t* const*>(original->data) : original->data;
                    WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, chars, static_cast<int>(original->length),
                                        original_path, sizeof(original_path) - 1, nullptr, nullptr);
                }
                // The image factory allocates an InputTemp path, but its UI caller
                // normally populates that path before handing it to MessageManager.
                // Keep the caller's source intact and let Weixin own the staged copy.
                if (!prepared->length || prepared->length >= 1024 || prepared->capacity < prepared->length)
                    RaiseException(ERROR_INVALID_DATA, 0, 0, nullptr);
                const wchar_t* destination = prepared->capacity > 7 ?
                    *reinterpret_cast<const wchar_t* const*>(prepared->data) : prepared->data;
                if (!CopyFileW(utf16.characters, destination, TRUE))
                    RaiseException(GetLastError(), 0, 0, nullptr);
                if (*reinterpret_cast<uint32_t*>(object + 0x118) != 3 ||
                    !set_native_string(reinterpret_cast<SmallString*>(object + 0xb0), config.target))
                    RaiseException(ERROR_INVALID_DATA, 0, 0, nullptr);
                SharedMessage transferred = message;
                _InterlockedIncrement(reinterpret_cast<volatile LONG*>(
                    static_cast<unsigned char*>(message.control) + 8));
                reinterpret_cast<SendFunction>(core_address(core_base, 0x178f100))(manager.object, result, &transferred, 1);
                report.native_status = *reinterpret_cast<uint32_t*>(result);
                report.native_status_high = *reinterpret_cast<uint32_t*>(result + 4);
                const auto error = reinterpret_cast<const SmallString*>(result + 8);
                if (error->length && error->length < sizeof(native_error) && error->capacity >= error->length) {
                    const char* error_bytes = error->capacity > 15 ?
                        *reinterpret_cast<const char* const*>(error->data) : error->data;
                    std::memcpy(native_error, error_bytes, static_cast<size_t>(error->length));
                }
                report.stage = 21;
                success = report.native_status == 0 && report.native_status_high == 0;
            }
        }
    } __except (record_exception(GetExceptionInformation(), &report)) {
        success = false;
    }
    // Native sends transfer their argument's retained reference, exactly as text does.
    guarded_release_shared(reinterpret_cast<SharedMessage*>(result + 40), &report);
    image_free_string(reinterpret_cast<SmallString*>(result + 8), &report);
    image_free_string(&roundtrip, &report);
    guarded_release_shared(&message, &report);
    guarded_release_shared(&manager, &report);
    guarded_release_shared(&context, &report);
    guarded_release_shared(&app, &report);
    success = success && report.exception == 0;
    image_dispatch_report(config, report, success, probe, native_error, prepared_path, original_path);
    return report.exception ? 5 : (success ? 0 : ERROR_INVALID_DATA);
}

static DWORD schedule_image(void* parameter, bool probe) {
    if (!parameter || live_tasks || live_completions) return ERROR_BUSY;
    const TextConfig config = *static_cast<const TextConfig*>(parameter);
    if (config.base.size != sizeof(TextConfig) || config.base.pid != GetCurrentProcessId() ||
        !valid_target(config.target)) return ERROR_INVALID_PARAMETER;
    HMODULE core = GetModuleHandleW(L"Weixin.dll");
    if (!core || !core_fingerprint(core)) return 4;
    core_base = reinterpret_cast<uintptr_t>(core);
    try {
        auto lease = std::make_shared<TextTaskLease>(config);
        std::function<void()> task = [lease, probe]() {
            _InterlockedIncrement(&active_callbacks);
            __try { send_image_on_task(lease->config, probe); }
            __finally { _InterlockedDecrement(&active_callbacks); }
        };
        return schedule_task(config.base, &task);
    } catch (const std::bad_alloc&) {
        return ERROR_NOT_ENOUGH_MEMORY;
    } catch (...) {
        return ERROR_GEN_FAILURE;
    }
}

extern "C" __declspec(dllexport) DWORD WINAPI WxInspectImagePath(void* parameter) {
    _InterlockedIncrement(&active_callbacks);
    __try { return schedule_image(parameter, true); }
    __finally { _InterlockedDecrement(&active_callbacks); }
}

extern "C" __declspec(dllexport) DWORD WINAPI WxSendImage(void* parameter) {
    _InterlockedIncrement(&active_callbacks);
    __try { return schedule_image(parameter, false); }
    __finally { _InterlockedDecrement(&active_callbacks); }
}
