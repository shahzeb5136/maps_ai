"""Satellite, Street View and historical panorama fetching."""

import concurrent.futures
import io
import logging
from typing import Dict, List, Optional, Tuple

import requests
from PIL import Image

from .geo import compass_bearing

log = logging.getLogger(__name__)

try:
    from streetview import search_panoramas
    HAS_PANO_SEARCH = True
except ImportError:  # temporal module degrades to a no-op without it
    HAS_PANO_SEARCH = False


def fetch_satellite_image(address: str, api_key: str, zoom: int = 20,
                          size: str = "640x640") -> Image.Image:
    resp = requests.get(
        "https://maps.googleapis.com/maps/api/staticmap",
        params={"center": address, "zoom": zoom, "size": size,
                "maptype": "satellite", "key": api_key},
        timeout=30,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Satellite API error ({resp.status_code}): {resp.text[:200]}")
    return Image.open(io.BytesIO(resp.content)).convert("RGB")


def fetch_street_view(api_key: str, heading: int, location: Optional[str] = None,
                      pano_id: Optional[str] = None, pitch: int = 5, fov: int = 90,
                      size: str = "640x640", allow_any_source: bool = False) -> Image.Image:
    """Fetch by address OR by a specific panorama id (this is how we time-travel)."""
    params = {
        "size": size, "heading": heading, "pitch": pitch, "fov": fov,
        "return_error_code": "true", "key": api_key,
    }
    if pano_id:
        params["pano"] = pano_id
    else:
        params["location"] = location
        if not allow_any_source:
            params["source"] = "outdoor"

    resp = requests.get("https://maps.googleapis.com/maps/api/streetview",
                        params=params, timeout=30)
    if resp.status_code != 200:
        raise RuntimeError(
            f"Street View error ({resp.status_code}) heading={heading} pano={pano_id}"
        )
    return Image.open(io.BytesIO(resp.content)).convert("RGB")


def download_all_property_perspectives(address: str, api_key: str,
                                       facade_heading: Optional[int],
                                       meta: Optional[dict]) -> Dict[str, Image.Image]:
    """Satellite + 4 street angles orbiting the facade heading."""
    if not meta:
        log.warning("No Street View coverage for '%s'. Satellite only.", address)

    images: Dict[str, Image.Image] = {}
    tasks = {"satellite_topdown": lambda: fetch_satellite_image(address, api_key)}

    if meta:
        pano_id = meta.get("pano_id")
        base = facade_heading if facade_heading is not None else 0
        angles = {
            "street_facade_front": base,
            "street_right_flank": (base + 90) % 360,
            "street_rear_opposite": (base + 180) % 360,
            "street_left_flank": (base + 270) % 360,
        }

        def _grab(heading: int) -> Image.Image:
            if pano_id:
                try:
                    return fetch_street_view(api_key, heading=heading, pano_id=pano_id)
                except Exception as exc:
                    log.info("Pano fetch failed (%s); retrying by location", exc)
            return fetch_street_view(api_key, heading=heading, location=address,
                                     allow_any_source=True)

        for name, hd in angles.items():
            tasks[name] = (lambda h=hd: _grab(h))

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        futures = {ex.submit(fn): key for key, fn in tasks.items()}
        for fut in concurrent.futures.as_completed(futures):
            key = futures[fut]
            try:
                images[key] = fut.result()
            except Exception as exc:
                log.warning("Failed to fetch %s: %s", key, exc)

    if not has_street_imagery(images):
        log.warning("No street-level imagery obtained - facade assessment will be unreliable.")

    return images


def has_street_imagery(images: Dict[str, Image.Image]) -> bool:
    return any(k.startswith("street") for k in images)


def pick_facade(images: Dict[str, Image.Image]) -> Optional[Image.Image]:
    """The front elevation if we got it, else any street-level frame."""
    return images.get("street_facade_front") or next(
        (v for k, v in images.items() if k.startswith("street")), None
    )


# ── Historical panoramas ─────────────────────────────────────────────────────
def find_historical_panos(lat: float, lng: float, limit: int) -> List[dict]:
    if not HAS_PANO_SEARCH:
        log.warning("`streetview` package missing - skipping temporal module.")
        return []

    try:
        panos = search_panoramas(lat=lat, lon=lng)
    except Exception as exc:
        log.warning("Pano search failed: %s", exc)
        return []

    dated = [p for p in panos if getattr(p, "date", None)]
    log.info("Raw panos: %d (%d dated)", len(panos), len(dated))
    if not dated:
        return []

    dated.sort(key=lambda p: p.date)

    by_year: Dict[str, object] = {}
    for p in dated:
        by_year[p.date[:4]] = p           # keep the latest capture per year
    ordered = sorted(by_year.values(), key=lambda p: p.date)

    if len(ordered) <= limit:
        picks = ordered
    else:
        step = (len(ordered) - 1) / (limit - 1)
        picks = [ordered[i] for i in sorted({round(i * step) for i in range(limit)})]

    return [{"pano_id": p.pano_id, "date": p.date, "lat": p.lat, "lon": p.lon} for p in picks]


def download_timeline_images(panos: List[dict], target_lat: float, target_lng: float,
                             api_key: str) -> List[Tuple[str, Image.Image]]:
    results: List[Tuple[str, Image.Image]] = []

    def _grab(p):
        heading = compass_bearing(p["lat"], p["lon"], target_lat, target_lng)
        return p["date"], fetch_street_view(api_key, heading=heading,
                                            pano_id=p["pano_id"], fov=80)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        for fut in concurrent.futures.as_completed([ex.submit(_grab, p) for p in panos]):
            try:
                results.append(fut.result())
            except Exception as exc:
                log.warning("Failed to fetch timeline frame: %s", exc)

    results.sort(key=lambda t: t[0])
    return results
