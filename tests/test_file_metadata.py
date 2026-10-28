"""文件 XML 解析和账号隔离存储测试；不连接网络或原生服务。"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wxbot_framework.file_metadata import (
    FileMetadata, FileMetadataError, FileStore, MAX_FILE_XML_BYTES, parse_file_metadata,
)
from wxbot_framework.files import MAX_FILE_BYTES

SCRATCH = Path(__file__).resolve().parent.parent / ".runtime/tests"
ACCOUNT = "wxid_file_metadata_robot"
MD5 = "0123456789abcdef0123456789abcdef"


def xml(attach="", title="document.txt", extra="", message_type="6"):
    return (f"<msg><appmsg><title>{title}</title><type>{message_type}</type>"
            f"<appattach>{attach}</appattach>{extra}</appmsg></msg>")


class MetadataTests(unittest.TestCase):
    def test_extracts_fields_without_exposing_private_repr(self):
        result = parse_file_metadata(xml(
            '<totallen>123</totallen><fileext>txt</fileext><cdnattachurl>private-url</cdnattachurl>'
            '<aeskey>private-aes</aeskey><attachid>private-attach</attachid>'
            f'<filekey>private-filekey</filekey><encryver>1</encryver><md5>{MD5.upper()}</md5>',
            title="报告&amp;计划.txt"))
        self.assertEqual((result.title, result.size, result.extension, result.md5, result.encryver),
                         ("报告&计划.txt", 123, "txt", MD5, 1))
        self.assertEqual((result.cdnattachurl, result.aeskey, result.attachid, result.filekey),
                         ("private-url", "private-aes", "private-attach", "private-filekey"))
        for secret in ("private-url", "private-aes", "private-attach", "private-filekey"):
            self.assertNotIn(secret, repr(result))
        with self.assertRaises(FrozenInstanceError):
            result.size = 1
        self.assertFalse(hasattr(result, "__dict__"))

    def test_unknown_fields_are_not_paths_and_missing_integrity_stays_unknown(self):
        result = parse_file_metadata(xml('<path>../../outside</path>', title="../../evil.txt"))
        self.assertEqual(result, FileMetadata(title="../../evil.txt"))
        self.assertFalse(hasattr(result, "path"))
        self.assertIsNone(result.size)
        self.assertEqual(result.md5, "")

    def test_other_valid_appmsg_types_are_ignored(self):
        for value in ("1", "5", "8", "57", "2000"):
            with self.subTest(message_type=value):
                self.assertIsNone(parse_file_metadata(xml(message_type=value)))

    def test_md5_can_be_on_appmsg_or_appattach_but_must_agree(self):
        for body in (xml(extra=f"<md5>{MD5}</md5>"), xml(f"<md5>{MD5}</md5>"),
                     xml(f"<md5>{MD5.upper()}</md5>", extra=f"<md5>{MD5}</md5>")):
            self.assertEqual(parse_file_metadata(body).md5, MD5)
        for body in (xml(extra="<md5>invalid</md5>"), xml("<md5>z</md5>"),
                     xml(f"<md5>{MD5}</md5>", extra="<md5>" + "a" * 32 + "</md5>")):
            with self.subTest(length=len(body)), self.assertRaises(FileMetadataError):
                parse_file_metadata(body)

    def test_size_limits_zero_and_invalid_numbers(self):
        self.assertEqual(parse_file_metadata(xml(f"<totallen>{MAX_FILE_BYTES}</totallen>")).size,
                         MAX_FILE_BYTES)
        for value in ("0", "-1", "+1", "1.5", "1e3", "１２", "", " 1", str(MAX_FILE_BYTES + 1)):
            with self.subTest(value=value), self.assertRaises(FileMetadataError):
                parse_file_metadata(xml(f"<totallen>{value}</totallen>"))
        for value in ("-1", "2147483648", "1.0"):
            with self.subTest(value=value), self.assertRaises(FileMetadataError):
                parse_file_metadata(xml(f"<encryver>{value}</encryver>"))

    def test_rejects_dtd_entities_bad_unicode_and_byte_limit(self):
        invalid = (None, b"<msg />", "", " ", xml() + "\0", xml() + "\ud800",
                   '<!DOCTYPE msg [<!ENTITY secret "hidden">]>' + xml(title="&secret;"),
                   '<!DOCTYPE msg SYSTEM "file:///C:/secret.txt">' + xml(),
                   xml(title="&unknown;"), xml() + " " * MAX_FILE_XML_BYTES,
                   xml(title="文" * (MAX_FILE_XML_BYTES // 2)))
        for value in invalid:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(FileMetadataError):
                parse_file_metadata(value)
        body = xml()
        self.assertIsNotNone(parse_file_metadata(body + " " * (MAX_FILE_XML_BYTES - len(body))))

    def test_wrong_nested_duplicate_or_missing_structure_is_rejected(self):
        invalid = ("<appmsg><type>6</type><appattach /></appmsg>", "<msg />", "<msg><appmsg /></msg>",
                   "<msg><wrapper><appmsg><type>6</type><appattach /></appmsg></wrapper></msg>",
                   "<msg><appmsg><type>6</type></appmsg></msg>",
                   "<msg><appmsg><type>6</type><wrapper><appattach /></wrapper></appmsg></msg>",
                   xml().replace("</appmsg>", "<appattach /></appmsg>"),
                   xml().replace("</appmsg>", "<type>6</type></appmsg>"),
                   xml("<totallen>1</totallen><totallen>1</totallen>"),
                   xml("<aeskey><x /></aeskey>"), xml(title="<x />"),
                   xml().replace("</msg>", "<appmsg><type>6</type><appattach /></appmsg></msg>"),
                   xml().replace("<type>6</type>", "<type>bad</type>"), "<msg><appmsg></msg>")
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(FileMetadataError):
                parse_file_metadata(value)

    def test_excessive_structure_and_private_fields_rejected_without_echo(self):
        invalid = (xml("<aeskey>" + "s" * 1025 + "</aeskey>"),
                   xml("<cdnattachurl>" + "s" * 8193 + "</cdnattachurl>"),
                   xml("<x>" * 17 + "</x>" * 17), xml("<x />" * 257),
                   xml('<x ' + " ".join(f'a{i}="x"' for i in range(65)) + ' />'))
        for value in invalid:
            with self.subTest(length=len(value)), self.assertRaises(FileMetadataError) as raised:
                parse_file_metadata(value)
            self.assertNotIn("<msg", str(raised.exception))
            self.assertNotIn("ssss", str(raised.exception))


class StoreTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="file-metadata-", dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self._cleanup)
        self.store = FileStore(self.root / "files", ACCOUNT)

    def _cleanup(self):
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        self.temporary.cleanup()

    def test_save_any_content_with_safe_name_and_no_overwrite(self):
        first = self.store.save(b"binary\0\xff", name="../../hello.txt", sequence=4)
        second = self.store.save(b"second", name="hello.txt", sequence=4)
        self.assertNotEqual(first, second)
        self.assertEqual(first.parent, self.store.directory)
        self.assertTrue(first.name.startswith("file-4-"))
        self.assertTrue(first.name.endswith("-hello.txt"))
        self.assertEqual(first.read_bytes(), b"binary\0\xff")
        self.assertEqual(second.read_bytes(), b"second")
        self.assertEqual(set(self.store.directory.iterdir()), {first, second})

    def test_size_limit_and_empty_invalid_payloads(self):
        for data in (None, b"", "a", bytearray(b"x")):
            with self.subTest(data_type=type(data).__name__), self.assertRaises(ValueError):
                self.store.save(data)
        self.assertFalse(self.store.root.exists())
        result = self.store.save(b"x" * (21 * 1024 * 1024))
        self.assertEqual(result.stat().st_size, 21 * 1024 * 1024)

    def test_account_isolation_is_case_sensitive_and_never_uses_id_as_path(self):
        stores = [self.store] + [FileStore(self.store.root, account)
                                for account in (ACCOUNT.upper(), "..", "CON", "wxid_other")]
        paths = [store.save(b"one", name="NUL.txt") for store in stores]
        self.assertEqual(len({path.parent for path in paths}), len(paths))
        for store, path in zip(stores, paths):
            self.assertEqual(path.parent.parent, self.store.root)
            self.assertTrue(path.name.endswith("-_NUL.txt"))
            self.assertNotEqual(path.parent.name, store.account)

    def test_invalid_account_root_sequence_and_name_do_not_create_files(self):
        for account in (None, "", "../outside", r"C:\outside", "filehelper", "123@chatroom", "gh_public"):
            with self.subTest(account=account), self.assertRaises(ValueError):
                FileStore(self.root, account)
        for root in (None, b"path", "", " ", "bad\0path", "https://example.test/files"):
            with self.subTest(root=root), self.assertRaises(ValueError):
                FileStore(root, ACCOUNT)
        for sequence in ("../outside", "7", True, -1, 1.5, 1 << 64):
            with self.subTest(sequence=sequence), self.assertRaises(ValueError):
                self.store.save(b"x", sequence=sequence)
        with self.assertRaises(ValueError):
            self.store.save(b"x", name=None)
        self.assertFalse(self.store.root.exists())

    def test_parallel_saves_same_name_preserve_all_contents(self):
        payloads = [index.to_bytes(4, "little") for index in range(24)]
        def save(index):
            return self.store.save(payloads[index], name="same.txt", sequence=1)
        with ThreadPoolExecutor(max_workers=6) as pool:
            paths = list(pool.map(save, range(len(payloads))))
        self.assertEqual(len(set(paths)), len(paths))
        self.assertEqual([path.read_bytes() for path in paths], payloads)
        self.assertEqual(set(self.store.directory.iterdir()), set(paths))

    def test_atomic_save_failures_clean_temporary_and_reservation(self):
        previous = self.store.save(b"previous")
        for function in ("os.replace", "os.fsync", "os.fdopen"):
            with self.subTest(function=function), patch("wxbot_framework.file_metadata." + function,
                    side_effect=OSError("simulated failure")), self.assertRaises(OSError):
                self.store.save(b"another")
            self.assertEqual(list(self.store.directory.iterdir()), [previous])
            self.assertEqual(previous.read_bytes(), b"previous")

    def test_filename_collision_retries_without_replacing_existing(self):
        with patch("wxbot_framework.file_metadata.secrets.token_hex", return_value="a" * 32):
            first = self.store.save(b"first", name="same.txt")
            with self.assertRaises(FileExistsError):
                self.store.save(b"second", name="same.txt")
        self.assertEqual(list(self.store.directory.iterdir()), [first])
        self.assertEqual(first.read_bytes(), b"first")
        with patch("wxbot_framework.file_metadata.secrets.token_hex", side_effect=["a" * 32, "b" * 32]):
            second = self.store.save(b"second", name="same.txt")
        self.assertNotEqual(first, second)

    def test_directory_redirection_rejected_before_writes(self):
        original = Path.resolve
        outside = self.root / "outside"
        def redirected(path, *args, **kwargs):
            return outside if path == self.store.directory else original(path, *args, **kwargs)
        with patch.object(Path, "resolve", redirected), self.assertRaises(ValueError):
            self.store.save(b"private")
        self.assertFalse(outside.exists())
        self.assertEqual(list(self.store.directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
