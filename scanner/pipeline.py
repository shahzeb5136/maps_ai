"""
The scan itself.

`run_scan()` is synchronous and CPU-light but network-heavy (roughly 1-3
minutes of Google Maps and Gemini calls). The API layer runs it on a worker
thread; nothing in here touches the database or knows about credits.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from PIL import Image

from . import analysis, config
from .geo import compass_bearing, geocode_address, get_streetview_metadata
from .imagery import (
    OWNER_PREFIX,
    download_all_property_perspectives,
    download_timeline_images,
    find_historical_panos,
    pick_facade,
)
from .report_pdf import build_pdf

log = logging.getLogger(__name__)

# (stage, human-readable detail) — surfaced to the user while the job runs.
ProgressFn = Callable[[str, str], None]


class ScanFailed(Exception):
    """A scan that cannot produce a report. The caller refunds the credit."""


def _noop(stage: str, detail: str) -> None:
    log.info("[%s] %s", stage, detail)


def run_scan(address: str, out_dir: Path,
             on_progress: Optional[ProgressFn] = None,
             owner_photos: Optional[List[str]] = None) -> dict:
    """
    Scan `address`, write every artifact into `out_dir`, and return the payload
    describing them. Filenames in the payload are relative to `out_dir`; the API
    turns them into signed URLs.

    `owner_photos` names files the API has already validated, re-encoded and
    written into `out_dir`. They join the Google imagery for every stage that
    looks at pictures.
    """
    progress = on_progress or _noop
    config.require_keys()

    out_dir.mkdir(parents=True, exist_ok=True)
    maps_key = config.MAPS_API_KEY
    client = analysis.make_client(config.GEMINI_API_KEY)

    # ── Locate ───────────────────────────────────────────────────────────────
    progress("locating", f"Locating {address}")
    try:
        lat, lng = geocode_address(address, maps_key)
    except Exception as exc:
        raise ScanFailed(str(exc)) from exc
    log.info("Target %.6f, %.6f", lat, lng)

    meta = get_streetview_metadata(address, maps_key)
    facade_heading = None
    if meta:
        cam = meta["location"]
        facade_heading = compass_bearing(cam["lat"], cam["lng"], lat, lng)
        log.info("Pano %s | %s | aim %s deg", meta.get("pano_id"), meta.get("date"),
                 facade_heading)

    # ── Imagery ──────────────────────────────────────────────────────────────
    progress("imagery", "Fetching satellite and street-level imagery")
    images = download_all_property_perspectives(address, maps_key, facade_heading, meta)

    # Owner photos go in after the Google set, so they read last in the report
    # and never displace a satellite or Street View plate.
    images.update(_load_owner_photos(out_dir, owner_photos or []))

    if not images:
        raise ScanFailed(
            "No imagery is available for this location. Google Maps returned nothing "
            "for either the satellite or street view, and no photos were attached."
        )
    log.info("Got %d perspectives (%d owner-supplied)",
             len(images), sum(1 for k in images if k.startswith(OWNER_PREFIX)))

    # ── Condition assessment ─────────────────────────────────────────────────
    progress("inspection", "Running the condition assessment")
    try:
        report = analysis.analyze_property(address, images, client)
    except Exception as exc:
        raise ScanFailed(f"Condition assessment failed: {exc}") from exc
    log.info("Grade %s (confidence %s)",
             report.architectural_profile.overall_property_grade, report.imagery_confidence)

    # ── Temporal ─────────────────────────────────────────────────────────────
    timeline_imgs: List[Tuple[str, Image.Image]] = []
    temporal = None
    if config.ENABLE_TEMPORAL:
        progress("temporal", "Searching historical Street View captures")
        s_lat, s_lng = (meta["location"]["lat"], meta["location"]["lng"]) if meta else (lat, lng)
        panos = find_historical_panos(s_lat, s_lng, config.MAX_HISTORICAL_PANOS)
        if len(panos) >= 2:
            log.info("Using %d captures: %s", len(panos), [p["date"] for p in panos])
            timeline_imgs = download_timeline_images(panos, lat, lng, maps_key)
            if len(timeline_imgs) >= 2:
                progress("temporal", f"Diffing {len(timeline_imgs)} historical captures")
                try:
                    temporal = analysis.analyze_timeline(address, timeline_imgs, client)
                except Exception as exc:
                    # A non-fatal stage: the report is still worth delivering.
                    log.warning("Temporal analysis failed: %s", exc)
                    timeline_imgs = []
            else:
                log.info("Too few timeline frames downloaded, skipping analysis")
                timeline_imgs = []
        else:
            log.info("Not enough historical coverage, skipping temporal")

    # ── Renovation ───────────────────────────────────────────────────────────
    concepts = None
    before_img: Optional[Image.Image] = None
    rendered: List[Optional[Image.Image]] = []
    facade_label = ""
    if config.ENABLE_RENOVATION:
        # The inspection stage has seen every plate, so it nominates the one to
        # render from; pick_facade only falls back to its own ordering when that
        # nomination names an image we do not hold.
        facade, facade_label = pick_facade(images, report.best_exterior_view)
        if facade is not None:
            log.info("Rendering concepts from '%s'%s", facade_label,
                     " (owner-supplied)" if facade_label.startswith(OWNER_PREFIX) else "")
            progress("renovation", "Designing three renovation concepts from your imagery")
            try:
                concepts = analysis.build_renovation_concepts(
                    address, facade, report, client,
                    facade_label=facade_label, images=images,
                )
                progress("rendering", f"Rendering {len(concepts.variants)} concept images")
                rendered = analysis.render_all_variants(facade, concepts, client)
                log.info("%d/%d concepts rendered", sum(1 for r in rendered if r), len(rendered))
                before_img = facade if any(rendered) else None
            except Exception as exc:
                log.warning("Renovation stage failed: %s", exc)
                concepts, rendered, before_img = None, [], None
        else:
            log.info("No facade image, skipping renovation")

    # ── Persist ──────────────────────────────────────────────────────────────
    progress("saving", "Writing the report")
    payload = _save(out_dir, address, lat, lng, images, report,
                    timeline_imgs, temporal, concepts, before_img, rendered,
                    facade_label)

    progress("pdf", "Building the PDF")
    try:
        build_pdf(out_dir / "report.pdf", payload, out_dir)
        payload["pdf"] = "report.pdf"
    except Exception as exc:
        # The web report still works without it; don't fail a paid scan over a PDF.
        log.exception("PDF generation failed: %s", exc)
        payload["pdf"] = None

    (out_dir / "report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return payload


def _load_owner_photos(out_dir: Path, names: List[str]) -> Dict[str, Image.Image]:
    """
    Open the attachments the API already wrote into the scan directory.

    A photo that will not open is skipped rather than fatal: the API decoded and
    re-encoded every one of these on the way in, so a failure here means disk
    trouble, and losing one attachment is a far better outcome than losing the
    paid scan.
    """
    loaded: Dict[str, Image.Image] = {}
    for name in names:
        path = out_dir / name
        try:
            with Image.open(path) as img:
                loaded[Path(name).stem] = img.convert("RGB")
        except Exception as exc:
            log.warning("Could not load owner photo %s: %s", name, exc)
    if loaded:
        log.info("Loaded %d owner photo(s)", len(loaded))
    return loaded


def _save(out_dir: Path, address: str, lat: float, lng: float,
          images: Dict[str, Image.Image], report, timeline_imgs, temporal,
          concepts, before_img, rendered, facade_label: str = "") -> dict:
    """Write every image to disk and assemble the payload that references them."""
    imagery: Dict[str, str] = {}
    for name, img in images.items():
        if img is None:
            continue
        fname = f"{name}.jpg"
        img.save(out_dir / fname, "JPEG", quality=95)
        imagery[name] = fname

    timeline: List[dict] = []
    for date, img in timeline_imgs:
        fname = f"timeline_{date.replace('-', '_')}.jpg"
        img.save(out_dir / fname, "JPEG", quality=92)
        timeline.append({"date": date, "file": fname})

    renovation = None
    if concepts and before_img is not None and any(rendered):
        before_img.save(out_dir / "reno_before.jpg", "JPEG", quality=95)

        variants = []
        for i, variant in enumerate(concepts.variants):
            after = rendered[i] if i < len(rendered) else None
            after_file = None
            if after is not None:
                after_file = f"reno_after_{i + 1}.jpg"
                after.save(out_dir / after_file, "JPEG", quality=95)
            variants.append({**variant.model_dump(), "after": after_file})

        renovation = {
            "before": "reno_before.jpg",
            # Which supplied image the concepts were read from, so the report
            # can say whose photograph the before/after actually is.
            "before_source": facade_label,
            "before_is_owner_photo": facade_label.startswith(OWNER_PREFIX),
            "recommended_concept_name": concepts.recommended_concept_name,
            "recommendation_rationale": concepts.recommendation_rationale,
            "variants": variants,
        }

    return {
        "address": address,
        "scanned_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "coordinates": {"lat": lat, "lng": lng},
        "imagery": imagery,
        "timeline": timeline,
        "inspection": report.model_dump(),
        "temporal_analysis": temporal.model_dump() if temporal else None,
        "renovation": renovation,
        "pdf": None,
    }
