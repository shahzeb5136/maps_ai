"""
Pydantic response schemas.

These are handed straight to Gemini as `response_schema`, so the field
descriptions are load-bearing prompt text, not documentation. Renaming or
rewording a description changes what the model returns.
"""

from typing import List, Optional

from pydantic import BaseModel, Field

from .scoring import SYSTEMS_PROMPT

SPACES_PROMPT = (
    "'kitchen', 'living', 'dining', 'bedroom', 'bathroom', 'hallway', 'stairs', "
    "'office', 'laundry', 'utility' (plant room, boiler cupboard), 'basement', 'attic', "
    "'garage', 'exterior_front', 'exterior_rear', 'exterior_side', 'roof', 'garden', "
    "'pool', 'balcony' (also deck, terrace, porch), 'aerial', 'other'"
)


# ── Base inspection ──────────────────────────────────────────────────────────
class RoofInspection(BaseModel):
    shape: str = Field(description="e.g., Gable, Hip, Flat, Mansard, Complex/Multi-pitch")
    material: str = Field(description="e.g., Asphalt Shingle, Spanish Tile, Standing Seam Metal, Slate, Tar & Gravel")
    condition_score_1_to_10: int = Field(description="1 = failing, 10 = immaculate", ge=1, le=10)
    visible_wear_or_damage: List[str]
    solar_panels_detected: bool
    estimated_solar_coverage_pct: int = Field(ge=0, le=100)


class YardAndParcel(BaseModel):
    swimming_pool_detected: bool
    pool_type: Optional[str] = Field(description="'In-ground', 'Above-ground', or None")
    tree_canopy_density: str = Field(description="'Dense', 'Moderate', 'Sparse', or 'None'")
    driveway_type: str
    accessory_structures: List[str]


class ArchitecturalProfile(BaseModel):
    primary_style: str
    estimated_stories: int
    exterior_siding_materials: List[str]
    garage_capacity_estimated_cars: int
    curb_appeal_score_1_to_10: int = Field(ge=1, le=10)
    overall_property_grade: str = Field(
        description="e.g., 'A (Luxury/Pristine)', 'B (Well Maintained)', 'C (Fair)', "
                    "'D (Distressed)', or 'Insufficient imagery'")


class PropertyInspectionReport(BaseModel):
    address: str
    imagery_confidence: str = Field(
        description="'High' (satellite + multiple street views), 'Medium' (partial), "
                    "'Low' (satellite only - facade claims unsupported)")
    architectural_profile: ArchitecturalProfile
    roof_inspection: RoofInspection
    yard_and_parcel: YardAndParcel
    key_selling_points: List[str] = Field(description="3-5 positive exterior/property highlights")
    risk_factors_or_deferred_maintenance: List[str]
    best_exterior_view: str = Field(
        description="The label of the supplied image giving the most complete, straight-on "
                    "view of the building exterior — exactly as labelled above (e.g. "
                    "'owner_photo_1', 'street_facade_front'). Prefer an owner photograph "
                    "when one shows the whole elevation, since those are current. Empty "
                    "string if no image shows the exterior as a whole.")


# ── Temporal ─────────────────────────────────────────────────────────────────
class TemporalSnapshot(BaseModel):
    capture_date: str
    observed_state: str
    changes_since_previous: List[str] = Field(description="Empty for the oldest image")


class TemporalAnalysis(BaseModel):
    timeline: List[TemporalSnapshot]
    roof_replaced_or_repaired: bool
    new_structures_detected: List[str]
    possible_unpermitted_work: List[str]
    vegetation_trend: str = Field(description="'Significant growth', 'Stable', 'Cleared/Removed', or 'Declining'")
    maintenance_trajectory: str = Field(description="'Improving', 'Stable', or 'Declining'")
    upkeep_signal: str = Field(
        description="What the captures say about how the property is being looked "
                    "after: actively maintained, held steady, or left to decay")


# ── Renovation ───────────────────────────────────────────────────────────────
class RenovationScopeItem(BaseModel):
    scope_item: str = Field(description="One concrete piece of work, e.g. 'Replace the timber "
                                        "entry door with a flush oak slab'")
    visual_evidence: str = Field(
        description="What in the SUPPLIED IMAGES makes this item necessary. Name the image "
                    "it is visible in (e.g. 'owner_photo_2', 'street_facade_front') and "
                    "describe what is actually seen there. If no image supports it, do not "
                    "include the item.")
    effort: str = Field(description="'Light', 'Moderate' or 'Substantial' — the labour and "
                                    "disruption relative to the other items. Never a price.")


