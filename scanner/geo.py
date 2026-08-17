"""Geocoding, bearings and Street View metadata lookups."""

import logging
import math
import re
from typing import Optional, Tuple

import requests

log = logging.getLogger(__name__)


def sanitize_folder_name(address: str) -> str:
    clean = re.sub(r"[^\w\s-]", "", address).strip().lower()
    return re.sub(r"[-\s]+", "_", clean) or "property"


def geocode_address(address: str, api_key: str) -> Tuple[float, float]:
    """Google Geocoding, falling back to OSM Nominatim if Google says no."""
    try:
        data = requests.get(
            "https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": address, "key": api_key}, timeout=20,
        ).json()
        if data.get("status") == "OK":
            loc = data["results"][0]["geometry"]["location"]
            return loc["lat"], loc["lng"]
        log.warning("Google geocode returned %s - falling back to OSM", data.get("status"))
    except Exception as exc:
        log.warning("Google geocode failed (%s) - falling back to OSM", exc)

    resp = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": address, "format": "json", "limit": 1},
        headers={"User-Agent": "nexgen-property-scanner/1.0"},
        timeout=20,
    )
    results = resp.json()
    if not results:
        raise RuntimeError(
            f"Could not find '{address}' on the map. Try a more specific address."
        )
    log.info("Using OSM coordinates - less precise than Google's")
    return float(results[0]["lat"]), float(results[0]["lon"])


def compass_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Bearing in degrees from point 1 -> point 2. Aims the camera AT the building."""
    d_lon = math.radians(lon2 - lon1)
    y = math.sin(d_lon) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(d_lon))
    return int((math.degrees(math.atan2(y, x)) + 360) % 360)


def get_streetview_metadata(address: str, api_key: str) -> Optional[dict]:
    """Prefer Google-car imagery; fall back to any pano (incl. user photospheres)."""
    for source in ("outdoor", None):
        params = {"location": address, "key": api_key}
        if source:
            params["source"] = source
        try:
            data = requests.get(
                "https://maps.googleapis.com/maps/api/streetview/metadata",
                params=params, timeout=20,
            ).json()
        except Exception as exc:
            log.warning("Street View metadata lookup failed: %s", exc)
            return None
        if data.get("status") == "OK":
            data["_source"] = source or "any"
            return data
    return None
