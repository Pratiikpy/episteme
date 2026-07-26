"""Episteme marketplace avatar — 440x440, square corners, no face.

ἐπιστήμη is Plato's word for knowledge that is *justified*, as against δόξα — opinion that happens to be
right. In the Republic the distinction is a climb: the ascent out of the cave, the divided line from
shadow to Form. Knowledge is not a flash of insight; it is a structure you can stand on and point at,
step by step.

The mark is a PLATONIC SOLID ON AN ASCENDING STEPPED PLINTH, lit from behind. The octahedron is pure
form — six vertices, twelve edges, every one visible and countable. The steps beneath are the
justification: the solid is not floating, it rests on something, and you can see what. Episteme runs 48
deterministic services and every call returns a signed receipt naming each check that ran. The receipt
is the plinth.

Architectural and triangular, against Aletheia's curves and Reach's orthogonal maze, so none of the
three can be mistaken for another at thumbnail size. A cool blue-slate hour rather than dawn or night.

Atmosphere is rendered as LIGHT — gradients, haze, bloom, grain — not as imitation brushwork. An earlier
attempt stamped tens of thousands of impasto strokes and produced convincing texture with an
unconvincing picture: uniform stroke fields read as wood grain or textile.

NO HUMAN FIGURE AND NO FACE anywhere: a human head in profile is a documented instant rejection here.

Spec: exactly 440x440, RGB (never RGBA — alpha is what renders as rounded corners), under 1 MB, no text.

    python scripts/make_avatar_440.py
"""
from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from atmos import (bloom, god_rays, grain, haze_bands, lerp, radial_light,  # noqa: E402
                   screen, stars, vertical_sky)

SIZE = 440
SS = 3
W = SIZE * SS
C = W // 2

NIGHT = (10, 14, 26)
SLATE = (24, 34, 56)
STEEL = (46, 62, 92)
PALE = (128, 156, 190)
GOLD_DIM = (152, 124, 72)
GOLD = (246, 208, 132)
CREAM = (255, 250, 234)
STONE = (40, 48, 68)
STONE_HI = (120, 132, 164)
GREEN = (72, 232, 178)
GREEN_HI = (196, 255, 236)

BASE_Y = C + int(W * 0.300)
STEPS = 4


def _plinth(d: ImageDraw.ImageDraw) -> None:
    """Four receding tiers in one-point perspective — the justification the solid stands on.

    Each tier is a dark RISER plus a lighter TREAD with a bright nosing where they meet. Drawing only
    the treads in one flat tone (the first attempt) read as a single grey ramp; the tiers separate only
    when the riser is clearly darker than the tread above it.
    """
    for k in range(STEPS):
        half_low = W * (0.300 - 0.052 * k)
        half_top = W * (0.300 - 0.052 * (k + 1))
        y_low = BASE_Y - int(W * 0.050 * k)
        y_top = BASE_Y - int(W * 0.050 * (k + 1))
        lum = 0.30 + 0.70 * (k / max(1, STEPS - 1))
        d.polygon([(C - half_low, y_low), (C + half_low, y_low),
                   (C + half_top, y_top), (C - half_top, y_top)],
                  fill=tuple(int(STONE[j] * (0.46 + 0.30 * lum)) for j in range(3)))
        tread_h = int(W * 0.011)
        d.polygon([(C - half_top, y_top), (C + half_top, y_top),
                   (C + half_top, y_top + tread_h), (C - half_top, y_top + tread_h)],
                  fill=lerp(STONE, STONE_HI, 0.30 + 0.50 * lum))
        d.line([C - half_top, y_top, C + half_top, y_top],
               fill=lerp(STONE_HI, PALE, 0.30 + 0.50 * lum), width=int(2.4 * SS))
        for sx in (-1, 1):
            d.line([C + sx * half_low, y_low, C + sx * half_top, y_top],
                   fill=tuple(int(STONE[j] * 0.85) for j in range(3)), width=int(1.8 * SS))


