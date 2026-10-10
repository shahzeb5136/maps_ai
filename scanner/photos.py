"""
Pinpoint inspection: every photograph inspected on its own.

This is the stage that turns a pile of photos into evidence. Each image gets
its own vision call — a model shown one picture draws tight boxes, a model
shown fifty draws vague ones — and comes back as a set of located findings,
any equipment it can read the data plate of, the space it shows, and how good
a photograph it is. Calls run in parallel and report as they land, which is
what drives the live feed the user watches while the scan runs.

The model's labels are free text, so everything is normalised here onto the
fixed vocabularies in `scoring` before anything downstream counts it.
"""

import concurrent.futures
import logging
import re
from typing import Callable, Dict, List, Optional, Tuple

from google import genai
from PIL import Image

from . import config, gemini
from .imagery import OWNER_PREFIX
from .models import PhotoInspection
from .scoring import (
    SYSTEM_LABEL,
    clamp_int,
    equipment_age,
    normalise_box,
    normalise_confidence,
    normalise_severity,
    normalise_system,
    normalise_urgency,
)

log = logging.getLogger(__name__)

# Google plates worth inspecting like photographs. The satellite plate is the
# only view of most roofs; the front Street View plate is the only view of the
# facade when the owner supplied none. Flank plates mostly show the street.
GOOGLE_INSPECTED = ("satellite_topdown", "street_facade_front")

MAX_FINDINGS_PER_PHOTO = 15

SPACES = {
    "kitchen", "living", "dining", "bedroom", "bathroom", "hallway", "stairs", "office",
    "laundry", "utility", "basement", "attic", "garage", "exterior_front", "exterior_rear",
    "exterior_side", "roof", "garden", "pool", "balcony", "aerial", "other",
}

GOALS = {
    "buying": ("Buying", "The reader is deciding whether to buy this property. Be thorough "
               "about anything that would be expensive, disruptive or dangerous to put right."),
    "selling": ("Selling", "The reader is preparing to sell. Flag what a buyer's surveyor would "
                "raise and what will photograph or show poorly."),
    "owning": ("Owning", "The reader owns this property and wants to look after it well. "
               "Prioritise preventive maintenance and anything getting worse."),
    "renovating": ("Renovating", "The reader plans to renovate. Note the condition of what "
                   "would be kept, and anything that would complicate the work."),
}
DEFAULT_GOAL = "buying"

RUBRIC = """Severity rubric:
- critical: a safety hazard, or active damage that will get worse - exposed live wiring, an
  active leak, structural movement, a missing guard at a drop, a gas or carbon-monoxide risk,
  a large mould colony.
- major: a significant defect needing a specialist or a substantial repair - a failing roof
  covering, rot in structural timber, significant cracking, failed flashing, equipment
  visibly at the end of its life.
- minor: a repair one trade can deal with - failed sealant, a loose fitting, a dripping tap,
  a small patch of rot, blocked gutters, a cracked tile.
- cosmetic: appearance only - scuffs, worn paint, tired but working finishes.
Dated or unfashionable is not a defect, and a style preference is not a finding.

Urgency: 'immediate' (safety or active damage), 'within_6_months', 'within_2_years',
or 'monitor' (watch for change - e.g. an old stain that may be historic)."""


def photo_ref(label: str) -> str:
    """Short, stable code a finding ID is built from: P07, SAT, SVF."""
    if label.startswith(OWNER_PREFIX):
        tail = label.rsplit("_", 1)[-1]
        return f"P{int(tail):02d}" if tail.isdigit() else "P00"
    if label.startswith("satellite"):
        return "SAT"
    if label == "street_facade_front":
        return "SVF"
    return re.sub(r"[^A-Z0-9]", "", label.upper())[:4] or "IMG"


def source_of(label: str) -> str:
    if label.startswith(OWNER_PREFIX):
        return "owner"
    if label.startswith("satellite"):
        return "satellite"
    return "street_view"


