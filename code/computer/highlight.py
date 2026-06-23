"""Desktop screenshot annotation for Layer 3 (Set-of-Marks / Vision).

Draws dashed numbered boxes over AX elements whose bounding rectangles
are known. Mirrors browser/highlight.py; the only difference is the
coordinate source: browser uses Playwright's CSS-pixel rects, desktop uses
cua-driver's screen-pixel rects (no DPR scaling needed — coordinates are
already in physical pixels).

If the cua-driver interface does not return bounding boxes (some platforms
return AX text-only), this module falls back to labelling the whole screen
with a single mark.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO
from typing import Iterable, Optional

from PIL import Image, ImageDraw, ImageFont


@dataclass
class DesktopElement:
    """One annotatable element from the AX tree or screen."""
    id: int
    tag: str
    label: str
    x: float
    y: float
    w: float
    h: float


# Tag → (border, badge_fill) RGB  — same palette as browser/highlight.py
_PALETTE: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "button":   ((39, 174, 96),  (24, 106, 59)),
    "text":     ((46, 134, 193), (33, 97,  140)),
    "input":    ((230, 126, 34), (175, 96,  26)),
    "menu":     ((155, 89, 182), (113, 65,  133)),
    "link":     ((46, 134, 193), (33, 97,  140)),
    "checkbox": ((230, 126, 34), (175, 96,  26)),
}
_DEFAULT = ((192, 57, 43), (146, 43, 33))


def _font(size: int) -> ImageFont.ImageFont:
    for path in (
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    return ImageFont.load_default()


def _draw_dashed_rect(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    color: tuple[int, int, int],
    width: int = 2,
    dash: int = 8,
    gap: int = 5,
) -> None:
    x1, y1, x2, y2 = box

    def seg(p0, p1):
        ax, ay = p0
        bx, by = p1
        dx, dy = bx - ax, by - ay
        length = (dx ** 2 + dy ** 2) ** 0.5
        if length == 0:
            return
        ux, uy = dx / length, dy / length
        cursor = 0.0
        while cursor < length:
            stop = min(cursor + dash, length)
            sx, sy = ax + ux * cursor, ay + uy * cursor
            ex, ey = ax + ux * stop,   ay + uy * stop
            draw.line([(sx, sy), (ex, ey)], fill=color, width=width)
            cursor = stop + gap

    seg((x1, y1), (x2, y1))
    seg((x2, y1), (x2, y2))
    seg((x2, y2), (x1, y2))
    seg((x1, y2), (x1, y1))


def annotate(
    screenshot_png: bytes,
    elements: Iterable[DesktopElement],
) -> bytes:
    """Return new PNG bytes with numbered bounding boxes painted over elements.

    Coordinates in DesktopElement are physical screen pixels — no DPR scaling.
    """
    img = Image.open(BytesIO(screenshot_png)).convert("RGB")
    draw = ImageDraw.Draw(img, "RGBA")
    font = _font(14)
    W, H = img.size

    for el in elements:
        border, badge = _PALETTE.get(el.tag, _DEFAULT)
        x1 = max(0, min(W - 1, int(el.x)))
        y1 = max(0, min(H - 1, int(el.y)))
        x2 = max(0, min(W - 1, int(el.x + el.w)))
        y2 = max(0, min(H - 1, int(el.y + el.h)))
        _draw_dashed_rect(draw, (x1, y1, x2, y2), border, width=2)

        label = str(el.id)
        try:
            tw, th = draw.textbbox((0, 0), label, font=font)[2:]
        except AttributeError:
            tw, th = font.getsize(label)
        pad = 3
        bx1, by1 = x1, max(0, y1 - th - 2 * pad)
        bx2, by2 = bx1 + tw + 2 * pad, by1 + th + 2 * pad
        draw.rectangle((bx1, by1, bx2, by2), fill=badge + (235,))
        draw.rectangle((bx1, by1, bx2, by2), outline=(255, 255, 255, 255), width=1)
        draw.text((bx1 + pad, by1 + pad - 1), label, fill=(255, 255, 255), font=font)

    out = BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


def to_data_url(png_bytes: bytes, max_width: int = 900) -> str:
    """Convert PNG bytes to a data URL, resizing if wider than max_width.

    Full-screen screencapture on a Retina Mac can be 2880×1800 px — that
    encodes to hundreds of KB and many tokens.  Resizing to max_width px
    keeps enough detail for the Vision LLM to read UI text and identify
    elements while cutting token count (and latency) by 4-8×.
    """
    try:
        img = Image.open(BytesIO(png_bytes))
        if img.width > max_width:
            ratio = max_width / img.width
            new_h = int(img.height * ratio)
            img = img.resize((max_width, new_h), Image.LANCZOS)
            buf = BytesIO()
            img.save(buf, format="PNG", optimize=True)
            png_bytes = buf.getvalue()
    except Exception:
        pass  # fall through with original bytes on any PIL error
    return f"data:image/png;base64,{base64.b64encode(png_bytes).decode()}"


def make_legend(elements: list[DesktopElement]) -> str:
    """Build the text legend sent to the LLM alongside the annotated screenshot."""
    lines = []
    for el in elements:
        lines.append(f"[{el.id}] {el.tag}: {el.label}")
    return "\n".join(lines)