def _octahedron(d: ImageDraw.ImageDraw, mark_only: bool = False) -> int:
    """A regular octahedron in wireframe: six vertices, twelve edges, none hidden.

    An explicit vertex list rather than a projection, so the silhouette stays symmetrical and legible
    when the whole image is 48 pixels wide. Returns the solid's centre y, for the halo behind it."""
    top_y = BASE_Y - int(W * 0.050 * STEPS) + int(W * 0.006)
    h = int(W * 0.212)
    rx, ry = int(W * 0.196), int(W * 0.086)
    cy = top_y - h
    top, bot = (C, cy - h), (C, top_y)
    right, back, left, front = (C + rx, cy), (C, cy - ry), (C - rx, cy), (C, cy + ry)

    if not mark_only:
        d.polygon([top, right, bot, left], fill=(20, 30, 50))
        d.polygon([back, right, front, left], fill=(16, 25, 43))

    back_w, front_w = int(2.2 * SS), int(4.0 * SS)
    for a in (top, bot):
        d.line([*a, *back], fill=GOLD_DIM, width=back_w)
    d.line([*left, *back], fill=GOLD_DIM, width=back_w)
    d.line([*back, *right], fill=GOLD_DIM, width=back_w)
    for a in (right, left, front):
        d.line([*top, *a], fill=GOLD, width=front_w)
        d.line([*bot, *a], fill=GOLD, width=front_w)
    d.line([*left, *front], fill=GOLD, width=front_w)
    d.line([*front, *right], fill=GOLD, width=front_w)

    # Only the silhouette vertices get a node: dotting the interior front and back points put two
    # specks in the middle of the form that read as strays rather than as structure.
    for p in (top, bot, right, left):
        r = int(7.5 * SS)
        d.ellipse([p[0] - r, p[1] - r, p[0] + r, p[1] + r], fill=GREEN)
        r2 = int(3.0 * SS)
        d.ellipse([p[0] - r2, p[1] - r2, p[0] + r2, p[1] + r2], fill=GREEN_HI)

    ro = int(14 * SS)
    d.ellipse([C - ro, cy - ro, C + ro, cy + ro], outline=GREEN, width=int(2.2 * SS))
    ri = int(7 * SS)
    d.ellipse([C - ri, cy - ri, C + ri, cy + ri], fill=GREEN_HI)
    return cy


def build() -> Image.Image:
    # 1. A cool blue-slate hour: light gathering high behind the solid, deep at the base.
    img = vertical_sky(W, [(0.0, NIGHT), (0.26, SLATE), (0.50, STEEL),
                           (0.62, lerp(STEEL, PALE, 0.30)), (0.80, SLATE), (1.0, NIGHT)])
    img = screen(img, haze_bands(W, 31, lerp(STEEL, PALE, 0.40), count=8,
                                 blur=int(13 * SS), strength=0.30))
    stars(ImageDraw.Draw(img), W, 44, 130, (222, 236, 255), y_max=0.42,
          avoid=(C, C - int(W * 0.10), W * 0.26))

    # 2. Light behind the solid — it is being revealed, not spotlit from the front.
    halo_y = C - int(W * 0.075)
    img = screen(img, radial_light(W, C, halo_y, W * 0.36, lerp(PALE, CREAM, 0.30), falloff=2.3))
    img = screen(img, god_rays(W, C, halo_y, lerp(PALE, CREAM, 0.45), seed=19, count=22,
                               blur=int(8 * SS), strength=0.26))

    # 3. Plinth, then the solid over it.
    _plinth(ImageDraw.Draw(img))
    glow = Image.new("RGB", (W, W), (0, 0, 0))
    _octahedron(ImageDraw.Draw(glow), mark_only=True)
    img = screen(img, glow.filter(ImageFilter.GaussianBlur(int(8 * SS))))
    _octahedron(ImageDraw.Draw(img))

    img = bloom(img, radius=int(8 * SS), strength=0.40, threshold=178)
    out = img.resize((SIZE, SIZE), Image.LANCZOS)
    grain(out, seed=9, amount=5)
    return out


def main() -> None:
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "brand")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "episteme_avatar_440.png")
    img = build()
    img.save(out, "PNG", optimize=True)
    px = img.load()
    print(f"wrote {out}")
    print(f"  {img.size[0]}x{img.size[1]} {img.mode}  bytes {os.path.getsize(out):,}")
    print(f"  corners {[px[x, y] for x, y in ((0, 0), (SIZE - 1, 0), (0, SIZE - 1), (SIZE - 1, SIZE - 1))]}")
    for n in (96, 48):
        img.resize((n, n), Image.LANCZOS).resize((n * 4, n * 4), Image.NEAREST).save(
            os.path.join(out_dir, f"_check_{n}.png"))


if __name__ == "__main__":
    main()
