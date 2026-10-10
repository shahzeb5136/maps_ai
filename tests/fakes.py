"""
A stand-in for the Gemini client and the Google Maps fetches.

Answers each response schema with plausible, deterministic content — boxed
findings, a data plate, a duplicate defect for the synthesis to merge — so the
whole pipeline, the PDF and the API can run end to end with no keys and no
network. The image model "renders" by tinting its input.
"""

import io
import json
import re
from types import SimpleNamespace
from typing import Dict, List

from google.genai import errors
from PIL import Image, ImageDraw


def make_photo(seed: int, size=(1200, 900)) -> Image.Image:
    """A synthetic 'room': a gradient wall, a floor, a window, a dark stain."""
    w, h = size
    img = Image.new("RGB", size, (200, 190, 175))
    d = ImageDraw.Draw(img)
    for y in range(h):
        shade = int(170 + 50 * y / h)
        d.line([(0, y), (w, y)], fill=(shade, shade - 8 - seed * 3 % 20, shade - 20))
    d.rectangle([0, int(h * 0.7), w, h], fill=(120 + seed * 7 % 60, 90, 70))
    d.rectangle([int(w * 0.6), int(h * 0.15), int(w * 0.85), int(h * 0.5)], fill=(170, 210, 240))
    d.ellipse([int(w * 0.15), int(h * 0.1), int(w * 0.3), int(h * 0.22)], fill=(110, 100, 80))
    return img


def jpeg_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def _label(contents: List) -> str:
    for c in contents:
        if isinstance(c, str):
            m = re.match(r"IMAGE (\S+)", c)
            if m:
                return m.group(1)
    return ""


def _all_text(contents: List) -> str:
    return "\n".join(c for c in contents if isinstance(c, str))


def _finding(title, system, severity, urgency, trade, box, confidence="high"):
    return {"title": title, "detail": f"{title} is visible in the photo.", "system": system,
            "severity": severity, "urgency": urgency, "trade": trade,
            "recommendation": f"Have a {trade.lower()} look at the {title.lower()}.",
            "confidence": confidence, "box_2d": box}


PHOTO_PLAN: Dict[str, dict] = {
    "owner_photo_1": dict(space="living", space_label="Living room", is_exterior=False,
                          findings=[_finding("Hairline crack above door", "structure", "minor",
                                             "monitor", "Plasterer", [120, 400, 260, 470],
                                             "medium")]),
    "owner_photo_2": dict(space="kitchen", space_label="Kitchen", is_exterior=False,
                          findings=[_finding("Failed sealant behind sink", "plumbing", "minor",
                                             "within_6_months", "Plumber", [600, 300, 700, 520])]),
    "owner_photo_3": dict(space="bathroom", space_label="Bathroom", is_exterior=False,
                          findings=[_finding("Black mould at ceiling corner", "Damp", "Major",
                                             "within 6 months", "Damp specialist",
                                             [0.05, 0.7, 0.2, 0.95]),
                                    _finding("Exposed wiring at light fitting", "electrical",
                                             "critical", "immediate", "Electrician",
                                             [80, 450, 160, 540])]),
    "owner_photo_4": dict(space="exterior_front", space_label="Front elevation", is_exterior=True,
                          findings=[_finding("Cracked render below window", "exterior", "major",
                                             "within_6_months", "Renderer", [420, 560, 600, 700]),
                                    _finding("Blocked gutter", "roof", "minor", "within_6_months",
                                             "Roofer", [40, 100, 90, 600])]),
    "owner_photo_5": dict(space="utility", space_label="Boiler cupboard", is_exterior=False,
                          findings=[],
                          equipment=[{"kind": "Gas combi boiler", "brand": "Worcester",
                                      "model": "Greenstar 30i", "serial": "WB123456",
                                      "manufacture_year": 2009, "age_basis": "printed on label",
                                      "label_text": "30 kW, natural gas",
                                      "condition": "Casing intact, some discolouration.",
                                      "box_2d": [200, 300, 800, 700]}]),
    "owner_photo_6": dict(space="bedroom", space_label="Bedroom", is_exterior=False, findings=[]),
    "satellite_topdown": dict(space="aerial", space_label="Aerial view", is_exterior=True,
                              findings=[_finding("Moss on north roof slope", "roof", "minor",
                                                 "within_2_years", "Roofer", [300, 300, 450, 600],
                                                 "medium")]),
    "street_facade_front": dict(space="exterior_front", space_label="Front elevation",
                                is_exterior=True,
                                findings=[_finding("Render crack under front window", "exterior",
                                                   "major", "within_6_months", "Renderer",
                                                   [400, 540, 620, 720])]),
}


