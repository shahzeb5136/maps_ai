"""
Reimagine: image-to-image renders of the owner's own rooms.

Two uses of the same machinery. During a scan, a handful of the best interior
shots are restyled in the style the user picked, with any defects the
inspection found in them shown repaired — so the report closes on what the
place could be, not only on what is wrong with it. After a scan, the studio
endpoint renders any photo on demand: restyle, virtual staging, decluttering,
a repair preview, a twilight exterior, or a free instruction.

Every instruction ends with the same preservation clause. The value of a
render is that it is recognisably *this* room; a model left to its own
devices moves the window.
"""

import concurrent.futures
import logging
from typing import Dict, List, Optional, Tuple

from google import genai
from PIL import Image

from . import gemini

log = logging.getLogger(__name__)

# key -> (name, what the image model is told the style is made of)
STYLES: Dict[str, Tuple[str, str]] = {
    "modern_organic": ("Modern Organic",
                       "warm white-oak joinery and flooring, limewashed walls in warm white, "
                       "boucle and linen upholstery, travertine and stone accents, sculptural "
                       "plaster and paper lighting, olive trees and trailing plants"),
    "scandinavian": ("Scandinavian",
                     "pale ash and birch, crisp white walls, oatmeal and soft-grey textiles, "
                     "wool throws, simple tapered furniture, matte black accents, uncluttered "
                     "surfaces, bright airy light"),
    "japandi": ("Japandi",
                "low-profile furniture in natural oak and dark-stained timber, rice-paper "
                "lantern lighting, warm neutral plaster walls, linen in sand and charcoal, "
                "handmade ceramics, a single branch arrangement, calm restraint"),
    "contemporary_luxe": ("Contemporary Luxe",
                          "book-matched marble, brushed brass details, deep jewel-toned velvet "
                          "upholstery, fluted timber panelling, a statement pendant, layered "
                          "warm lighting"),
    "industrial": ("Industrial Loft",
                   "black steel framing and fixtures, reclaimed timber, cognac leather, "
                   "polished-concrete tones, exposed-filament lighting, deep green and grey"),
    "coastal": ("Coastal",
                "whitewashed timber, a sand and seafoam palette, rattan and woven textures, "
                "linen slipcovers, light oak floors, breezy natural light"),
    "mid_century": ("Mid-Century Modern",
                    "teak and walnut furniture with tapered legs, mustard, teal and "
                    "burnt-orange accents, globe and sputnik lighting, a geometric rug"),
    "classic": ("Classic Traditional",
                "panelled walls in a heritage paint colour, rich timber furniture, tailored "
                "upholstery, brass picture lights, a patterned rug, symmetrical arrangement"),
}
DEFAULT_STYLE = "modern_organic"

MODES: Dict[str, str] = {
    "restyle": "Redesign the space in a chosen style",
    "stage": "Furnish an empty or sparse room",
    "declutter": "Clear clutter and personal items",
    "repair": "Show the visible defects fixed",
    "twilight": "Turn an exterior into a dusk shot",
    "custom": "Your own instruction",
}

# Spaces a redesign render says little about.
_NOT_MAKEOVER = {"utility", "garage", "attic", "basement", "stairs", "laundry", "roof",
                 "aerial", "other"}

_PRESERVE_INTERIOR = (
    "This is a photo edit, not a new scene. Keep the exact camera position, lens, perspective "
    "and framing. Keep the architecture unchanged: walls, ceiling height, window and door "
    "openings, stairs, beams, radiators' positions and the view outside. Photorealistic "
    "interior photography, light consistent with the original, no text, logos, people or "
    "watermarks."
)
_PRESERVE_EXTERIOR = (
    "This is a photo edit, not a new scene. Keep the exact camera position, perspective and "
    "framing. Keep the building's footprint, roofline, window and door openings, neighbouring "
    "buildings and the street unchanged. Photorealistic architectural photography, no text, "
    "logos, people or watermarks."
)


def style_name(key: str) -> str:
    return STYLES.get(key, STYLES[DEFAULT_STYLE])[0]


