"""
Roof geometry and solar potential from the Google Solar API.

This is the one part of the report that is measured rather than read from a
photograph: Google derives it from its own aerial imagery and digital surface
model. It gives the roof's planes (pitch, orientation, area), how much sun
each gets, and a ranked layout of where panels would go — the same numbers
roofing and solar firms otherwise buy as a measurement report.

Same key as the rest of the Maps calls, but the "Solar API" has to be enabled
on its Google Cloud project. If it isn't, or the address is outside Google's
coverage, this returns None and the report simply has no solar section.
"""

import logging
import math
from typing import Any, Dict, List, Optional

import requests

log = logging.getLogger(__name__)

ENDPOINT = "https://solar.googleapis.com/v1/buildingInsights:findClosest"

# Enough to draw a sensible layout on a single satellite plate; a warehouse
# roof can return thousands, and the payload goes to the browser.
MAX_PANELS = 400

# findClosest returns the nearest building it knows. Past this distance from
# the geocoded point it is probably a neighbour, and saying so beats
# confidently describing someone else's roof.
MAX_DISTANCE_M = 60


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _latlng(obj: Optional[dict]) -> Optional[Dict[str, float]]:
    if not obj or "latitude" not in obj or "longitude" not in obj:
        return None
    return {"lat": float(obj["latitude"]), "lng": float(obj["longitude"])}


def _date(obj: Optional[dict]) -> Optional[str]:
    if not obj or not obj.get("year"):
        return None
    return f"{obj['year']:04d}-{obj.get('month', 1):02d}-{obj.get('day', 1):02d}"