def photo_inspection(label: str) -> dict:
    plan = PHOTO_PLAN.get(label) or dict(space="other", space_label="Room", is_exterior=False,
                                         findings=[])
    systems = sorted({f["system"] for f in plan["findings"]} |
                     ({"exterior", "windows_doors"} if plan["is_exterior"] else {"interior"}))
    return {
        "space": plan["space"], "space_label": plan["space_label"],
        "is_exterior": plan["is_exterior"],
        "caption": f"{plan['space_label']} with natural light",
        "description": f"A photo of the {plan['space_label'].lower()}.",
        "systems_visible": systems,
        "features": ["Large window"] if not plan["is_exterior"] else ["Bay window"],
        "materials": ["Painted plaster"],
        "findings": plan["findings"],
        "equipment": plan.get("equipment", []),
        "photo_quality_1_to_10": 7, "photo_issues": [], "retake_tip": "",
        "listing_appeal_1_to_10": 8 if plan["space"] in ("living", "exterior_front") else 5,
        "makeover_potential_1_to_10": 9 if plan["space"] in ("living", "bedroom", "kitchen") else 2,
    }


def synthesis(text: str) -> dict:
    ids = re.findall(r'"id": "([A-Z0-9]+-\d+)"', text)
    labels = re.findall(r'"label": "([a-z_0-9]+)"', text)
    issues = []
    crack = [i for i in ids if i in ("P04-1", "SVF-1")]
    if crack:
        issues.append({"title": "Cracked render on front elevation", "system": "exterior",
                       "severity": "major", "urgency": "within_6_months", "trade": "Renderer",
                       "summary": "One crack, seen from the street and in the owner's photo.",
                       "recommendation": "Rake out and re-render.", "evidence": crack})
    # One bogus ID to prove merge() ignores what does not exist.
    issues.append({"title": "Phantom", "system": "roof", "severity": "minor",
                   "urgency": "monitor", "trade": "Roofer", "summary": "", "recommendation": "",
                   "evidence": ["P99-9"]})
    spaces = [{"name": "Front elevation", "kind": "exterior_front",
               "photos": [l for l in labels if l in ("owner_photo_4", "street_facade_front")],
               "summary": "Rendered front.", "condition": "Fair"}]
    return {
        "verdict": "Sound overall, with a live electrical hazard and damp to deal with first.",
        "executive_summary": "Mostly well kept. Exposed wiring in the bathroom needs an "
                             "electrician now; mould and a render crack need attention.",
        "issues": issues, "spaces": spaces,
        "strengths": ["Kitchen worktops in good order"],
        "questions_to_ask": ["When was the boiler last serviced?"],
        "coverage_gaps": [{"area": "Loft", "why_it_matters": "Roof timbers and insulation.",
                           "shot": "A wide shot from the loft hatch with a torch."}],
        "dismissed": [],
    }


def property_report() -> dict:
    return {
        "address": "1 Test Street", "imagery_confidence": "High",
        "architectural_profile": {"primary_style": "Victorian terrace", "estimated_stories": 2,
                                  "exterior_siding_materials": ["Render"],
                                  "garage_capacity_estimated_cars": 0,
                                  "curb_appeal_score_1_to_10": 6,
                                  "overall_property_grade": "B (Well Maintained)"},
        "roof_inspection": {"shape": "Gable", "material": "Slate", "condition_score_1_to_10": 7,
                            "visible_wear_or_damage": ["Moss"], "solar_panels_detected": False,
                            "estimated_solar_coverage_pct": 0},
        "yard_and_parcel": {"swimming_pool_detected": False, "pool_type": None,
                            "tree_canopy_density": "Sparse", "driveway_type": "None",
                            "accessory_structures": []},
        "key_selling_points": ["Bay window"],
        "risk_factors_or_deferred_maintenance": ["Render cracking"],
        "best_exterior_view": "owner_photo_4",
    }


