import os
from pathlib import Path
import tempfile
import unittest

from wxbot_framework.linux_profiles import profile_candidates


class LinuxProfileCandidateTests(unittest.TestCase):
    def test_only_open_database_under_matching_profile_is_a_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home, proc = base / "home", base / "proc"
            account = "wxid_fixture123"
            directory = home / "xwechat_files" / (account + "_abcd") / "db_storage"
            directory.mkdir(parents=True)
            (home / ".xwechat" / account).mkdir(parents=True)
            db = directory / "message.db"
            db.write_bytes(b"not read")
            handles = proc / "11" / "fd"
            handles.mkdir(parents=True)
            (handles / "3").symlink_to(db)
            self.assertEqual(profile_candidates(11, home, proc_root=proc), (account,))
            (handles / "3").unlink()
            self.assertEqual(profile_candidates(11, home, proc_root=proc), ())

    def test_unmatched_directories_and_non_database_handles_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home, proc = base / "home", base / "proc"
            directory = home / "xwechat_files" / "wxid_fixture123_abcd"
            directory.mkdir(parents=True)
            (home / ".xwechat").mkdir()
            text = directory / "unrelated.txt"
            text.write_text("x")
            handles = proc / "12" / "fd"
            handles.mkdir(parents=True)
            (handles / "3").symlink_to(text)
            self.assertEqual(profile_candidates(12, home, proc_root=proc), ())
            (home / ".xwechat" / "wxid_fixture123").mkdir()
            self.assertEqual(profile_candidates(12, home, proc_root=proc), ())

    def test_multiple_open_profiles_remain_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home, proc = base / "home", base / "proc"
            handles = proc / "13" / "fd"
            handles.mkdir(parents=True)
            for i, account in enumerate(("wxid_fixtureone", "wxid_fixturetwo")):
                directory = home / "xwechat_files" / (account + "_abcd")
                directory.mkdir(parents=True)
                (home / ".xwechat" / account).mkdir(parents=True)
                db = directory / "contact.db"
                db.write_bytes(b"")
                (handles / str(i + 3)).symlink_to(db)
            self.assertEqual(profile_candidates(13, home, proc_root=proc),
                             ("wxid_fixtureone", "wxid_fixturetwo"))

    def test_symlinked_profile_directory_is_not_a_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            home, proc = base / "home", base / "proc"
            data = home / "xwechat_files"
            data.mkdir(parents=True)
            account = "wxid_fixture123"
            (home / ".xwechat" / account).mkdir(parents=True)
            outside = base / "outside"
            outside.mkdir()
            db = outside / "message.db"
            db.write_bytes(b"")
            (data / (account + "_abcd")).symlink_to(outside, target_is_directory=True)
            handles = proc / "14" / "fd"
            handles.mkdir(parents=True)
            (handles / "3").symlink_to(db)
            self.assertEqual(profile_candidates(14, home, proc_root=proc), ())
    def test_invalid_pid_and_missing_process(self):
        with self.assertRaises(ValueError):
            profile_candidates(True, Path("/tmp"))
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(profile_candidates(99, Path(tmp), proc_root=Path(tmp)), ())


if __name__ == "__main__":
    unittest.main()
