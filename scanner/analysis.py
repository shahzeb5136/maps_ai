"""
The three Gemini stages: condition assessment, temporal diffing, and the
renovation concepts plus their image-to-image renders.

Prompts are copied verbatim from the original script — they are tuned, and
rewording them changes the output quality, not just its phrasing.
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
        if label.startswith(OWNER_PREFIX):
            contents.append(
                f"Image Perspective: {label.replace('_', ' ').upper()} "
                "(SUPPLIED BY THE PROPERTY OWNER)"
            )
        else:
            contents.append(f"Image Perspective: {label.replace('_', ' ').upper()}")
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

Cross-reference overhead and horizontal views. Do not report features you cannot actually
see. Set imagery_confidence honestly. Return strict JSON per the schema.
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
- Maintenance trajectory: is the owner investing, or is the asset decaying?

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
                              client: genai.Client) -> RenovationConcepts:
    """One text call produces all three tiers, so they're costed against each other."""
    tier_spec = "\n".join(
        f"{i+1}. {name} - {desc}" for i, (name, desc) in enumerate(config.RENOVATION_TIERS)
    )

    prompt = f"""
You are a design-build general contractor and renovation ROI analyst.

Property: "{address}"
Current assessment: {report.architectural_profile.primary_style}, grade
{report.architectural_profile.overall_property_grade}, curb appeal
{report.architectural_profile.curb_appeal_score_1_to_10}/10, roof condition
{report.roof_inspection.condition_score_1_to_10}/10.
Known issues: {', '.join(report.risk_factors_or_deferred_maintenance) or 'none noted'}

Produce THREE distinct EXTERIOR-ONLY renovation concepts, one per tier, cheapest first:

{tier_spec}

Rules:
- The three must be VISUALLY distinct, not the same design at three price points.
  Different material palettes, different colour stories, different target buyers.
- Cost every line item in USD using local market labor rates and visible square footage.
- Budgets must escalate meaningfully across tiers and reflect genuinely different scopes.
- No changes to building footprint or roofline - facade, materials, windows, doors,
  paint, lighting, driveway and landscaping only.
- estimated_value_uplift_usd must be a defensible appraisal delta, not wishful thinking.
  Diminishing returns are real: the most expensive tier often has the WORST ROI, and you
  should say so if that is the case here.
- Each image_edit_prompt must instruct an image model to edit THIS EXACT photo: preserve
  camera angle, perspective, structure, neighbours and sky; change only the specified
  finishes. Be concrete about materials and colors.
- recommended_concept_name must be the best RISK-ADJUSTED return, not simply the highest ROI.

Return strict JSON per the schema.
"""
    resp = client.models.generate_content(
        model=config.TEXT_MODEL,
        contents=[facade, prompt],
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
