"""Draw the Game Save Genie mark and emit the icon assets.

Run this after changing the design; the generated files are committed so
neither the build nor a contributor needs Pillow installed:

    python packaging/make_icon.py

Outputs
-------
assets/icon.ico  multi-resolution app icon (exe, installer, shortcut)
assets/icon.png  512px source render, for READMEs and store listings
src/game_save_genie/assets/tray/<state>.png
                 32px tray glyphs, one per health state. These live inside the
                 package, not at the repo root, so a plain `pip install` ships
                 them too — not just the PyInstaller build.

The mark is a magic lamp: an oval body, a tapered spout, a ring handle, and
three sparkles. It is drawn at 8x and downsampled, so the curves stay clean at
16px where every tray icon actually lives.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"
TRAY_ASSETS = ROOT / "src" / "game_save_genie" / "assets" / "tray"

# Design space. Everything below is expressed in a 1000x1000 grid and scaled,
# so the geometry is readable and resolution-independent.
GRID = 1000
SUPERSAMPLE = 8

ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

LAMP = (255, 214, 138)  # warm gold
LAMP_SHADE = (232, 176, 92)  # underside of the body
SPARKLE = (255, 245, 214)

# Tray health states. The badge colour carries the status at 16px, where a
# glyph change would be invisible.
STATES: dict[str, tuple[int, int, int]] = {
    "ok": (74, 128, 240),  # normal: brand blue
    "warn": (224, 158, 48),  # something needs attention
    "error": (214, 74, 74),  # last backup or upload failed
    "paused": (122, 126, 138),  # watching disabled
}

BRAND_TOP = (108, 92, 231)
BRAND_BOTTOM = (74, 128, 240)


def _rounded_mask(size: int, radius_ratio: float = 0.22) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle(
        (0, 0, size - 1, size - 1), radius=int(size * radius_ratio), fill=255
    )
    return mask


def _vertical_gradient(
    size: int, top: tuple[int, int, int], bottom: tuple[int, int, int]
) -> Image.Image:
    grad = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / max(size - 1, 1)
        grad.putpixel(
            (0, y),
            tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),  # type: ignore[arg-type]
        )
    return grad.resize((size, size), Image.Resampling.NEAREST)


def _scale(points: list[tuple[float, float]], size: int) -> list[tuple[float, float]]:
    k = size / GRID
    return [(x * k, y * k) for x, y in points]


def _box(x0: float, y0: float, x1: float, y1: float, size: int) -> tuple[float, ...]:
    k = size / GRID
    return (x0 * k, y0 * k, x1 * k, y1 * k)


def _draw_lamp(draw: ImageDraw.ImageDraw, size: int) -> None:
    """Draw the lamp silhouette onto an already-painted badge."""
    # Spout: a tapered wedge leaving the body to the left and lifting slightly.
    draw.polygon(
        _scale(
            [(340, 545), (120, 452), (98, 500), (300, 620)],
            size,
        ),
        fill=LAMP,
    )

    # Ring handle on the right. An outlined ellipse gives a real hole, so the
    # badge gradient shows through instead of a repainted patch.
    draw.ellipse(
        _box(690, 470, 892, 668, size),
        outline=LAMP,
        width=max(1, round(size * 0.042)),
    )

    # Body: the wide oval that reads as "lamp" even at 16px. Drawn after the
    # handle so the ring tucks behind it.
    draw.ellipse(_box(268, 468, 742, 726, size), fill=LAMP)

    # Lid knob.
    draw.ellipse(_box(468, 396, 556, 484, size), fill=LAMP)

    # Foot, so the lamp sits rather than floats.
    draw.rounded_rectangle(
        _box(378, 690, 662, 762, size), radius=size * 0.035, fill=LAMP
    )

    # Shade under the body for a little depth. Kept subtle: at 16px it merges
    # into the silhouette rather than muddying it.
    draw.chord(_box(268, 468, 742, 726, size), start=20, end=160, fill=LAMP_SHADE)

    # Three sparkles rising from the spout — the "genie" half of the name.
    for cx, cy, r in ((250, 300, 46), (392, 208, 32), (176, 168, 24)):
        draw.polygon(
            _scale(
                [
                    (cx, cy - r),
                    (cx + r * 0.28, cy - r * 0.28),
                    (cx + r, cy),
                    (cx + r * 0.28, cy + r * 0.28),
                    (cx, cy + r),
                    (cx - r * 0.28, cy + r * 0.28),
                    (cx - r, cy),
                    (cx - r * 0.28, cy - r * 0.28),
                ],
                size,
            ),
            fill=SPARKLE,
        )


def render(
    size: int,
    badge_top: tuple[int, int, int],
    badge_bottom: tuple[int, int, int],
) -> Image.Image:
    """Render the mark at ``size`` px with the given badge gradient."""
    work = size * SUPERSAMPLE

    canvas = _vertical_gradient(work, badge_top, badge_bottom).convert("RGBA")
    canvas.putalpha(_rounded_mask(work))
    _draw_lamp(ImageDraw.Draw(canvas), work)

    return canvas.resize((size, size), Image.Resampling.LANCZOS)


def _load_font(size: int, bold: bool = False) -> Any:
    """A real UI font if one is available, else Pillow's bitmap default.

    The card is generated once and committed, so a missing font degrades the
    render rather than breaking anyone's build.
    """
    candidates = (
        ["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"]
        if bold
        else ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"]
    )
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_social_card() -> Image.Image:
    """The 1200x630 image link previews show — Reddit, Discord, Twitter."""
    width, height = 1200, 630
    card = _vertical_gradient(height, (18, 21, 32), (28, 24, 54)).convert("RGBA")
    card = card.resize((width, height), Image.Resampling.NEAREST)

    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((-260, -420, 900, 340), fill=(108, 92, 231, 60))
    card = Image.alpha_composite(card, glow.filter(ImageFilter.GaussianBlur(150)))

    mark = render(200, BRAND_TOP, BRAND_BOTTOM)
    card.paste(mark, (96, 118), mark)

    draw = ImageDraw.Draw(card)
    draw.text((96, 356), "Game Save Genie", font=_load_font(76, bold=True), fill=(240, 242, 248))
    draw.text(
        (96, 452),
        "Steam Cloud for the games that don't have it.",
        font=_load_font(36),
        fill=(178, 186, 204),
    )
    draw.text(
        (96, 516),
        "Automatic, versioned, self-hosted save sync  ·  MIT  ·  no subscription",
        font=_load_font(26),
        fill=(124, 134, 153),
    )
    return card.convert("RGB")


def main() -> int:
    ASSETS.mkdir(parents=True, exist_ok=True)
    TRAY_ASSETS.mkdir(parents=True, exist_ok=True)

    master = render(512, BRAND_TOP, BRAND_BOTTOM)
    master.save(ASSETS / "icon.png")
    # The website is served from docs/, so it cannot reach ../assets. Write the
    # copy here rather than leaving someone to remember it and let them drift.
    site_assets = ROOT / "docs" / "assets"
    if site_assets.is_dir():
        master.save(site_assets / "icon.png")
        render_social_card().save(site_assets / "social-card.png")

    # Render each ICO size independently rather than letting the encoder
    # downscale one bitmap: the 16px entry needs its own LANCZOS pass or the
    # spout disappears.
    master.save(
        ASSETS / "icon.ico",
        format="ICO",
        sizes=[(s, s) for s in ICO_SIZES],
        append_images=[render(s, BRAND_TOP, BRAND_BOTTOM) for s in ICO_SIZES],
    )

    for state, colour in STATES.items():
        top = tuple(min(255, c + 34) for c in colour)
        render(32, top, colour).save(TRAY_ASSETS / f"{state}.png")  # type: ignore[arg-type]

    print(f"Wrote {ASSETS / 'icon.ico'} ({', '.join(str(s) for s in ICO_SIZES)})")
    print(f"Wrote {ASSETS / 'icon.png'} (512)")
    print(f"Wrote {len(STATES)} tray glyphs to {TRAY_ASSETS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