def concepts() -> dict:
    variant = lambda tier, name: {  # noqa: E731
        "tier": tier, "concept_name": name, "design_narrative": "A fresh front.",
        "target_buyer": "Young families", "image_edit_prompt": "Repaint the render white.",
        "scope_items": [{"scope_item": "Repair render", "visual_evidence": "owner_photo_4 crack",
                         "effort": "Moderate"}],
        "overall_effort": "Moderate", "grounded_in_images": ["owner_photo_4"],
        "visual_impact": "Clean facade.", "highest_leverage_move": "Render repair",
        "key_risk": "Hidden damp."}
    return {"variants": [variant("Cosmetic Refresh", "Chalk"), variant("Signature", "Slate"),
                         variant("Full", "Glass")],
            "recommended_concept_name": "Slate", "recommendation_rationale": "Fixes the crack."}


def listing(text: str) -> dict:
    labels = re.findall(r'"label": "(owner_photo_\d+)"', text)
    return {"headline": "Light-filled Victorian terrace", "description": "A lovely home. " * 20,
            "highlights": ["Bay window", "Natural light"],
            "photo_order": list(reversed(labels)) + ["not_a_photo"],
            "retake": [{"photo": labels[0], "note": "Shoot from the doorway."}] if labels else [],
            "captions": [{"photo": l, "note": f"Caption for {l}"} for l in labels],
            "pre_listing_fixes": ["Re-seal the sink"], "social_caption": "Just listed!"}


def ask(text: str) -> dict:
    return {"answer": "Yes - there is black mould in the bathroom ceiling corner.",
            "confidence": "high",
            "citations": [{"photo": "owner_photo_3", "note": "Mould in the corner",
                           "box_2d": [50, 700, 200, 950]},
                          {"photo": "nonexistent", "note": "x", "box_2d": []}],
            "follow_ups": ["Is it spreading?"]}


def temporal() -> dict:
    return {"timeline": [{"capture_date": "2015-06", "observed_state": "Painted render",
                          "changes_since_previous": []},
                         {"capture_date": "2021-06", "observed_state": "Render cracked",
                          "changes_since_previous": ["Crack appeared"]}],
            "roof_replaced_or_repaired": False, "new_structures_detected": [],
            "possible_unpermitted_work": [], "vegetation_trend": "Stable",
            "maintenance_trajectory": "Stable", "upkeep_signal": "Held steady."}


class FakeModels:
    def __init__(self, fail_once: tuple = ()):
        self.calls: List[str] = []
        self._fail = set(fail_once)

    def generate_content(self, model=None, contents=None, config=None):
        contents = list(contents or [])
        schema = getattr(config, "response_schema", None) if config is not None else None
        if schema is None:
            self.calls.append("image")
            src = next((c for c in contents if isinstance(c, Image.Image)), None)
            out = (src or Image.new("RGB", (400, 300))).copy()
            out = Image.blend(out, Image.new("RGB", out.size, (40, 120, 200)), 0.35)
            buf = io.BytesIO()
            out.save(buf, "PNG")
            part = SimpleNamespace(inline_data=SimpleNamespace(data=buf.getvalue()))
            return SimpleNamespace(text=None, candidates=[
                SimpleNamespace(content=SimpleNamespace(parts=[part]))])

        name = schema.__name__
        self.calls.append(name)
        text = _all_text(contents)
        label = _label(contents)
        key = f"{name}:{label}"
        if key in self._fail:
            self._fail.discard(key)
            raise errors.ServerError(503, {"error": {"code": 503, "message": "overloaded",
                                                     "status": "UNAVAILABLE"}})
        body = {
            "PhotoInspection": lambda: photo_inspection(label),
            "InspectionSynthesis": lambda: synthesis(text),
            "PropertyInspectionReport": property_report,
            "RenovationConcepts": concepts,
            "ListingKit": lambda: listing(text),
            "AskAnswer": lambda: ask(text),
            "TemporalAnalysis": temporal,
        }[name]()
        return SimpleNamespace(text=json.dumps(body), candidates=[])