def instruction(mode: str, space: str, is_exterior: bool, style: str = DEFAULT_STYLE,
                defects: Optional[List[str]] = None, custom: str = "") -> str:
    name, desc = STYLES.get(style, STYLES[DEFAULT_STYLE])
    space = (space or ("exterior" if is_exterior else "room")).lower()
    preserve = _PRESERVE_EXTERIOR if is_exterior else _PRESERVE_INTERIOR
    fixes = [d for d in (defects or []) if d][:6]

    if mode == "twilight":
        return ("Convert this daytime exterior photograph into a twilight (blue hour) shot: a deep "
                "blue dusk sky with a soft warm glow at the horizon, warm interior light in every "
                "window, exterior and garden lighting switched on, wet-look highlights on paving "
                "kept subtle. " + preserve)
    if mode == "declutter":
        return (f"Declutter this {space}: remove personal items, loose objects, cables, clutter on "
                "surfaces and floors, and surplus small furniture. Keep the main furniture, "
                "fixtures, finishes and layout exactly as they are, tidied and straightened. "
                + preserve)
    if mode == "repair":
        what = ("these visible defects professionally repaired: " + "; ".join(fixes) + "."
                if fixes else "everything visibly worn or damaged professionally made good: "
                "cracks filled, stains removed, sealant renewed, paint refreshed.")
        return (f"Show this {space} with {what} Change nothing else - the same furniture, decor, "
                "finishes and layout. " + preserve)
    if mode == "stage":
        return (f"Virtually stage this {space}: furnish it fully and tastefully in a {name} style "
                f"({desc}), with furniture scaled correctly to the room and arranged to suit its "
                "layout. Keep fixed elements - flooring, kitchens, built-ins - as they are. "
                + preserve)
    if mode == "custom":
        return f"{custom.strip()}. " + preserve

    # restyle
    if is_exterior:
        body = (f"Restyle the exterior of this building in a {name} spirit ({desc}, interpreted "
                "for an exterior): new facade finishes, colours, front door, lighting and "
                "planting.")
    else:
        body = (f"Redesign this {space} in a {name} style: {desc}. Replace the furniture, decor, "
                "soft furnishings, light fittings and wall and floor finishes to suit the style.")
    if fixes:
        body += " Also show these visible defects repaired: " + "; ".join(fixes) + "."
    return body + " " + preserve


def pick_candidates(photos: List[dict], spaces: List[dict], count: int) -> List[dict]:
    """
    The interior shots most worth restyling: wide views of living spaces,
    one per distinct space, best first.
    """
    space_of = {label: sp["name"] for sp in spaces for label in sp["photos"]}
    ranked = sorted(
        (p for p in photos
         if p["source"] == "owner" and not p.get("error") and not p["is_exterior"]
         and p["space"] not in _NOT_MAKEOVER and p["makeover_potential"] >= 5),
        key=lambda p: (-p["makeover_potential"], -p["listing_appeal"]),
    )
    picked, used = [], set()
    for p in ranked:
        room = space_of.get(p["label"], p["space_label"]).lower()
        if room in used:
            continue
        used.add(room)
        picked.append(p)
        if len(picked) >= count:
            break
    return picked


def render(client: genai.Client, image: Image.Image, text: str,
           label: str = "render") -> Optional[Image.Image]:
    return gemini.edit_image(client, image, text, label=label)


def render_makeovers(client: genai.Client, candidates: List[dict],
                     images: Dict[str, Image.Image], style: str,
                     spaces: List[dict]) -> List[Tuple[dict, Optional[Image.Image]]]:
    """Restyle every candidate in parallel, defects shown fixed."""
    space_of = {label: sp["name"] for sp in spaces for label in sp["photos"]}

    def one(p: dict) -> Tuple[dict, Optional[Image.Image]]:
        defects = [f["title"] for f in p["findings"] if not f["dismissed"]]
        room = space_of.get(p["label"], p["space_label"])
        text = instruction("restyle", room, False, style, defects)
        img = render(client, images[p["label"]], text, label=f"makeover {p['label']}")
        meta = {
            "source": p["label"],
            "space": room,
            "style": style,
            "style_name": style_name(style),
            "title": f"{style_name(style)} {room.lower()}",
            "fixes": defects[:6],
        }
        return meta, img

    out: List[Tuple[dict, Optional[Image.Image]]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(candidates))) as ex:
        for result in ex.map(one, candidates):
            out.append(result)
    return out
