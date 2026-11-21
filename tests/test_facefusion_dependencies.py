"""Worker dependency discovery without loading models, a camera or WeChat."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

NATIVE = Path(__file__).resolve().parents[1] / 'native'
if str(NATIVE) not in sys.path:
    sys.path.insert(0, str(NATIVE))
from facefusion_worker import resolve_git_executable


class GitDependencyTests(unittest.TestCase):
    def test_explicit_executable_wins_over_inherited_path(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'git.exe'
            executable.touch()
            with patch.dict(os.environ, {'WXBOT_GIT_EXECUTABLE': str(executable)}), \
                    patch('facefusion_worker.shutil.which') as search:
                self.assertEqual(resolve_git_executable(), str(executable))
                search.assert_not_called()

    def test_invalid_explicit_path_has_a_controlled_error(self):
        with patch.dict(os.environ, {'WXBOT_GIT_EXECUTABLE': 'not-an-installed-git.exe'}):
            with self.assertRaisesRegex(RuntimeError, '^git_executable_unavailable$'):
                resolve_git_executable()

    def test_existing_path_executable_is_preserved(self):
        with patch.dict(os.environ, {'WXBOT_GIT_EXECUTABLE': ''}), \
                patch('facefusion_worker.shutil.which', return_value='synthetic-git.exe'):
            self.assertEqual(resolve_git_executable(), 'synthetic-git.exe')

    def test_missing_git_does_not_launch_a_worker_or_expose_private_paths(self):
        with patch.dict(os.environ, {'WXBOT_GIT_EXECUTABLE': ''}), \
                patch('facefusion_worker.shutil.which', return_value=None), \
                patch('facefusion_worker.Path.is_file', return_value=False):
            with self.assertRaisesRegex(RuntimeError, '^git_executable_unavailable$'):
                resolve_git_executable()
