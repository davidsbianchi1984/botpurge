"""Draw the app icons (shield with a check, violet on night blue). Run: python packaging/make_icons.py"""
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "botpurge" / "web"


def icon(size: int, maskable: bool = False) -> Image.Image:
    s = 4 * size                                    # draw large, shrink for smooth edges
    im = Image.new("RGB", (s, s), "#0d0a20")
    d = ImageDraw.Draw(im)
    for y in range(s):                              # soft violet glow from the top
        t = 1 - y / s
        d.line([(0, y), (s, y)], fill=(int(13 + 30 * t), int(10 + 19 * t), int(32 + 78 * t)))
    pad = s * (0.22 if maskable else 0.14)          # maskable icons need a safe zone
    w, top, bot = s - 2 * pad, pad, s - pad
    cx = s / 2
    shield = [(pad, top + w * 0.12), (cx, top), (s - pad, top + w * 0.12), (s - pad, top + w * 0.5),
              (cx, bot), (pad, top + w * 0.5)]
    d.polygon(shield, fill="#7b5cff")
    inset = w * 0.07
    inner = [(pad + inset, top + w * 0.16), (cx, top + inset), (s - pad - inset, top + w * 0.16), (s - pad - inset, top + w * 0.49),
             (cx, bot - inset * 1.4), (pad + inset, top + w * 0.49)]
    d.polygon(inner, fill="#9d7bff")
    lw = int(w * 0.09)
    d.line([(cx - w * 0.2, top + w * 0.47), (cx - w * 0.05, top + w * 0.62), (cx + w * 0.23, top + w * 0.3)], fill="#ffffff", width=lw, joint="curve")
    return im.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    for n in (192, 512):
        icon(n).save(OUT / f"icon-{n}.png", optimize=True)
    icon(512, maskable=True).save(OUT / "icon-maskable.png", optimize=True)
    icon(180).save(OUT / "apple-touch-icon.png", optimize=True)
    print("icons written to", OUT)
