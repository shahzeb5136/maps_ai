"""
The three Gemini stages: condition assessment, temporal diffing, and the
renovation concepts plus their image-to-image renders.

Every stage is grounded in the supplied photographs and nothing else. The
prompts say so repeatedly and deliberately: this pipeline sees a handful of
images, so it reports what is visible in them and refuses the questions
pictures cannot answer.

The prompts are tuned; rewording them changes the output quality, not just
its phrasing.
"""

import concurrent.futures
import io
import logging
from typing import Dict, List, Optional, Tuple

from google import genai
from google.genai import types
from PIL import Image

from . import config
from .imagery import OWNER_PREFIX, has_ground_imagery, has_owner_photos
from .models import (
    PropertyInspectionReport,
    RenovationConcepts,
    RenovationVariant,
    TemporalAnalysis,
)

log = logging.getLogger(__name__)

# Gemini would otherwise try to call our pydantic models as tools.
NO_AFC = types.AutomaticFunctionCallingConfig(disable=True)


def make_client(api_key: str) -> genai.Client:
    return genai.Client(api_key=api_key)


# ── 1. Base inspection ───────────────────────────────────────────────────────
def analyze_property(address: str, images: Dict[str, Image.Image],
                     client: genai.Client) -> PropertyInspectionReport:
    contents: List[object] = []
    for label, img in images.items():
        # The raw key rides along with the display label because
        # best_exterior_view has to come back as something we can look up.
        if label.startswith(OWNER_PREFIX):
            contents.append(
                f"Image Perspective: {label.replace('_', ' ').upper()} "
                f"(label: {label}) (SUPPLIED BY THE PROPERTY OWNER)"
            )
        else:
            contents.append(
                f"Image Perspective: {label.replace('_', ' ').upper()} (label: {label})"
            )
        contents.append(img)

    contents.append(f"""
You are a Senior Geospatial Real Estate Appraiser and Property Condition Assessor.
Target Address: "{address}"

Supplied imagery:
- Top-down satellite: parcel footprint, roof shape/area, pools, yard, detached structures, canopy.
- Street View perspectives (if present): facade, story count, garage, siding, curb appeal,
  deferred maintenance.
- Owner-supplied photographs (if present): taken deliberately by the owner, so they are
  usually current and often show what the other sources cannot - close-up material
  condition, elevations hidden from the road, damage, or recent work.

Cross-reference overhead and horizontal views. The images above are your only evidence:
every field must be traceable to something visible in one of them. Do not report features
you cannot actually see, and do not infer them from the address, the neighbourhood or the
apparent class of the building. Set imagery_confidence honestly.

For best_exterior_view, name the single image that a designer would work from - the most
complete, least obstructed view of the building's exterior - using its label exactly as
given above. Return strict JSON per the schema.
""")

    if has_owner_photos(images):
        contents.append(
            "NOTE ON OWNER PHOTOGRAPHS: these are the most recent and most detailed view "
            "of the property, so prefer them over satellite or Street View wherever they "
            "disagree - Street View captures can be years stale. Two cautions. They are "
            "not independently verified and the owner chose what to photograph, so absence "
            "of a defect in them is not evidence the defect is absent; keep basing "
            "whole-property judgements on the full image set. And if an owner photo "
            "plainly shows a different building from the satellite footprint, say so in "
            "risk_factors_or_deferred_maintenance rather than silently reconciling them."
        )

    if not has_ground_imagery(images):
        contents.append(
            "NOTE: No ground-level imagery is available - neither Street View nor owner "
            "photographs. Assess ONLY what the overhead view supports. Set "
            "imagery_confidence to 'Low', curb_appeal_score_1_to_10 to 1, and "
            "overall_property_grade to 'Insufficient imagery' rather than guessing facade "
            "condition. Leave facade-dependent fields empty or explicitly marked unknown."
        )

    resp = client.models.generate_content(
        model=config.TEXT_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=PropertyInspectionReport,
            temperature=0.1,
            automatic_function_calling=NO_AFC,
        ),
    )
    return PropertyInspectionReport.model_validate_json(resp.text)


# ── 2. Temporal ──────────────────────────────────────────────────────────────
def analyze_timeline(address: str, timeline: List[Tuple[str, Image.Image]],
                     client: genai.Client) -> TemporalAnalysis:
    contents: List[object] = []
    for date, img in timeline:
        contents.append(f"--- STREET VIEW CAPTURE DATE: {date} ---")
        contents.append(img)

    contents.append(f"""
You are a forensic property change-detection analyst reviewing a chronological
series of Street View captures of: "{address}"

Images are supplied OLDEST FIRST. Compare them frame by frame and report:
- Roof: replaced, re-coated, aged, or unchanged?
- New construction: extensions, ADUs, sheds, carports, patios, boundary walls, pools.
  Flag anything appearing abruptly as POSSIBLE UNPERMITTED WORK if it looks structural
  rather than routine maintenance.
- Vegetation: canopy growth (root/foundation risk, fire fuel) vs clearing.
- Upkeep: is the property being actively looked after, or visibly decaying?

State WHICH date range each change occurred in. Camera position and image quality vary
between captures - if that makes a comparison unreliable, say so rather than inventing
change. Return strict JSON per the schema.
""")

    resp = client.models.generate_content(
        model=config.TEXT_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=TemporalAnalysis,
            temperature=0.1,
            automatic_function_calling=NO_AFC,
        ),
    )
    return TemporalAnalysis.model_validate_json(resp.text)


