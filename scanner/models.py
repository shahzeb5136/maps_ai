"""
Pydantic response schemas.

These are handed straight to Gemini as `response_schema`, so the field
descriptions are load-bearing prompt text, not documentation. Renaming or
rewording a description changes what the model returns.
"""

from typing import List, Optional

from pydantic import BaseModel, Field


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