class FakeClient:
    def __init__(self, **kw):
        self.models = FakeModels(**kw)


SOLAR = {
    "imagery_date": "2023-05-01", "imagery_quality": "HIGH",
    "center": {"lat": 51.50010, "lng": -0.12010}, "distance_m": 12.0, "likely_neighbour": False,
    "roof_area_m2": 96.4, "roof_ground_area_m2": 80.1, "max_panels": 18,
    "max_array_area_m2": 35.2, "max_sunshine_hours": 1180, "carbon_offset_kg_per_mwh": 233.0,
    "panel": {"capacity_w": 400, "height_m": 1.879, "width_m": 1.045, "lifetime_years": 20},
    "segments": [
        {"pitch": 32.0, "azimuth": 182.0, "area_m2": 48.0, "ground_area_m2": 40.0,
         "sunshine_median": 1100.0, "center": {"lat": 51.50005, "lng": -0.12010},
         "sw": {"lat": 51.49999, "lng": -0.12020}, "ne": {"lat": 51.50008, "lng": -0.12000},
         "height_m": 7.5},
        {"pitch": 32.0, "azimuth": 2.0, "area_m2": 48.0, "ground_area_m2": 40.0,
         "sunshine_median": 700.0, "center": {"lat": 51.50014, "lng": -0.12010},
         "sw": {"lat": 51.50009, "lng": -0.12020}, "ne": {"lat": 51.50018, "lng": -0.12000},
         "height_m": 7.5},
    ],
    "configs": [[4, 1500], [8, 2900], [12, 4200], [18, 5600]],
    "panels": [[51.50003 + i * 0.00001, -0.12015 + (i % 3) * 0.00002, i % 2, 0, 330.0 - i]
               for i in range(18)],
}


def patch_network(monkeypatch, *, geocode_ok: bool = True, google: bool = True):
    """Replace every Google Maps touchpoint in the pipeline module."""
    from scanner import pipeline

    def geocode(address, key):
        if not geocode_ok:
            raise RuntimeError(f"Could not find '{address}' on the map.")
        return 51.5001, -0.1201

    def meta(address, key):
        return {"location": {"lat": 51.5000, "lng": -0.1203}, "pano_id": "pano",
                "date": "2021-06", "_source": "outdoor"} if google else None

    def perspectives(address, key, heading, meta_, center=None):
        if not google:
            return {}
        return {"satellite_topdown": make_photo(10, (640, 640)),
                "street_facade_front": make_photo(11, (640, 640)),
                "street_right_flank": make_photo(12, (640, 640))}

    monkeypatch.setattr(pipeline, "geocode_address", geocode)
    monkeypatch.setattr(pipeline, "get_streetview_metadata", meta)
    monkeypatch.setattr(pipeline, "download_all_property_perspectives", perspectives)
    monkeypatch.setattr(pipeline, "fetch_building_insights",
                        lambda lat, lng, key: SOLAR if google else None)
    monkeypatch.setattr(pipeline, "find_historical_panos",
                        lambda lat, lng, limit: [{"pano_id": "a", "date": "2015-06", "lat": lat,
                                                  "lon": lng},
                                                 {"pano_id": "b", "date": "2021-06", "lat": lat,
                                                  "lon": lng}] if google else [])
    monkeypatch.setattr(pipeline, "download_timeline_images",
                        lambda panos, lat, lng, key: [(p["date"], make_photo(20 + i, (640, 640)))
                                                      for i, p in enumerate(panos)])
