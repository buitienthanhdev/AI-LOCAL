"""Vẽ nút AI dạng bể nước bằng Pillow (vẽ to x4 rồi thu nhỏ => viền mịn, không răng cưa).

Mực nước = % đã học; màu: đỏ <=30%, vàng 31-70%, xanh lá >70%.
Hiệu ứng: sóng 2 lớp, bọt khí nổi lên khi đang học, quầng sáng "thở" khi đang học,
vòng sáng chạy quanh viền khi đang trả lời, phóng to nhẹ khi rê chuột."""
import math
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

SS = 4                      # hệ số vẽ phóng to để khử răng cưa
KEY_RGB = (1, 2, 3)         # màu nền trong suốt của cửa sổ (trùng KEY ở widget.py)
BASE = (20, 26, 36)         # nền bể
FONT_DIR = Path(r"C:\Windows\Fonts")


def level_color(p: float) -> tuple[int, int, int]:
    return (239, 68, 68) if p <= 30 else (234, 179, 8) if p <= 70 else (34, 197, 94)


def _mix(c1, c2, t):
    return tuple(int(a + (b - a) * t) for a, b in zip(c1, c2))


def _font(size: int, bold: bool = True):
    for name in (("segoeuib.ttf", "arialbd.ttf") if bold else ("segoeui.ttf", "arial.ttf")):
        try:
            return ImageFont.truetype(str(FONT_DIR / name), size)
        except OSError:
            continue
    return ImageFont.load_default()


class BubbleArt:
    def __init__(self, size: int, pad: int = 9):
        self.size, self.pad = size, pad
        self.full = size + 2 * pad
        self.bubbles: list[list[float]] = []
        self._fonts = {}

    def font(self, px, bold=True):
        k = (px, bold)
        if k not in self._fonts:
            self._fonts[k] = _font(px, bold)
        return self._fonts[k]

    def _tick_bubbles(self, learning: bool, level: float, r: float, cx: float, cy: float):
        surface = cy + r - 2 * r * level / 100
        if learning and level > 3 and random.random() < 0.25 and len(self.bubbles) < 9:
            x = cx + random.uniform(-0.6, 0.6) * r
            self.bubbles.append([x, cy + r * 0.9, random.uniform(1.2, 3.0) * SS, random.uniform(0.6, 1.4) * SS])
        for b in self.bubbles:
            b[1] -= b[3]
            b[0] += math.sin(b[1] / (6 * SS)) * 0.4 * SS
        self.bubbles = [b for b in self.bubbles if b[1] > surface + 2 * SS]

    def render(self, level: float, phase: float, t: float, learning: bool, busy: bool, hover: bool,
               dot: tuple[int, int, int], label: str = "AI") -> Image.Image:
        S = self.full * SS
        img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        cx = cy = S / 2
        scale = 1.04 if hover else 1.0
        r = (self.size / 2 - 2) * SS * scale
        lvl = max(0.0, min(100.0, level))
        col = level_color(lvl)

        # Quầng sáng "thở" khi đang học (vòng ngoài mờ dần về nền)
        if learning:
            pulse = (math.sin(t * 3) + 1) / 2
            for i in range(3, 0, -1):
                rr = r + (2 + i * 2 + pulse * 3) * SS
                d.ellipse((cx - rr, cy - rr, cx + rr, cy + rr), fill=_mix(BASE, col, 0.18 * (4 - i) * (0.5 + pulse / 2)))

        # Nền bể
        d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=BASE)

        # Nước: 2 lớp sóng, cắt theo hình tròn bằng mask
        if lvl > 0:
            water = Image.new("RGBA", (S, S), (0, 0, 0, 0))
            wd = ImageDraw.Draw(water)
            surface = cy + r - 2 * r * lvl / 100
            amp = (2.4 if lvl < 100 else 0) * SS * (1.4 if learning else 1)
            for layer, (c, ph, a) in enumerate(((_mix(col, BASE, 0.45), phase * 0.8 + 1.7, amp * 0.8),
                                                (col, phase, amp))):
                pts = [(x, surface + a * math.sin(x / (7 * SS) + ph) + layer * 1.5 * SS)
                       for x in range(int(cx - r - SS), int(cx + r + SS) + 1, SS)]
                pts += [(cx + r + SS, S), (cx - r - SS, S)]
                wd.polygon(pts, fill=c)
            # bọt khí
            self._tick_bubbles(learning, lvl, r, cx, cy)
            for x, y, br, _ in self.bubbles:
                wd.ellipse((x - br, y - br, x + br, y + br), outline=(255, 255, 255, 150), width=max(1, SS // 2))
            mask = Image.new("L", (S, S), 0)
            ImageDraw.Draw(mask).ellipse((cx - r, cy - r, cx + r, cy + r), fill=255)
            img.alpha_composite(Image.composite(water, Image.new("RGBA", (S, S)), mask))

        # Vệt bóng kính (góc trên trái)
        gloss = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(gloss).ellipse((cx - r * 0.72, cy - r * 0.86, cx + r * 0.1, cy - r * 0.32),
                                      fill=(255, 255, 255, 26))
        img.alpha_composite(gloss)

        # Viền
        d = ImageDraw.Draw(img)
        d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=col, width=int(2.6 * SS))
        # Đang trả lời: cung sáng chạy quanh viền
        if busy:
            start = (t * 360) % 360
            d.arc((cx - r, cy - r, cx + r, cy + r), start, start + 80, fill=(255, 255, 255), width=int(3 * SS))

        # Chữ
        f1, f2 = self.font(int(self.size * 0.27 * SS)), self.font(int(self.size * 0.15 * SS))
        for dx, dy, c in ((SS, SS, (0, 0, 0)), (0, 0, (255, 255, 255))):
            d.text((cx + dx, cy - 3 * SS + dy), label, font=f1, fill=c, anchor="mm")
        pct = f"{level:.0f}%" if level >= 1 or level == 0 else f"{level:.1f}%"
        d.text((cx + SS, cy + r * 0.48 + SS), pct, font=f2, fill=(0, 0, 0), anchor="mm")
        d.text((cx, cy + r * 0.48), pct, font=f2, fill=(255, 255, 255), anchor="mm")

        # Chấm trạng thái máy chủ
        dr = 5.5 * SS
        dx, dy = cx + r * 0.72, cy - r * 0.72
        d.ellipse((dx - dr - SS, dy - dr - SS, dx + dr + SS, dy + dr + SS), fill=BASE)
        d.ellipse((dx - dr, dy - dr, dx + dr, dy + dr), fill=dot)

        small = img.resize((self.full, self.full), Image.LANCZOS)
        # Cửa sổ chỉ hỗ trợ trong suốt nhị phân => điểm mờ: nền key; điểm rõ: trộn với màu nền bể
        bg = Image.new("RGBA", small.size, BASE + (255,))
        solid = Image.alpha_composite(bg, small)
        out = Image.new("RGB", small.size, KEY_RGB)
        out.paste(solid.convert("RGB"), (0, 0), small.getchannel("A").point(lambda a: 255 if a >= 110 else 0))
        return out