class RenovationVariant(BaseModel):
    tier: str = Field(description="Which scope tier this concept fulfils")
    concept_name: str = Field(description="Short evocative name for the design direction")
    design_narrative: str = Field(description="2-3 sentences describing the transformation")
    target_buyer: str = Field(description="Who this version of the property is aimed at")
    image_edit_prompt: str = Field(
        description="Image-to-image instruction preserving footprint, camera angle and roofline")
    scope_items: List[RenovationScopeItem]
    overall_effort: str = Field(
        description="'Light', 'Moderate' or 'Substantial' — the scale of the whole concept "
                    "relative to the other two tiers")
    grounded_in_images: List[str] = Field(
        description="Labels of the supplied images this concept was read from, listing any "
                    "owner-supplied photographs first")
    visual_impact: str = Field(
        description="What a person standing in front of the property would notice changing, "
                    "and which currently-visible defect it resolves")
    highest_leverage_move: str
    key_risk: str = Field(description="Main thing that could make this concept underperform")


class RenovationConcepts(BaseModel):
    variants: List[RenovationVariant] = Field(
        description="Exactly three, one per requested tier, lightest scope first")
    recommended_concept_name: str = Field(
        description="Which variant best resolves what the images actually show, weighed "
                    "against its effort and disruption")
    recommendation_rationale: str = Field(
        description="Cite the specific image evidence that drove the choice")


# ── Pinpoint inspection: one photograph at a time ───────────────────────────
class BoxedFinding(BaseModel):
    title: str = Field(description="Short name for the defect, 3-8 words, e.g. 'Water stain on "
                                   "ceiling', 'Missing ridge tile', 'Exposed wiring at junction box'")
    detail: str = Field(description="What exactly is visible and why it matters, 1-2 sentences. "
                                    "Describe only what the photograph shows.")
    system: str = Field(description=f"Building system, one of: {SYSTEMS_PROMPT}")
    severity: str = Field(description="'critical' | 'major' | 'minor' | 'cosmetic' - see the rubric")
    urgency: str = Field(description="'immediate' | 'within_6_months' | 'within_2_years' | 'monitor'")
    trade: str = Field(description="Who should deal with it, e.g. 'Roofer', 'Electrician', "
                                   "'Plumber', 'Structural engineer', 'Damp specialist', "
                                   "'HVAC engineer', 'Glazier', 'Handyman', 'Painter & decorator', "
                                   "'General contractor', 'Pest control', 'Landscaper'")
    recommendation: str = Field(description="The next step, concrete and specific to what is visible")
    confidence: str = Field(description="'high' (clearly visible), 'medium' (likely), or 'low' "
                                        "(possible - needs a closer look)")
    box_2d: List[float] = Field(description="Tight box around the visible evidence as [ymin, xmin, "
                                          "ymax, xmax], each value normalised to 0-1000 over "
                                          "this photograph")


class EquipmentReading(BaseModel):
    kind: str = Field(description="What it is, e.g. 'Gas combi boiler', 'Water heater (tank)', "
                                  "'Furnace', 'AC condenser', 'Heat pump', 'Electrical panel', "
                                  "'Smoke alarm', 'Dishwasher'")
    brand: str = Field(description="Brand exactly as printed, or empty if not legible")
    model: str = Field(description="Model number exactly as printed, or empty")
    serial: str = Field(description="Serial number exactly as printed, or empty")
    manufacture_year: Optional[int] = Field(description="Four-digit year of manufacture, or 0 "
                                                        "when it cannot be determined. Never "
                                                        "guess.")
    age_basis: str = Field(description="'printed on label' | 'decoded from serial' | "
                                       "'estimated from appearance' | 'unknown'")
    label_text: str = Field(description="Other legible ratings worth recording - fuel, capacity, "
                                        "kW/BTU, amperage, refrigerant. Empty if none")
    condition: str = Field(description="Visible condition in one sentence")
    box_2d: List[float] = Field(description="Box around the equipment (or its data plate if that "
                                          "is the clearer read) as [ymin, xmin, ymax, xmax], "
                                          "0-1000")


class PhotoInspection(BaseModel):
    space: str = Field(description=f"Where the photo was taken, one of: {SPACES_PROMPT}")
    space_label: str = Field(description="What a listing would call this space, e.g. 'Kitchen', "
                                         "'Primary bedroom', 'Family bathroom', 'Rear elevation', "
                                         "'Loft', 'Boiler cupboard'")
    is_exterior: bool
    caption: str = Field(description="One factual, specific line that could sit under this "
                                     "photo in a listing")
    description: str = Field(description="2-3 sentences on what the photo shows - layout, "
                                         "fixtures, materials, anything notable. Used later to "
                                         "answer questions about the property, so be concrete.")
    systems_visible: List[str] = Field(description="Every system key this photo gives evidence "
                                                   "about, whether or not anything is wrong")
    features: List[str] = Field(description="Positive features clearly visible, e.g. 'Quartz "
                                            "worktops', 'Vaulted ceiling', 'Walk-in shower', "
                                            "'South-facing garden'. Empty if none")
    materials: List[str] = Field(description="Main finishes visible, e.g. 'Oak flooring', "
                                             "'Painted brick', 'Concrete roof tiles'")
    findings: List[BoxedFinding]
    equipment: List[EquipmentReading]
    photo_quality_1_to_10: int = Field(description="Technical quality of the photograph itself")
    photo_issues: List[str] = Field(description="Problems with the photograph: 'blurry', 'dark', "
                                                "'overexposed', 'tilted', 'cluttered', 'obstructed', "
                                                "'too close', 'low resolution'. Empty if fine")
    retake_tip: str = Field(description="How to retake it better, or empty if it is fine")
    listing_appeal_1_to_10: int = Field(description="How well this works as a marketing photo")
    makeover_potential_1_to_10: int = Field(
        description="How much a redesign render of this photo would show: high for a wide "
                    "interior view of a living space, low for close-ups, plant, and exteriors")


