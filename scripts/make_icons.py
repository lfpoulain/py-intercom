"""Builds the role icons from logo.png: the logo plus a corner badge.

    Régie  -> teal badge with a rack of three units
    Poste  -> amber badge with a headset

Writes src/py_intercom/img/logo-server.ico and logo-client.ico (and a PNG of
each, used by the README and as a source for the .ico). Needs Pillow.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

IMG = Path(__file__).resolve().parents[1] / "src" / "py_intercom" / "img"
SIZE = 1024
ICO_SIZES = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]
WHITE = (255, 255, 255, 255)
RING = (11, 44, 69, 255)  # the logo's navy, so the badge detaches from any background
CENTER = (340, 340)  # glyphs are drawn on their own 680 px layer, then scaled down
RADIUS = 300
BADGE_SCALE = 0.7
BADGE_AT = (735, 735)  # where the badge's centre lands on the logo


def _server_glyph(d: ImageDraw.ImageDraw) -> None:
    cx, cy = CENTER
    for dy in (-135, 0, 135):
        d.rounded_rectangle((cx - 175, cy + dy - 50, cx + 175, cy + dy + 50), radius=26, fill=WHITE)
        d.ellipse((cx + 105, cy + dy - 16, cx + 137, cy + dy + 16), fill=(43, 181, 160, 255))
        d.rounded_rectangle((cx - 140, cy + dy - 12, cx - 40, cy + dy + 12), radius=12, fill=(43, 181, 160, 255))


def _client_glyph(d: ImageDraw.ImageDraw) -> None:
    cx, cy = CENTER
    w = 56
    d.arc((cx - 165, cy - 190, cx + 165, cy + 140), start=180, end=360, fill=WHITE, width=w)
    for sx in (-1, 1):
        x = cx + sx * 165
        d.rounded_rectangle((x - 50, cy - 40, x + 50, cy + 120), radius=34, fill=WHITE)
    d.arc((cx - 40, cy + 20, cx + 190, cy + 190), start=20, end=110, fill=WHITE, width=34)  # mic boom
    d.ellipse((cx + 8, cy + 168, cx + 62, cy + 222), fill=WHITE)


def build(name: str, color: tuple[int, int, int], glyph) -> None:
    base = Image.open(IMG / "logo.png").convert("RGBA").resize((SIZE, SIZE), Image.LANCZOS)
    layer = Image.new("RGBA", (680, 680), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    cx, cy = CENTER
    d.ellipse((cx - RADIUS - 34, cy - RADIUS - 34, cx + RADIUS + 34, cy + RADIUS + 34), fill=RING)
    d.ellipse((cx - RADIUS, cy - RADIUS, cx + RADIUS, cy + RADIUS), fill=(*color, 255))
    glyph(d)
    side = round(680 * BADGE_SCALE)
    layer = layer.resize((side, side), Image.LANCZOS)
    icon = base.copy()
    icon.alpha_composite(layer, (BADGE_AT[0] - side // 2, BADGE_AT[1] - side // 2))
    icon.save(IMG / f"logo-{name}.png")
    icon.save(IMG / f"logo-{name}.ico", sizes=ICO_SIZES)


if __name__ == "__main__":
    build("server", (43, 181, 160), _server_glyph)
    build("client", (245, 166, 35), _client_glyph)
