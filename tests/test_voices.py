from dataclasses import FrozenInstanceError
from io import BytesIO
import importlib.util
import math
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

from wxbot_framework import voices


def silk(packets=1, *, standard=False):
    header = voices.STANDARD_SILK_HEADER if standard else voices.TENCENT_SILK_HEADER
    return header + (struct.pack("<h", 3) + b"abc") * packets + (b"\xff\xff" if standard else b"")


def wav(samples=320, rate=16000, channels=1, width=2):
    result = BytesIO()
    with wave.open(result, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.setframerate(rate)
        output.writeframes(b"\0" * samples * channels * width)
    return result.getvalue()


class VoiceFileTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def file(self, data, name="input.data"):
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_silk_detected_by_content_and_info_is_frozen(self):
        path = self.file(silk(2), "input.mp3")
        result = voices.inspect_voice_file(path)
        self.assertEqual(result.path, path.resolve())
        self.assertEqual(result.duration_ms, 40)
        with self.assertRaises(FrozenInstanceError):
            result.duration_ms = 20

    def test_silk_accepts_exactly_sixty_seconds(self):
        self.assertEqual(voices.inspect_voice_file(self.file(silk(3000))).duration_ms, 60000)

    def test_silk_rejects_over_sixty_seconds(self):
        with self.assertRaisesRegex(ValueError, "60 秒"):
            voices.inspect_voice_file(self.file(silk(3001)))

    def test_silk_rejects_incomplete_packets_lengths_and_empty_streams(self):
        header = voices.TENCENT_SILK_HEADER
        for body in (b"", b"\x01", b"\x00\x00", b"\xfe\xff", b"\x04\x00abc"):
            with self.subTest(body=body), self.assertRaises(ValueError):
                voices.inspect_voice_file(self.file(header + body))

    def test_standard_tail_is_accepted_only_at_standard_end(self):
        self.assertEqual(voices.inspect_voice_file(self.file(silk(2, standard=True))).duration_ms, 40)
        invalid = (
            silk() + b"\xff\xff",
            silk(standard=True) + b"trailing",
            silk(standard=True)[:-2],
            voices.STANDARD_SILK_HEADER + b"\xff\xff",
        )
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                voices.inspect_voice_file(self.file(data))

    def test_wav_supported_sample_rates_and_rounded_duration(self):
        for rate in (8000, 12000, 16000, 24000):
            with self.subTest(rate=rate):
                self.assertEqual(voices.inspect_voice_file(self.file(wav(rate // 50, rate))).duration_ms, 20)
        self.assertEqual(voices.inspect_voice_file(self.file(wav(1))).duration_ms, 1)

    def test_wav_accepts_sixty_seconds_rejects_one_extra_sample(self):
        self.assertEqual(voices.inspect_voice_file(self.file(wav(8000 * 60, 8000))).duration_ms, 60000)
        with self.assertRaisesRegex(ValueError, "60 秒"):
            voices.inspect_voice_file(self.file(wav(8000 * 60 + 1, 8000)))

    def test_wav_rejects_unsupported_channels_width_and_rate(self):
        for data in (wav(channels=2), wav(width=1), wav(rate=44100)):
            with self.subTest(header=data[:44]), self.assertRaisesRegex(ValueError, "单声道"):
                voices.inspect_voice_file(self.file(data))

    def test_wav_rejects_empty_truncated_or_inconsistent_lengths(self):
        data = wav()
        for invalid in (wav(0), data[:-1], data + b"x", data[:24]):
            with self.subTest(length=len(invalid)), self.assertRaises(ValueError):
                voices.inspect_voice_file(self.file(invalid))

    def test_rejects_mp3_and_invalid_file_content_despite_extension(self):
        with self.assertRaisesRegex(ValueError, "暂不支持 MP3"):
            voices.inspect_voice_file(self.file(b"ID3" + b"\0" * 40, "voice.silk"))

    def test_rejects_empty_missing_directory_nul_and_network_paths(self):
        invalid = (self.file(b""), self.root / "missing", self.root,
                   "a\0b.silk", "", b"input.silk", "https://example.invalid/a.silk",
                   "\\\\server\\share\\a.silk", "//server/share/a.silk", None)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                voices.inspect_voice_file(value)

    def test_rejects_oversized_input_without_reading_it(self):
        path = self.root / "large.silk"
        with path.open("wb") as output:
            output.truncate(voices.MAX_VOICE_BYTES + 1)
        with patch.object(Path, "open", side_effect=AssertionError("不得读取过大文件")):
            with self.assertRaisesRegex(ValueError, "20 MiB"):
                voices.inspect_voice_file(path)

    def test_utf8_path_limit_uses_bytes_not_characters(self):
        voices._check_local_path(Path("a" * 1023))
        with self.assertRaisesRegex(ValueError, "1023"):
            voices._check_local_path(Path("a" * 1024))
        with self.assertRaisesRegex(ValueError, "1023"):
            voices._check_local_path(Path("中" * 342))

    def test_silk_snapshot_is_independent_and_only_own_directory_is_removed(self):
        original = silk(2)
        source = self.file(original)
        target = self.root / "prepared"
        target.mkdir()
        other = target / "keep.txt"
        other.write_text("keep", encoding="utf-8")
        with patch.object(voices, "_load_encoder", side_effect=AssertionError("SILK 不需编码器")):
            with voices.prepare_voice(source, target) as result:
                snapshot = result.path
                self.assertNotEqual(snapshot, source)
                self.assertTrue(snapshot.is_relative_to(target))
                source.write_bytes(b"changed")
                self.assertEqual(snapshot.read_bytes(), original)
                self.assertEqual(result.duration_ms, 40)
            self.assertFalse(snapshot.exists())
        self.assertTrue(source.exists())
        self.assertEqual(other.read_text(encoding="utf-8"), "keep")
        self.assertEqual(list(target.iterdir()), [other])

    def test_standard_silk_snapshot_normalizes_header_and_removes_tail(self):
        source = self.file(silk(standard=True))
        with voices.prepare_voice(source, self.root / "prepared") as result:
            self.assertEqual(result.path.read_bytes(), silk())
        self.assertEqual(source.read_bytes(), silk(standard=True))

    def test_snapshot_is_cleaned_when_caller_raises(self):
        source = self.file(silk())
        target = self.root / "prepared"
        with self.assertRaisesRegex(RuntimeError, "caller failed"):
            with voices.prepare_voice(source, target) as result:
                snapshot = result.path
                raise RuntimeError("caller failed")
        self.assertFalse(snapshot.exists())
        self.assertEqual(list(target.iterdir()), [])

    def test_missing_encoder_has_install_hint_and_creates_no_snapshot(self):
        source = self.file(wav())
        target = self.root / "prepared"
        with patch.object(voices.importlib, "import_module", side_effect=ModuleNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "pip install silk-python"):
                with voices.prepare_voice(source, target):
                    self.fail("没有编码器时不应进入上下文")
        self.assertFalse(target.exists())

    def test_wav_encoder_receives_padded_pcm_and_tencent_flag(self):
        source = self.file(wav(321))
        encoder = Mock()

        def encode(pcm, output, rate, bit_rate, *, tencent):
            self.assertEqual(len(pcm.read()), 640 * 2)
            self.assertEqual((rate, bit_rate, tencent), (16000, 24000, True))
            output.write(silk(2))

        encoder.encode.side_effect = encode
        with patch.object(voices, "_load_encoder", return_value=encoder):
            with voices.prepare_voice(source, self.root / "prepared") as result:
                self.assertEqual(result.duration_ms, 40)
                self.assertEqual(result.path.read_bytes(), silk(2))
        encoder.encode.assert_called_once()

    def test_short_wav_is_padded_to_one_packet_instead_of_lost(self):
        source = self.file(wav(1))
        encoder = Mock()

        def encode(pcm, output, *args, **kwargs):
            self.assertEqual(len(pcm.read()), 320 * 2)
            output.write(silk())

        encoder.encode.side_effect = encode
        with patch.object(voices, "_load_encoder", return_value=encoder):
            with voices.prepare_voice(source, self.root / "prepared") as result:
                self.assertEqual(result.duration_ms, 20)

    def test_failed_or_invalid_encoder_output_is_cleaned(self):
        source = self.file(wav())
        target = self.root / "prepared"
        for output in (b"bad", silk(2), silk(standard=True)):
            encoder = Mock()
            encoder.encode.side_effect = lambda pcm, file, *args, **kwargs: file.write(output)
            with self.subTest(output=output), patch.object(voices, "_load_encoder", return_value=encoder):
                with self.assertRaises(ValueError):
                    with voices.prepare_voice(source, target):
                        self.fail("不能使用无效编码结果")
                self.assertEqual(list(target.iterdir()), [])
        encoder.encode.side_effect = RuntimeError("codec failed")
        with patch.object(voices, "_load_encoder", return_value=encoder):
            with self.assertRaisesRegex(RuntimeError, "转换"):
                with voices.prepare_voice(source, target):
                    self.fail("不能使用失败编码结果")
        self.assertEqual(list(target.iterdir()), [])

    def test_invalid_silk_does_not_create_output_directory(self):
        source = self.file(voices.TENCENT_SILK_HEADER)
        target = self.root / "prepared"
        with self.assertRaises(ValueError):
            with voices.prepare_voice(source, target):
                self.fail("无效语音不可准备")
        self.assertFalse(target.exists())

    @unittest.skipUnless(importlib.util.find_spec("pysilk"), "可选 silk-python 编码依赖未安装")
    def test_real_encoder_roundtrip_keeps_duration_and_audio(self):
        import pysilk
        samples = 16000
        pcm = b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / 16000)))
                       for i in range(samples))
        encoded = BytesIO()
        with wave.open(encoded, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(pcm)
        source = self.file(encoded.getvalue(), "roundtrip.wav")
        with voices.prepare_voice(source, self.root / "prepared") as result:
            decoded = BytesIO()
            pysilk.decode(BytesIO(result.path.read_bytes()), decoded, 16000)
            self.assertEqual(result.duration_ms, 1000)
            self.assertEqual(len(decoded.getvalue()), len(pcm))
            power = sum(value * value for (value,) in struct.iter_unpack("<h", decoded.getvalue()))
            self.assertGreater(power / samples, 10000)


if __name__ == "__main__":
    unittest.main()
