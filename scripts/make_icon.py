"""Generate the application icon (assets/app.ico) — step 13.

Renders a dark, gaming×hacker themed shield with a neon cyan-teal "S" glyph and
a purple accent ring, then exports a multi-size Windows .ico. Deterministic and
dependency-light (Pillow only). Re-runnable: ``python scripts/make_icon.py``.
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont


BG = (10, 14, 18)          # near-black
ACCENT = (39, 224, 200)    # #27e0c8 neon cyan-teal
ACCENT2 = (155, 123, 255)  # #9b7bff purple


def _shield_points(w, h, pad):
    cx = w / 2
    top = pad
    bottom = h - pad
    left = pad
    right = w - pad
    shoulder = h * 0.40
    return [
        (cx, top),
        (right, top + h * 0.10),
        (right, shoulder),
        (cx, bottom),
        (left, shoulder),
        (left, top + h * 0.10),
    ]


def render(size: int) -> Image.Image:
    # Supersample for crisp anti-aliased edges, then downscale.
    S = max(size * 4, 256)
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    pad = S * 0.10
    pts = _shield_points(S, S, pad)

    # Outer purple ring (the shield border).
    d.polygon(pts, fill=BG + (255,), outline=ACCENT2 + (255,))
    # Re-draw a slightly inset filled shield for the body.
    inset = [(x + (S / 2 - x) * 0.06, y + (S / 2 - y) * 0.06) for (x, y) in pts]
    d.polygon(inset, fill=BG + (255,))

    # Draw thicker border by stroking the polygon edges.
    for width_frac, color in ((0.030, ACCENT2), (0.014, ACCENT)):
        lw = max(1, int(S * width_frac))
        ring = pts + [pts[0]]
        for a, b in zip(ring, ring[1:]):
            d.line([a, b], fill=color + (255,), width=lw)

    # Central glyph: "S" for SNI Spoofer.
    glyph = "S"
    font = None
    for cand in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        if os.path.exists(cand):
            try:
                font = ImageFont.truetype(cand, int(S * 0.46))
                break
            except Exception:
                pass
    if font is None:
        font = ImageFont.load_default()

    bbox = d.textbbox((0, 0), glyph, font=font)
    gw, gh = bbox[2] - bbox[0], bbox[3] - bbox[1]
    gx = (S - gw) / 2 - bbox[0]
    gy = (S - gh) / 2 - bbox[1] - S * 0.02

    # Subtle glow then solid glyph.
    d.text((gx, gy), glyph, font=font, fill=ACCENT2 + (110,))
    d.text((gx, gy), glyph, font=font, fill=ACCENT + (255,))

    return img.resize((size, size), Image.LANCZOS)


def main():
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(here, "assets")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "app.ico")
    sizes = [16, 24, 32, 48, 64, 128, 256]
    base = render(256)
    imgs = [render(s) for s in sizes]
    base.save(out, format="ICO", sizes=[(s, s) for s in sizes],
              append_images=imgs)
    print("wrote", out)


if __name__ == "__main__":
    main()