def _source_note(label: str, street_date: Optional[str]) -> str:
    src = source_of(label)
    if src == "owner":
        return ("a photograph supplied with the scan - treat it as current and as the closest "
                "look available")
    if src == "satellite":
        return ("an overhead satellite image centred on the parcel - use it for the roof "
                "covering and roof features, the plot, outbuildings, pools and trees. "
                "Neighbouring buildings are not the subject")
    when = f" captured {street_date}" if street_date else ""
    return (f"a Google Street View capture{when} aimed at the building - it may be out of "
            "date, so report only what it clearly shows")


def _prompt(label: str, index: int, total: int, address: str, goal: str,
            street_date: Optional[str]) -> str:
    goal_note = GOALS.get(goal, GOALS[DEFAULT_GOAL])[1]
    return f"""You are a meticulous chartered building surveyor and certified home inspector.
This is image {index} of {total} supplied for "{address}": {_source_note(label, street_date)}.
{goal_note}

Inspect THIS image only and report:
1. Every defect, hazard or maintenance item you can actually see, each as a finding with a
   tight box around the visible evidence. Look closely - ceilings for stains, corners and
   window reveals for mould, joints for failed sealant, walls and render for cracks, timber
   for rot, sockets and switches for damage, balustrades, steps, roof coverings, flashing,
   gutters, pointing.
2. Any equipment - boiler, furnace, water heater, AC or heat-pump unit, electrical panel,
   smoke or CO alarm, major appliances. Transcribe legible data-plate text exactly. Give
   manufacture_year only if it is printed, or reliably decodable from that manufacturer's
   serial-number format; say which in age_basis. Otherwise 0.
3. Which building systems the image gives evidence about, the space it shows, and how good
   a photograph it is.

{RUBRIC}

Rules:
- Only what is visible here. Do not infer from the address, the type of building, or what is
  typical. If a mark could be historic or active (a stain, a crack), report it at 'medium' or
  'low' confidence and recommend how to check.
- Boxes are [ymin, xmin, ymax, xmax] on a 0-1000 grid over this image, tight around the
  evidence - not the whole room.
- An image that shows nothing wrong has zero findings. Do not pad.
- No prices, costs, budgets or valuations, anywhere.
Return strict JSON per the schema."""


def _space(raw: str, is_exterior: bool) -> str:
    key = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if key in SPACES:
        return key
    for space in SPACES:
        if space in key:
            return space
    return "exterior_front" if is_exterior else "other"


def _clean(text, limit: int = 600) -> str:
    return " ".join(str(text or "").split())[:limit]


