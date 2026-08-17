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
    investment_signal: str


# ── Renovation ───────────────────────────────────────────────────────────────
class RenovationLineItem(BaseModel):
    scope_item: str
    estimated_cost_usd: int
    reasoning: str


class RenovationVariant(BaseModel):
    tier: str = Field(description="Which budget tier this concept fulfils")
    concept_name: str = Field(description="Short evocative name for the design direction")
    design_narrative: str = Field(description="2-3 sentences describing the transformation")
    target_buyer: str = Field(description="Who this version of the property is aimed at")
    image_edit_prompt: str = Field(
        description="Image-to-image instruction preserving footprint, camera angle and roofline")
    line_items: List[RenovationLineItem]
    total_estimated_cost_usd: int
    estimated_value_uplift_usd: int
    estimated_roi_pct: int = Field(description="(uplift - cost) / cost * 100. May be negative.")
    highest_leverage_move: str
    key_risk: str = Field(description="Main thing that could make this concept underperform")


class RenovationConcepts(BaseModel):
    variants: List[RenovationVariant] = Field(description="Exactly three, one per requested tier, cheapest first")
    recommended_concept_name: str = Field(description="Which variant offers the best risk-adjusted return")
    recommendation_rationale: str
