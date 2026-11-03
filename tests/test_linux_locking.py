import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from wxbot_framework.locking import InstanceLock


@unittest.skipUnless(os.name == "posix", "Linux/POSIX lock test")
class LinuxLockTests(unittest.TestCase):
    def test_two_processes_and_abnormal_exit_release_lock(self):
        with tempfile.TemporaryDirectory(prefix="helper-lock-") as directory:
            path = Path(directory) / "bot.lock"
            code = ("import sys; from wxbot_framework.locking import InstanceLock; "
                    "lock=InstanceLock(sys.argv[1]).acquire(); print('ready',flush=True); sys.stdin.read()")
            child = subprocess.Popen([sys.executable, "-B", "-c", code, str(path)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), "ready")
                with self.assertRaises(RuntimeError):
                    InstanceLock(path).acquire()
                child.kill()
                child.communicate(timeout=3)
                with InstanceLock(path):
                    pass
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=3)

    def test_same_instance_cannot_be_acquired_twice(self):
        with tempfile.TemporaryDirectory(prefix="helper-lock-") as directory:
            lock = InstanceLock(Path(directory) / "bot.lock")
            with lock:
                with self.assertRaises(RuntimeError):
                    lock.acquire()
            lock.close()


if __name__ == "__main__":
    unittest.main()