def normalise(label: str, raw: PhotoInspection) -> dict:
    """Map one model reply onto the payload's fixed vocabulary, with IDs."""
    ref = photo_ref(label)
    fallback_system = "exterior" if raw.is_exterior else "interior"
    if label.startswith("satellite"):
        fallback_system = "roof"

    findings = []
    for f in raw.findings[:MAX_FINDINGS_PER_PHOTO]:
        title = _clean(f.title, 120)
        if not title:
            continue
        severity = normalise_severity(f.severity)
        findings.append({
            "id": f"{ref}-{len(findings) + 1}",
            "photo": label,
            "title": title,
            "detail": _clean(f.detail),
            "system": normalise_system(f.system or title, fallback_system),
            "severity": severity,
            "urgency": normalise_urgency(f.urgency, severity),
            "trade": _clean(f.trade, 60) or "General contractor",
            "recommendation": _clean(f.recommendation),
            "confidence": normalise_confidence(f.confidence),
            "box": normalise_box(f.box_2d),
            "dismissed": False,
        })

    equipment = []
    for e in raw.equipment[:8]:
        kind = _clean(e.kind, 80)
        if not kind:
            continue
        item = equipment_age({
            "id": f"{ref}-E{len(equipment) + 1}",
            "photo": label,
            "kind": kind,
            "brand": _clean(e.brand, 60),
            "model": _clean(e.model, 60),
            "serial": _clean(e.serial, 60),
            "manufacture_year": e.manufacture_year or 0,
            "age_basis": _clean(e.age_basis, 40) or "unknown",
            "label_text": _clean(e.label_text, 200),
            "condition": _clean(e.condition, 300),
            "box": normalise_box(e.box_2d),
        })
        equipment.append(item)

    # A photo of a boiler is evidence about the heating even if the model
    # forgot to say so; equipment counts towards coverage for its system.
    systems_visible = sorted({
        s for s in (normalise_system(x, "") for x in
                    list(raw.systems_visible) + [e["kind"] for e in equipment])
        if s in SYSTEM_LABEL
    } | {f["system"] for f in findings})

    return {
        "label": label,
        "ref": ref,
        "source": source_of(label),
        "space": _space(raw.space, raw.is_exterior),
        "space_label": _clean(raw.space_label, 60) or ("Exterior" if raw.is_exterior else "Interior"),
        "is_exterior": bool(raw.is_exterior),
        "caption": _clean(raw.caption, 200),
        "description": _clean(raw.description, 900),
        "systems_visible": systems_visible,
        "features": [_clean(x, 80) for x in raw.features[:12] if _clean(x, 80)],
        "materials": [_clean(x, 80) for x in raw.materials[:12] if _clean(x, 80)],
        "findings": findings,
        "equipment": equipment,
        "quality": clamp_int(raw.photo_quality_1_to_10, 1, 10, 6),
        "photo_issues": [_clean(x, 40) for x in raw.photo_issues[:6] if _clean(x, 40)],
        "retake_tip": _clean(raw.retake_tip, 240),
        "listing_appeal": clamp_int(raw.listing_appeal_1_to_10, 1, 10, 5),
        "makeover_potential": clamp_int(raw.makeover_potential_1_to_10, 1, 10, 3),
        "error": None,
    }


def failed(label: str, reason: str) -> dict:
    """Placeholder for a photo whose inspection call failed outright. The photo
    still appears in the report; it just carries no findings."""
    return {
        "label": label, "ref": photo_ref(label), "source": source_of(label),
        "space": "other", "space_label": "Unclassified", "is_exterior": False,
        "caption": "", "description": "", "systems_visible": [], "features": [],
        "materials": [], "findings": [], "equipment": [], "quality": 0,
        "photo_issues": [], "retake_tip": "", "listing_appeal": 0,
        "makeover_potential": 0, "error": reason,
    }


def inspect_photo(client: genai.Client, label: str, image: Image.Image, index: int,
                  total: int, address: str, goal: str,
                  street_date: Optional[str] = None) -> dict:
    raw = gemini.structured(
        client, PhotoInspection,
        [f"IMAGE {label}", image,
         _prompt(label, index, total, address, goal, street_date)],
        temperature=0.1, label=f"inspect {label}",
    )
    return normalise(label, raw)


PhotoDone = Callable[[dict, int, int], None]


def inspect_all(client: genai.Client, images: List[Tuple[str, Image.Image]], address: str,
                goal: str, street_date: Optional[str] = None,
                on_done: Optional[PhotoDone] = None) -> List[dict]:
    """
    Inspect every image in parallel; results come back in input order.

    A photo whose call fails is kept with `error` set rather than failing the
    scan — one unreadable photo out of fifty should cost the user that photo,
    not the report.
    """
    total = len(images)
    results: Dict[str, dict] = {}
    done = 0

    with concurrent.futures.ThreadPoolExecutor(max_workers=config.VISION_CONCURRENCY) as ex:
        futures = {
            ex.submit(inspect_photo, client, label, img, i + 1, total, address, goal,
                      street_date): label
            for i, (label, img) in enumerate(images)
        }
        for fut in concurrent.futures.as_completed(futures):
            label = futures[fut]
            try:
                results[label] = fut.result()
            except Exception as exc:  # noqa: BLE001
                log.warning("Inspection of %s failed: %s", label, exc)
                results[label] = failed(label, "This photo could not be analysed.")
            done += 1
            if on_done:
                try:
                    on_done(results[label], done, total)
                except Exception as exc:  # noqa: BLE001 - a UI update never fails a scan
                    log.debug("on_done callback failed: %s", exc)

    return [results[label] for label, _ in images]
