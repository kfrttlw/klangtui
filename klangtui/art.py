"""Cover art in glyphs: each cell carries 2×2 sub-pixels as a quadrant
character (▘▝▖▗▚▞…) in two colours — twice the detail of plain half-blocks.

Decoding needs Pillow; without it klangtui simply shows no cover.
"""

from __future__ import annotations

import io
import re

from rich.text import Text

try:
    from PIL import Image
except ImportError:              # optional: no Pillow → no cover
    Image = None

# keyed by the (TL, TR, BL, BR) mask of the sub-pixels drawn in the fg colour
_QUAD = {
    (1, 0, 0, 0): "▘", (0, 1, 0, 0): "▝", (0, 0, 1, 0): "▖", (0, 0, 0, 1): "▗",
    (1, 1, 0, 0): "▀", (0, 0, 1, 1): "▄", (1, 0, 1, 0): "▌", (0, 1, 0, 1): "▐",
    (1, 0, 0, 1): "▚", (0, 1, 1, 0): "▞", (1, 1, 1, 0): "▛", (1, 1, 0, 1): "▜",
    (1, 0, 1, 1): "▙", (0, 1, 1, 1): "▟",
}


def available() -> bool:
    return Image is not None


def small_url(url: str) -> str:
    """SoundCloud's -large is 100 px; a cover a few cells wide needs no more
    than the 67 px variant (~2.5 KB)."""
    return re.sub(r"-large(\.[a-zA-Z]+)$", r"-t67x67\1", url)


def _hex(c) -> str:
    return f"#{c[0]:02x}{c[1]:02x}{c[2]:02x}"


def _avg(cs) -> tuple[int, int, int]:
    n = len(cs)
    return (sum(c[0] for c in cs) // n, sum(c[1] for c in cs) // n,
            sum(c[2] for c in cs) // n)


def render(data: bytes, cols: int, rows: int) -> Text | None:
    """Image bytes → a cols×rows block of quadrant glyphs (None if undecodable)."""
    if Image is None:
        return None
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return None
    # centre-crop to what the block looks like on screen: a cell is about
    # twice as tall as it is wide, so cols×rows cells ≈ cols : 2·rows
    w, h = im.size
    want = cols / (2 * rows)
    if w / h > want:
        nw = int(h * want)
        im = im.crop(((w - nw) // 2, 0, (w - nw) // 2 + nw, h))
    else:
        nh = int(w / want)
        im = im.crop((0, (h - nh) // 2, w, (h - nh) // 2 + nh))
    lanczos = getattr(Image, "Resampling", Image).LANCZOS
    im = im.resize((cols * 2, rows * 2), lanczos)
    px = im.load()
    out = Text(no_wrap=True)
    for cy in range(rows):
        if cy:
            out.append("\n")
        for cx in range(cols):
            x, y = cx * 2, cy * 2
            quad = [px[x, y], px[x + 1, y], px[x, y + 1], px[x + 1, y + 1]]
            # split the 4 sub-pixels along the channel with the widest spread —
            # keeps real colour contrast, not just brightness
            ch = max(range(3), key=lambda k: max(c[k] for c in quad) - min(c[k] for c in quad))
            vals = [c[ch] for c in quad]
            mid = (max(vals) + min(vals)) / 2
            mask = tuple(1 if v >= mid else 0 for v in vals)
            if mask in ((1, 1, 1, 1), (0, 0, 0, 0)):
                out.append("█", style=_hex(_avg(quad)))
                continue
            fg = _avg([c for c, m in zip(quad, mask, strict=True) if m])
            bg = _avg([c for c, m in zip(quad, mask, strict=True) if not m])
            out.append(_QUAD[mask], style=f"{_hex(fg)} on {_hex(bg)}")
    return out