# ── 3. Renovation ────────────────────────────────────────────────────────────
def build_renovation_concepts(address: str, facade: Image.Image,
                              report: PropertyInspectionReport,
                              client: genai.Client,
                              facade_label: str = "",
                              images: Optional[Dict[str, Image.Image]] = None,
                              ) -> RenovationConcepts:
    """
    One text call produces all three tiers, so they are scoped against each other.

    The base photo goes in first because it is what the image model will edit,
    but every owner-supplied photograph goes in beside it. Those close-ups are
    what turn a concept from a plausible-sounding design into one addressed to
    this building's actual peeling paint, cracked tile and rusted railing — the
    detail no satellite frame and no years-old Street View capture carries.
    """
    tier_spec = "\n".join(
        f"{i+1}. {name} - {desc}" for i, (name, desc) in enumerate(config.RENOVATION_TIERS)
    )

    images = images or {}
    contents: List[object] = [
        f"BASE PHOTOGRAPH (label: {facade_label or 'facade'}) - this exact photo will be "
        "edited to render the concept. Read the building from it.",
        facade,
    ]

    owner_labels = [k for k in sorted(images) if k.startswith(OWNER_PREFIX)]
    extra_owner = [k for k in owner_labels if images[k] is not facade]
    for label in extra_owner:
        contents.append(
            f"OWNER-SUPPLIED PHOTOGRAPH (label: {label}) - taken by the owner, current, "
            "and the closest look available at real material condition."
        )
        contents.append(images[label])

    evidence_note = (
        f"You have {len(extra_owner)} additional owner photograph(s) beyond the base "
        "photo. They are the ground truth for material condition: where they disagree "
        "with the assessment summary or with a Street View capture, believe the "
        "photographs. Draw scope from what they actually show."
        if extra_owner else
        "No owner photographs were supplied beyond the base photo, so the base photo is "
        "your only close look at the building. Keep the scope to defects and materials "
        "visible in it, and say so honestly in visual_evidence rather than inventing "
        "detail the photo does not resolve."
    )

    prompt = f"""
You are a design-build general contractor briefing a client on exterior work.

Property: "{address}"
Assessment read from the same imagery: {report.architectural_profile.primary_style}, grade
{report.architectural_profile.overall_property_grade}, curb appeal
{report.architectural_profile.curb_appeal_score_1_to_10}/10, roof condition
{report.roof_inspection.condition_score_1_to_10}/10.
Known issues: {', '.join(report.risk_factors_or_deferred_maintenance) or 'none noted'}

{evidence_note}

Produce THREE distinct EXTERIOR-ONLY renovation concepts, one per tier, lightest first:

{tier_spec}

Rules:
- THE PHOTOGRAPHS ARE THE BRIEF. Every scope item must answer something you can point to
  in a supplied image, and visual_evidence must name that image and say what is visible
  in it. Drop any item you cannot ground that way, however conventional it sounds.
- Work from the photographs, not from assumptions about the address, the neighbourhood,
  or what buildings of this type usually need.
- The three must be VISUALLY distinct, not one design at three levels of intensity.
  Different material palettes, different colour stories, different target buyers.
- Scope must escalate meaningfully across tiers: the tiers differ in how much of the
  envelope is touched and how much disruption that causes, expressed through `effort`
  and `overall_effort` as Light / Moderate / Substantial.
- NEVER state or imply money. No prices, budgets, quotes, currency figures, value uplift,
  resale gain, ROI, payback or "worth it" arithmetic. A handful of photographs cannot
  support any of that, and a number invented from them would be a fabrication. Describe
  scope, effort and visible impact instead, and let the reader price it locally.
- No changes to building footprint or roofline - facade, materials, windows, doors,
  paint, lighting, driveway and landscaping only.
- Each image_edit_prompt must instruct an image model to edit THE BASE PHOTOGRAPH exactly:
  preserve camera angle, perspective, structure, neighbours and sky; change only the
  specified finishes. Be concrete about materials and colors.
- grounded_in_images must list the labels you actually read the concept from, owner
  photographs first.
- recommended_concept_name is the concept that best resolves the defects visible in the
  photographs for the effort it demands - not simply the largest one.

Return strict JSON per the schema.
"""
    contents.append(prompt)

    resp = client.models.generate_content(
        model=config.TEXT_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RenovationConcepts,
            temperature=0.6,          # higher: we want the three to diverge
            automatic_function_calling=NO_AFC,
        ),
    )
    return RenovationConcepts.model_validate_json(resp.text)


def render_variant(facade: Image.Image, variant: RenovationVariant,
                   client: genai.Client) -> Optional[Image.Image]:
    instruction = (
        f"{variant.image_edit_prompt}\n\n"
        "CRITICAL: this is a photo edit, not a new scene. Keep the exact same camera position, "
        "focal length, building geometry, roofline, window openings and background. "
        "Photorealistic architectural photography, natural daylight, no text or watermarks."
    )
    try:
        resp = client.models.generate_content(
            model=config.IMAGE_MODEL, contents=[facade, instruction]
        )
        for part in resp.candidates[0].content.parts:
            if getattr(part, "inline_data", None) and part.inline_data.data:
                return Image.open(io.BytesIO(part.inline_data.data)).convert("RGB")
        log.warning("No image returned for concept '%s'", variant.concept_name)
    except Exception as exc:
        log.warning("Failed to render concept '%s': %s", variant.concept_name, exc)
    return None


def render_all_variants(facade: Image.Image, concepts: RenovationConcepts,
                        client: genai.Client) -> List[Optional[Image.Image]]:
    """Three image calls in parallel - roughly the latency of one."""
    rendered: List[Optional[Image.Image]] = [None] * len(concepts.variants)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(render_variant, facade, v, client): i
                   for i, v in enumerate(concepts.variants)}
        for fut in concurrent.futures.as_completed(futures):
            rendered[futures[fut]] = fut.result()
    return rendered
