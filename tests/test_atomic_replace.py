"""Short sharing conflicts must not lose config or hide permanent write errors."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from wxbot_framework.config import replace_file
from wxbot_framework.web_panel import atomic_json, read_json


def conflict(code=32):
    error = PermissionError('synthetic Windows sharing conflict')
    error.winerror = code
    return error


class AtomicReplaceTests(unittest.TestCase):
    def test_windows_transient_lock_retries_same_atomic_replacement(self):
        for code in (5, 32, 33):
            with self.subTest(code=code), patch('wxbot_framework.config.os.replace',
                    side_effect=[conflict(code), None]) as replace, patch('wxbot_framework.config.time.sleep') as sleep:
                replace_file('source', 'destination')
                self.assertEqual(replace.call_args_list[0], replace.call_args_list[1])
                self.assertEqual(sleep.call_args.args, (.01,))

    def test_permanent_permission_error_is_bounded_and_preserves_original_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'config.json'
            destination.write_text('{"version":1}', encoding='utf-8')
            with patch('wxbot_framework.config.os.replace', side_effect=conflict()) as replace, \
                    patch('wxbot_framework.config.time.sleep'), self.assertRaises(PermissionError):
                atomic_json(destination, {'version': 2})
            self.assertEqual(replace.call_count, 6)
            self.assertEqual(read_json(destination), {'version': 1})
            self.assertEqual(list(Path(directory).iterdir()), [destination])

    def test_other_errors_are_not_retried(self):
        for error in (PermissionError('permanent'), FileNotFoundError('missing')):
            with patch('wxbot_framework.config.os.replace', side_effect=error) as replace, \
                    patch('wxbot_framework.config.time.sleep') as sleep, self.assertRaises(type(error)):
                replace_file('source', 'destination')
            replace.assert_called_once()
            sleep.assert_not_called()
