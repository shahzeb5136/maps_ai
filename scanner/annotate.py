"""
Pictures drawn from the findings, for the PDF.

The web report draws its boxes live over the original photographs; paper
cannot, so the PDF gets pre-drawn versions: each photo with its issues boxed
and tagged, a close crop of the evidence for every issue in the register, the
satellite plate with the measured roof planes and a panel layout, and small
thumbnails for the photo log (which the web grids use too, rather than
pulling fifty full-size photographs to draw fifty small squares).
"""

import logging
from typing import Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFont

from . import solar as solar_mod

log = logging.getLogger(__name__)

SEVERITY_RGB = {
    "critical": (220, 38, 38),
    "major": (234, 88, 12),
    "minor": (202, 138, 4),
    "cosmetic": (100, 116, 139),
}


def _font(size: int):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow without FreeType sizing
        return ImageFont.load_default()


def _px(box: Sequence[int], w: int, h: int) -> Tuple[int, int, int, int]:
    y0, x0, y1, x1 = box
    return int(x0 / 1000 * w), int(y0 / 1000 * h), int(x1 / 1000 * w), int(y1 / 1000 * h)


def thumbnail(img: Image.Image, edge: int = 480) -> Image.Image:
    copy = img.copy()
    copy.thumbnail((edge, edge), Image.LANCZOS)
    return copy


def annotated(img: Image.Image, marks: List[Tuple[str, str, Sequence[int]]],
              edge: int = 1200) -> Image.Image:
    """marks: [(tag, severity, box)]. Boxes drawn in severity colour, tagged."""
    out = thumbnail(img, edge).convert("RGB")
    w, h = out.size
    draw = ImageDraw.Draw(out)
    stroke = max(3, round(min(w, h) / 220))
    font = _font(max(14, round(min(w, h) / 38)))
    for tag, severity, box in marks:
        colour = SEVERITY_RGB.get(severity, SEVERITY_RGB["minor"])
        x0, y0, x1, y1 = _px(box, w, h)
        draw.rectangle([x0, y0, x1, y1], outline=colour, width=stroke)
        tw = draw.textlength(tag, font=font)
        th = font.size if hasattr(font, "size") else 12
        ty = y0 - th - 8 if y0 - th - 8 > 0 else y0 + stroke
        draw.rectangle([x0, ty, x0 + tw + 10, ty + th + 6], fill=colour)
        draw.text((x0 + 5, ty + 2), tag, fill=(255, 255, 255), font=font)
    return out


def crop(img: Image.Image, box: Sequence[int], severity: str = "minor",
         edge: int = 640, pad: float = 0.6, min_frac: float = 0.28) -> Image.Image:
    """
    The evidence and some context around it, with the box drawn in.

    Padded so the defect reads in its setting — a crack is easier to judge
    with the window it runs from — and never smaller than `min_frac` of the
    frame, so a tiny box does not blow up into mush.
    """
    w, h = img.size
    x0, y0, x1, y1 = _px(box, w, h)
    bw, bh = max(1, x1 - x0), max(1, y1 - y0)
    cw = max(bw * (1 + 2 * pad), w * min_frac)
    ch = max(bh * (1 + 2 * pad), h * min_frac)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    left = int(max(0, min(w - cw, cx - cw / 2)))
    top = int(max(0, min(h - ch, cy - ch / 2)))
    right, bottom = int(min(w, left + cw)), int(min(h, top + ch))

    region = img.crop((left, top, right, bottom)).convert("RGB")
    draw = ImageDraw.Draw(region)
    colour = SEVERITY_RGB.get(severity, SEVERITY_RGB["minor"])
    stroke = max(2, round(min(region.size) / 160))
    draw.rectangle([x0 - left, y0 - top, x1 - left, y1 - top], outline=colour, width=stroke)
    region.thumbnail((edge, edge), Image.LANCZOS)
    return region


def roof_overlay(satellite: Image.Image, solar: dict, center: Tuple[float, float],
                 zoom: int, size: int, panels: Optional[int] = None) -> Image.Image:
    """The satellite plate with roof planes outlined and the panel layout drawn."""
    out = satellite.convert("RGB").copy()
    if out.size != (size, size):
        out = out.resize((size, size))
    layer = Image.new("RGBA", out.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    lat, lng = center

    for seg in solar.get("segments") or []:
        if not seg.get("sw") or not seg.get("ne"):
            continue
        ax, ay = solar_mod.project(seg["sw"]["lat"], seg["sw"]["lng"], lat, lng, zoom, size)
        bx, by = solar_mod.project(seg["ne"]["lat"], seg["ne"]["lng"], lat, lng, zoom, size)
        draw.rectangle([min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)],
                       outline=(77, 158, 232, 255), width=2)

    count = panels if panels is not None else len(solar.get("panels") or [])
    for panel in (solar.get("panels") or [])[:count]:
        pts = solar_mod.panel_polygon(panel, solar, lat, lng, zoom, size)
        draw.polygon(pts, fill=(15, 23, 42, 210), outline=(250, 204, 21, 255))

    return Image.alpha_composite(out.convert("RGBA"), layer).convert("RGB")


def issue_marks(photo_label: str, issues: List[dict],
                findings: Dict[str, dict]) -> List[Tuple[str, str, Sequence[int]]]:
    """Every boxed finding in one photo, tagged with the issue it belongs to."""
    marks = []
    for issue in issues:
        for fid in issue["evidence"]:
            f = findings.get(fid)
            if f and f["photo"] == photo_label and f.get("box") and not f.get("dismissed"):
                marks.append((issue["id"], issue["severity"], f["box"]))
    return marks
