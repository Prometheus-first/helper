"""可重复生成的题卡，不下载图片、不携带答案文件名或图片文本元数据。"""
from __future__ import annotations

import os
from pathlib import Path
import math
import uuid

from PIL import Image, ImageDraw, ImageFont

VERSION = 'v1'
WIDTH, HEIGHT = 720, 620
INK = '#15191f'
BLUE = '#3387bb'
ORANGE = '#e89329'


def find_font(value=None):
    if value is not None:
        if not isinstance(value, str) or not value.strip() or '\0' in value:
            raise ValueError('picture_idiom.font_file 应为本地中文字体路径')
        candidates = [Path(value)]
    else:
        directory = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
        candidates = [directory / name for name in ('simkai.ttf', 'msyh.ttc', 'simhei.ttf')]
    for path in candidates:
        if path.is_file():
            ImageFont.truetype(str(path), 24)
            return path.resolve()
    raise ValueError('看图猜成语需要中文字体，请配置 picture_idiom.font_file（如楷体或微软雅黑）')


class CardRenderer:
    def __init__(self, directory, font_file=None):
        self.directory = Path(directory).resolve()
        self.font_file = find_font(font_file)
        self.fonts = {}

    def font(self, size):
        if size not in self.fonts:
            self.fonts[size] = ImageFont.truetype(str(self.font_file), size)
        return self.fonts[size]

    def text(self, image, value, x, y, size=100, color=INK, rotation=0):
        if rotation:
            layer = Image.new('RGBA', (size * 3, size * 3))
            self.text(layer, value, size * 1.5, size * 1.5, size, color)
            layer = layer.rotate(rotation, resample=Image.Resampling.BICUBIC, expand=True)
            image.paste(layer, (int(x - layer.width / 2), int(y - layer.height / 2)), layer)
            return
        draw = ImageDraw.Draw(image)
        font = self.font(size)
        left, top, right, bottom = draw.textbbox((0, 0), value, font=font)
        draw.text((x - (left + right) / 2, y - (top + bottom) / 2), value, font=font, fill=color)

    @staticmethod
    def arrow(draw, start, end, color=INK, width=5):
        draw.line((start, end), fill=color, width=width)
        angle = math.atan2(end[1] - start[1], end[0] - start[0])
        wings = [(end[0] - 18 * math.cos(angle + offset), end[1] - 18 * math.sin(angle + offset))
                 for offset in (-0.45, 0.45)]
        draw.polygon((end, *wings), fill=color)

    def render(self, puzzle):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f'card-{puzzle.code}-{VERSION}.png'
        if path.is_file():
            with Image.open(path) as cached:
                cached.verify()
            return path
        image = Image.new('RGB', (WIDTH, HEIGHT), 'white')
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((2, 2, WIDTH - 3, HEIGHT - 3), radius=24, outline='#e4e6e9', width=3)
        self.text(image, '看图猜成语', WIDTH / 2, 56, 38, ORANGE)
        draw.line((55, 108, 665, 108), fill='#eeeeee', width=2)
        self._body(image, puzzle)
        self.text(image, '猜一个四字成语', WIDTH / 2, 566, 25, '#90969e')
        temporary = self.directory / f'.card-{uuid.uuid4().hex}.tmp'
        try:
            image.save(temporary, format='PNG')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def _body(self, image, puzzle):
        draw = ImageDraw.Draw(image)
        word = lambda text, x, y, size=100, color=INK, rotation=0: self.text(image, text, x, y, size, color, rotation)
        kind, values = puzzle.kind, puzzle.values
        if kind == 'surround':
            word('梁', 360, 360, 230)
            for x, y in ((360, 172), (155, 350), (565, 350)):
                word('日', x, y, 84)
        elif kind == 'above':
            word(values[0], 360, 220, 130, BLUE)
            word(values[1], 360, 415, 150)
        elif kind == 'lengths':
            for index, length in enumerate((440, 440, 440, 180, 180)):
                draw.line((140, 190 + index * 65, 140 + length, 190 + index * 65), fill=INK, width=12)
        elif kind == 'counts':
            colors = (INK, BLUE, ORANGE, '#b94e7a', '#6d6ab1', '#377b63')
            for row, (value, count) in enumerate(((values[0], values[1]), (values[2], values[3]))):
                step = min(150, 570 / count)
                for index in range(count):
                    word(value, 360 + (index - (count - 1) / 2) * step, 235 + row * 190,
                         min(125, int(step * 0.86)), colors[index % len(colors)])
        elif kind == 'cows':
            for index in range(9):
                word('牛', 240 + index % 3 * 120, 190 + index // 3 * 90, 74)
            word('毛', 360, 470, 78, BLUE)
        elif kind == 'eye_rows':
            word('目', 210, 330, 170)
            for index in range(10):
                word('行', 470, 168 + index * 34, 30)
        elif kind == 'sizes':
            word(values[0], 230, 330, values[1])
            word(values[2], 520, 330, values[3])
        elif kind == 'strong':
            draw.rectangle((165, 190, 555, 470), outline='#c8ccd0', width=4)
            for x, y in ((165, 190), (555, 190), (165, 470), (555, 470)):
                draw.rectangle((x - 48, y - 48, x + 48, y + 48), fill='white')
                word('强', x, y, 82)
            word('干', 360, 330, 130, BLUE)
        elif kind == 'mouth_heart':
            draw.rectangle((110, 245, 300, 435), outline=INK, width=11)
            word('是', 205, 340, 110)
            heart = [(500, 275), (465, 232), (423, 246), (400, 290), (415, 344), (500, 435),
                     (585, 344), (600, 290), (577, 246), (535, 232), (500, 275)]
            draw.polygon(heart, fill='#fff3f5')
            draw.line(heart, fill='#c85c77', width=6)
            word('非', 500, 330, 108)
        elif kind == 'outside':
            draw.rectangle((220, 285, 500, 485), outline='#adb5bf', width=5)
            word(values[0], 360, 385, 125)
            word(values[0], 360, 190, 105, BLUE)
        elif kind == 'oil':
            word('油', 360, 185, 95, BLUE)
            self.arrow(draw, (360, 250), (360, 320), BLUE)
            word('火', 360, 430, 170, '#d36e32')
        elif kind == 'rotated':
            word(values[0], 225, 330, 165, rotation=values[1])
            word(values[2], 505, 330, 165, rotation=values[3])
        elif kind == 'fall':
            word('一', 360, 175, 110)
            self.arrow(draw, (360, 240), (360, 405), BLUE, 7)
            word('千丈', 360, 475, 90)
        elif kind == 'mirror':
            draw.pieslice((95, 240, 265, 410), 90, 270, fill='#edf3f7', outline=INK, width=5)
            draw.pieslice((125, 240, 295, 410), 270, 450, fill='#edf3f7', outline=INK, width=5)
            self.arrow(draw, (325, 325), (405, 325), BLUE)
            draw.ellipse((450, 235, 630, 415), fill='#edf3f7', outline=INK, width=5)
            draw.line((485, 355, 560, 275), fill='white', width=9)
        elif kind == 'water_stone':
            draw.polygon(((245, 445), (265, 320), (355, 230), (445, 310), (465, 445)), fill='#dde0e2', outline=INK, width=4)
            word('石', 355, 355, 95)
            for x in range(100, 640, 60):
                draw.arc((x, 425, x + 60, 455), 0, 180, fill=BLUE, width=5)
            draw.line((100, 260, 215, 260), fill='#b7d9ee', width=3)
            self.arrow(draw, (140, 290), (140, 400), BLUE)
        elif kind == 'drop_stone':
            draw.rounded_rectangle((200, 355, 520, 475), radius=20, fill='#dfe2e4', outline=INK, width=4)
            word('石', 265, 415, 65)
            draw.ellipse((337, 390, 383, 450), fill='white', outline=BLUE, width=3)
            for y in (170, 235, 300):
                draw.polygon(((360, y - 20), (345, y + 5), (375, y + 5)), fill=BLUE)
                draw.ellipse((345, y - 5, 375, y + 22), fill=BLUE)
            draw.ellipse((350, 480, 370, 505), fill=BLUE)
        elif kind == 'cut':
            draw.line((110, 380, 320, 380), fill='#b28b53', width=16)
            draw.line((405, 400, 615, 400), fill='#b28b53', width=16)
            draw.polygon(((360, 265), (405, 280), (342, 425), (315, 390)), fill='#e7edf0', outline=INK, width=4)
            draw.polygon(((360, 265), (405, 280), (435, 215), (390, 200)), fill='#b3814d', outline=INK, width=4)
        elif kind == 'door':
            draw.rectangle((145, 165, 575, 490), outline='#9f805b', width=8)
            draw.polygon(((145, 165), (225, 220), (225, 460), (145, 490)), fill='#eadcc9', outline='#9f805b', width=5)
            draw.polygon(((575, 165), (495, 220), (495, 460), (575, 490)), fill='#eadcc9', outline='#9f805b', width=5)
            draw.polygon(((265, 430), (320, 305), (355, 355), (400, 260), (460, 430)), fill='#99b7aa', outline='#3b7663', width=4)
        elif kind == 'stone_birds':
            word('鸟', 210, 220, 100)
            word('鸟', 515, 220, 100)
            self.arrow(draw, (340, 355), (235, 285), BLUE)
            self.arrow(draw, (380, 355), (490, 285), BLUE)
            draw.polygon(((295, 470), (315, 380), (395, 370), (435, 470)), fill='#e0e3e5', outline=INK, width=4)
            word('石', 365, 425, 60)
        else:
            raise ValueError('题库包含未知绘图类型')