def _median(quantiles: Optional[List[float]]) -> Optional[float]:
    if not quantiles:
        return None
    return round(float(quantiles[len(quantiles) // 2]), 1)


def compact(data: dict, lat: float, lng: float) -> Optional[dict]:
    """Reduce a buildingInsights response to what the report draws and states."""
    potential = data.get("solarPotential") or {}
    center = _latlng(data.get("center"))
    if not potential or not center:
        return None

    distance = _haversine_m(lat, lng, center["lat"], center["lng"])
    whole = potential.get("wholeRoofStats") or {}

    segments = []
    for seg in potential.get("roofSegmentStats") or []:
        stats = seg.get("stats") or {}
        box = seg.get("boundingBox") or {}
        segments.append({
            "pitch": round(float(seg.get("pitchDegrees") or 0), 1),
            "azimuth": round(float(seg.get("azimuthDegrees") or 0), 1),
            "area_m2": round(float(stats.get("areaMeters2") or 0), 1),
            "ground_area_m2": round(float(stats.get("groundAreaMeters2") or 0), 1),
            "sunshine_median": _median(stats.get("sunshineQuantiles")),
            "center": _latlng(seg.get("center")),
            "sw": _latlng(box.get("sw")),
            "ne": _latlng(box.get("ne")),
            "height_m": round(float(seg.get("planeHeightAtCenterMeters") or 0), 1),
        })

    configs = [
        [int(c.get("panelsCount") or 0), round(float(c.get("yearlyEnergyDcKwh") or 0))]
        for c in potential.get("solarPanelConfigs") or []
        if c.get("panelsCount")
    ]

    panels = []
    for p in (potential.get("solarPanels") or [])[:MAX_PANELS]:
        c = _latlng(p.get("center"))
        if not c:
            continue
        panels.append([
            round(c["lat"], 7), round(c["lng"], 7),
            1 if p.get("orientation") == "PORTRAIT" else 0,
            int(p.get("segmentIndex") or 0),
            round(float(p.get("yearlyEnergyDcKwh") or 0), 1),
        ])

    return {
        "imagery_date": _date(data.get("imageryDate")),
        "imagery_quality": data.get("imageryQuality"),
        "center": center,
        "distance_m": round(distance, 1),
        "likely_neighbour": distance > MAX_DISTANCE_M,
        "roof_area_m2": round(float(whole.get("areaMeters2") or 0), 1),
        "roof_ground_area_m2": round(float(whole.get("groundAreaMeters2") or 0), 1),
        "max_panels": int(potential.get("maxArrayPanelsCount") or 0),
        "max_array_area_m2": round(float(potential.get("maxArrayAreaMeters2") or 0), 1),
        "max_sunshine_hours": round(float(potential.get("maxSunshineHoursPerYear") or 0)),
        "carbon_offset_kg_per_mwh": round(float(potential.get("carbonOffsetFactorKgPerMwh") or 0), 1),
        "panel": {
            "capacity_w": round(float(potential.get("panelCapacityWatts") or 0)),
            "height_m": float(potential.get("panelHeightMeters") or 0),
            "width_m": float(potential.get("panelWidthMeters") or 0),
            "lifetime_years": int(potential.get("panelLifetimeYears") or 0),
        },
        "segments": segments,
        "configs": configs,
        "panels": panels,
    }


def fetch_building_insights(lat: float, lng: float, api_key: str) -> Optional[dict]:
    """The compacted insights for the building at (lat, lng), or None."""
    try:
        resp = requests.get(ENDPOINT, params={
            "location.latitude": f"{lat:.7f}",
            "location.longitude": f"{lng:.7f}",
            # Without this the API only answers where it has HIGH quality
            # imagery; LOW still measures the roof, just from older data.
            "requiredQuality": "LOW",
            "key": api_key,
        }, timeout=25)
    except Exception as exc:  # noqa: BLE001
        log.warning("Solar API unreachable: %s", exc)
        return None

    if resp.status_code == 403:
        log.warning("Solar API refused the key (403). Enable 'Solar API' on the Maps "
                    "key's Google Cloud project to add roof geometry to reports.")
        return None
    if resp.status_code == 404:
        log.info("Solar API has no building at %.6f, %.6f", lat, lng)
        return None
    if resp.status_code != 200:
        log.warning("Solar API error %s: %s", resp.status_code, resp.text[:200])
        return None

    try:
        return compact(resp.json(), lat, lng)
    except Exception as exc:  # noqa: BLE001 - a malformed reply loses a section, not a scan
        log.warning("Could not read Solar API response: %s", exc)
        return None


# ── Projection ───────────────────────────────────────────────────────────────
# The satellite plate is a Static Maps image centred on the geocoded point, so
# a lat/lng maps to a pixel by plain Web Mercator arithmetic. Used to draw the
# roof planes and panels onto the plate for the PDF; the browser does the same
# sums for its live overlay.
def project(lat: float, lng: float, center_lat: float, center_lng: float,
            zoom: int, size: int) -> tuple:
    def world(la: float, ln: float) -> tuple:
        siny = min(max(math.sin(math.radians(la)), -0.9999), 0.9999)
        scale = 256 * (2 ** zoom)
        return (scale * (0.5 + ln / 360),
                scale * (0.5 - math.log((1 + siny) / (1 - siny)) / (4 * math.pi)))
    cx, cy = world(center_lat, center_lng)
    px, py = world(lat, lng)
    return (size / 2 + (px - cx), size / 2 + (py - cy))


def meters_per_pixel(lat: float, zoom: int) -> float:
    return 156543.03392 * math.cos(math.radians(lat)) / (2 ** zoom)


def panel_polygon(panel: list, solar: dict, center_lat: float, center_lng: float,
                  zoom: int, size: int) -> List[tuple]:
    """Corner pixels of one panel, rotated to its roof plane's azimuth."""
    lat, lng, portrait, seg_index, _ = panel
    segs = solar.get("segments") or []
    azimuth = segs[seg_index]["azimuth"] if 0 <= seg_index < len(segs) else 180.0
    w = (solar["panel"]["width_m"] or 1.0) / 2
    h = (solar["panel"]["height_m"] or 1.7) / 2
    cx, cy = project(lat, lng, center_lat, center_lng, zoom, size)
    mpp = meters_per_pixel(center_lat, zoom)
    orientation = 90 if portrait else 0
    pts = []
    for x, y in ((w, h), (w, -h), (-w, -h), (-w, h)):
        dist = math.hypot(x, y) / mpp
        heading = math.radians(math.degrees(math.atan2(y, x)) + orientation + azimuth)
        # Heading is clockwise from north; screen y grows downward.
        pts.append((cx + dist * math.sin(heading), cy - dist * math.cos(heading)))
    return pts
