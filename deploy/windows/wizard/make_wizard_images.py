"""Render the Inno Setup wizard images (BMP) from brand/lucida-icon-256.png.

    python deploy/windows/wizard/make_wizard_images.py

Writes wizard-large-{100,150,200}.bmp (the welcome and finish page panel) and
wizard-small-{100,150,200}.bmp (the icon at the top right of the other pages) next to this file.
Run it again after changing the brand icon; the BMPs are committed so the build needs no Pillow.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ICON = Image.open(HERE.parents[2] / "brand" / "lucida-icon-256.png").convert("RGBA")
INK, MINT = (13, 32, 28), (143, 227, 200)
FONT_CANDIDATES = ["/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf", "DejaVuSerif-Italic.ttf", "georgiai.ttf"]


def font(size):
    for f in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            pass
    return ImageFont.load_default()


def large(scale):
    w, h = round(164 * scale), round(314 * scale)
    img = Image.new("RGB", (w, h), INK)
    d = ImageDraw.Draw(img)
    # A faint diagonal split, echoing the mark: original on one side, result on the other.
    d.polygon([(0, h), (w, round(h * 0.55)), (w, h)], fill=(18, 44, 38))
    s = round(96 * scale)
    img.paste(ICON.resize((s, s), Image.LANCZOS), ((w - s) // 2, round(56 * scale)), ICON.resize((s, s), Image.LANCZOS))
    f = font(round(30 * scale))
    tw = d.textlength("Lucida", font=f)
    d.text(((w - tw) / 2, round(170 * scale)), "Lucida", font=f, fill=MINT)
    f2 = font(round(11 * scale))
    for i, line in enumerate(["Forensic image and", "video workbench"]):
        tw = d.textlength(line, font=f2)
        d.text(((w - tw) / 2, round((214 + 15 * i) * scale)), line, font=f2, fill=(190, 214, 206))
    return img


def small(scale):
    s = round(55 * scale)
    img = Image.new("RGB", (s, s), (255, 255, 255))
    pad = round(3 * scale)
    icon = ICON.resize((s - 2 * pad, s - 2 * pad), Image.LANCZOS)
    img.paste(icon, (pad, pad), icon)
    return img


for pct in (100, 150, 200):
    large(pct / 100).save(HERE / f"wizard-large-{pct}.bmp")
    small(pct / 100).save(HERE / f"wizard-small-{pct}.bmp")
print("wizard images written to", HERE)