# ── Synthesis: the whole property ────────────────────────────────────────────
class MergedIssue(BaseModel):
    title: str = Field(description="Short name for the issue")
    system: str = Field(description=f"One of: {SYSTEMS_PROMPT}")
    severity: str = Field(description="'critical' | 'major' | 'minor' | 'cosmetic'")
    urgency: str = Field(description="'immediate' | 'within_6_months' | 'within_2_years' | 'monitor'")
    trade: str = Field(description="Who to call")
    summary: str = Field(description="What is wrong, where, and why it matters, 1-3 sentences")
    recommendation: str = Field(description="The next step")
    evidence: List[str] = Field(description="IDs of the photo findings showing this issue, e.g. "
                                            "['P03-1', 'P07-2']. Only IDs from the supplied list")


class SpaceGroup(BaseModel):
    name: str = Field(description="Distinct space name, numbered where there are several, e.g. "
                                  "'Kitchen', 'Bedroom 2', 'Front elevation'")
    kind: str = Field(description=f"One of: {SPACES_PROMPT}")
    photos: List[str] = Field(description="Labels of every photo taken in this space")
    summary: str = Field(description="One or two sentences on the space and its condition")
    condition: str = Field(description="'Good' | 'Fair' | 'Poor' | 'Not enough to judge'")


class CoverageGap(BaseModel):
    area: str = Field(description="The area or system the photos do not show")
    why_it_matters: str
    shot: str = Field(description="Exactly what photograph to take to close the gap")


class InspectionSynthesis(BaseModel):
    verdict: str = Field(description="One-sentence headline judgement of the property's condition")
    executive_summary: str = Field(description="3-5 sentences: overall condition, the issues that "
                                               "matter most, and what to do first")
    issues: List[MergedIssue]
    spaces: List[SpaceGroup]
    strengths: List[str] = Field(description="What is genuinely in good order, each tied to "
                                             "something seen")
    questions_to_ask: List[str] = Field(description="Questions to put to the seller, agent, "
                                                    "landlord or contractor, each prompted by "
                                                    "something visible")
    coverage_gaps: List[CoverageGap]
    dismissed: List[str] = Field(description="Finding IDs judged false positives on review (e.g. "
                                             "a shadow read as a stain). Usually empty")


# ── Listing kit ──────────────────────────────────────────────────────────────
class PhotoNote(BaseModel):
    photo: str = Field(description="Photo label")
    note: str


class ListingKit(BaseModel):
    headline: str = Field(description="Listing headline, under 80 characters")
    description: str = Field(description="Listing description, 150-220 words, warm but factual")
    highlights: List[str] = Field(description="5-8 short feature bullets, each visible in a photo")
    photo_order: List[str] = Field(description="Every usable photo label in the order a listing "
                                               "should show them, best opener first")
    retake: List[PhotoNote] = Field(description="Photos to reshoot before listing, and how")
    captions: List[PhotoNote] = Field(description="A caption for each photo in photo_order")
    pre_listing_fixes: List[str] = Field(description="Quick, visible fixes that would improve the "
                                                     "photos and first impressions")
    social_caption: str = Field(description="A short social-media post announcing the listing")


# ── Ask the property ─────────────────────────────────────────────────────────
class AskCitation(BaseModel):
    photo: str = Field(description="Label of a photo that supports the answer")
    note: str = Field(description="What in that photo supports it")
    box_2d: List[float] = Field(description="Box around the evidence [ymin, xmin, ymax, xmax], "
                                          "0-1000, or an empty list for the whole photo")


class AskAnswer(BaseModel):
    answer: str = Field(description="Direct answer in plain English, 1-6 sentences. Say so "
                                    "plainly when the photos cannot answer it.")
    confidence: str = Field(description="'high' | 'medium' | 'low'")
    citations: List[AskCitation]
    follow_ups: List[str] = Field(description="Up to 3 short follow-up questions worth asking")
