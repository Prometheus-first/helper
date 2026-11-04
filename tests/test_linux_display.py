import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw
from wxbot_framework.linux_display import HeadlessDisplay


class LinuxQuickLoginTests(unittest.TestCase):
    def image(self):
        image = Image.new("RGB", (1024, 768), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((366 + 58, 185 + 289, 366 + 232, 185 + 325), fill=(0, 180, 70))
        draw.rectangle((366 + 10, 185 + 350, 366 + 40, 185 + 360), fill=(30, 50, 120))
        draw.rectangle((366 + 160, 185 + 350, 366 + 190, 185 + 360), fill=(30, 50, 120))
        return image

    def test_only_known_quick_login_window_matches(self):
        image = self.image()
        self.assertEqual(HeadlessDisplay._quick_login_target(image, [(366, 185, 292, 396)]), (426, 545))
        self.assertIsNone(HeadlessDisplay._quick_login_target(image, [(366, 185, 300, 396)]))
        self.assertIsNone(HeadlessDisplay._quick_login_target(Image.new("RGB", (1024, 768), "white"), [(366,185,292,396)]))
        draw=ImageDraw.Draw(image)
        draw.rectangle((366+150,185+345,366+290,185+379), fill="white")
        self.assertIsNone(HeadlessDisplay._quick_login_target(image, [(366,185,292,396)]))

    def test_switch_is_scoped_and_only_once(self):
        display=HeadlessDisplay.__new__(HeadlessDisplay)
        display.lock=threading.RLock()
        display._quick_login_switched=False
        window=SimpleNamespace(returncode=0,stdout='0x200007 "Weixin": ("wechat" "wechat")  292x396+366+185  +366+185')
        click=SimpleNamespace(returncode=0)
        with patch('wxbot_framework.linux_display.shutil.which', return_value='/usr/bin/xwininfo'), \
             patch('wxbot_framework.linux_display.subprocess.run', side_effect=[window,click]) as run:
            self.assertTrue(display._switch_quick_login(self.image(), {}))
            self.assertFalse(display._switch_quick_login(self.image(), {}))
            self.assertEqual(run.call_count, 2)

if __name__ == "__main__":
    unittest.main()
