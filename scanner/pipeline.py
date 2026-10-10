"""
The scan itself.

`run_scan()` is synchronous and CPU-light but network-heavy. The API layer runs
it on a worker thread; nothing in here touches the database or knows about
credits.

The shape of a scan, in phases that overlap wherever they can:

    locate       geocode, Street View metadata
    imagery      satellite + Street View plates  ‖  Solar API roof measurements
    photos       every photo inspected on its own, in parallel, boxes and all
                 ‖  historical Street View diffing (in the background)
    synthesis    exterior survey  ‖  merge findings into issues, group rooms
    concepts     exterior renovation concepts  ‖  interior makeovers  ‖  listing kit
    report       health score, annotated evidence, PDF

Only two things can fail a scan: finding nothing to look at, and having no
usable assessment at the end. Every other stage is an enrichment, and losing
one costs the report a section, never the user their credit.
"""

import concurrent.futures
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from PIL import Image

from . import analysis, annotate, config, makeovers, photos as photo_stage, synthesis
from .geo import compass_bearing, geocode_address, get_streetview_metadata
from .imagery import (
    OWNER_PREFIX,
    SATELLITE_SIZE,
    SATELLITE_ZOOM,
    download_all_property_perspectives,
    download_timeline_images,
    find_historical_panos,
    owner_photo_labels,
    pick_facade,
)
from .report_pdf import build_pdf
from .scoring import health_score
from .solar import fetch_building_insights

log = logging.getLogger(__name__)

# (stage, human-readable detail) — surfaced to the user while the job runs.
ProgressFn = Callable[[str, str], None]
# The live feed: a snapshot of every photo inspected so far.
LiveFn = Callable[[dict], None]

# How many owner photos the exterior survey and the exterior concepts look at.
# They want the elevations, not fifty interiors.
MAX_EXTERIOR_OWNER = 8


class ScanFailed(Exception):
    """A scan that cannot produce a report. The caller refunds the credit."""


@dataclass
class ScanOptions:
    goal: str = photo_stage.DEFAULT_GOAL
    style: str = makeovers.DEFAULT_STYLE
    on_live: Optional[LiveFn] = field(default=None, repr=False)


def _noop(stage: str, detail: str) -> None:
    log.info("[%s] %s", stage, detail)


class _LiveFeed:
    """Accumulates per-photo results into the snapshot the browser polls."""

    def __init__(self, sink: Optional[LiveFn], labels: List[str], progress: ProgressFn):
        self.sink = sink
        self.progress = progress
        self.state = {"v": 0, "total": len(labels), "done": 0, "order": labels, "photos": {}}

    def photo_done(self, result: dict, done: int, total: int) -> None:
        self.state["v"] += 1
        self.state["done"] = done
        self.state["photos"][result["label"]] = {
            "space": result["space_label"],
            "error": bool(result.get("error")),
            "findings": [{"title": f["title"], "severity": f["severity"], "box": f["box"]}
                         for f in result["findings"]],
            "equipment": len(result["equipment"]),
        }
        found = sum(len(p["findings"]) for p in self.state["photos"].values())
        self.progress("photos", f"Inspected {done} of {total} images · {found} findings so far")
        if self.sink:
            self.sink(json.loads(json.dumps(self.state)))


def run_scan(address: str, out_dir: Path,
             on_progress: Optional[ProgressFn] = None,
             owner_photos: Optional[List[str]] = None,
             options: Optional[ScanOptions] = None) -> dict:
    """
    Scan `address`, write every artifact into `out_dir`, and return the payload
    describing them. Filenames in the payload are relative to `out_dir`; the API
    turns them into signed URLs.

    `owner_photos` names files the API has already validated, re-encoded and
    written into `out_dir`.
    """
    progress = on_progress or _noop
    opts = options or ScanOptions()
    config.require_keys()

    out_dir.mkdir(parents=True, exist_ok=True)
    maps_key = config.MAPS_API_KEY
    client = analysis.make_client(config.GEMINI_API_KEY)

    owner = _load_owner_photos(out_dir, owner_photos or [])

    # ── Locate ───────────────────────────────────────────────────────────────
    progress("locating", f"Locating {address}")
    lat: Optional[float] = None
    lng: Optional[float] = None
    try:
        lat, lng = geocode_address(address, maps_key)
        log.info("Target %.6f, %.6f", lat, lng)
    except Exception as exc:
        # With photos to read, an address the map cannot find still makes a
        # report — a farmhouse down an unnamed track is exactly the property
        # Google has nothing on and the owner has everything on.
        if not owner:
            raise ScanFailed(str(exc)) from exc
        log.info("Could not geocode '%s' (%s); continuing from the photos alone", address, exc)

    meta = get_streetview_metadata(address, maps_key) if lat is not None else None
    facade_heading = None
    street_date = None
    if meta:
        cam = meta["location"]
        facade_heading = compass_bearing(cam["lat"], cam["lng"], lat, lng)
        street_date = meta.get("date")
        log.info("Pano %s | %s | aim %s deg", meta.get("pano_id"), street_date, facade_heading)

    # ── Imagery ‖ roof measurements ──────────────────────────────────────────
    images: Dict[str, Image.Image] = {}
    solar = None
    if lat is not None:
        progress("imagery", "Fetching satellite, Street View and roof measurements")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            f_imgs = ex.submit(download_all_property_perspectives, address, maps_key,
                               facade_heading, meta, (lat, lng))
            f_solar = (ex.submit(fetch_building_insights, lat, lng, maps_key)
                       if config.ENABLE_SOLAR else None)
            images = f_imgs.result()
            solar = f_solar.result() if f_solar else None
        # Written now rather than at the end, so the live feed can show the
        # plates while they are being inspected.
        for name, img in images.items():
            img.save(out_dir / f"{name}.jpg", "JPEG", quality=95)
            annotate.thumbnail(img).save(out_dir / f"thumb_{name}.jpg", "JPEG", quality=84)

    # Owner photos go in after the Google set, so they never displace a plate.
    images.update(owner)
    if not images:
        raise ScanFailed(
            "No imagery is available for this location. Google Maps returned nothing "
            "for either the satellite or street view, and no photos were attached."
        )
    log.info("Got %d images (%d owner-supplied)", len(images), len(owner))

    # ── Photos ‖ temporal ────────────────────────────────────────────────────
    owner_labels = owner_photo_labels(owner)
    inspect_list = [(label, owner[label]) for label in owner_labels]
    inspect_list += [(label, images[label]) for label in photo_stage.GOOGLE_INSPECTED
                     if label in images]

    feed = _LiveFeed(opts.on_live, [label for label, _ in inspect_list], progress)
    progress("photos", f"Inspecting {len(inspect_list)} images one by one")

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as background:
        temporal_job = (background.submit(_temporal, address, meta, lat, lng, maps_key, client)
                        if config.ENABLE_TEMPORAL and lat is not None else None)
        photos = photo_stage.inspect_all(client, inspect_list, address, opts.goal,
                                         street_date, on_done=feed.photo_done)
        progress("photos", "Finishing the historical comparison")
        timeline_imgs, temporal = temporal_job.result() if temporal_job else ([], None)

    usable = [p for p in photos if not p.get("error")]
    log.info("Inspected %d/%d images, %d findings", len(usable), len(photos),
             sum(len(p["findings"]) for p in photos))

    # ── Exterior survey ‖ synthesis ──────────────────────────────────────────
    progress("synthesis", f"Cross-checking {sum(len(p['findings']) for p in usable)} findings "
                          "and grouping rooms")
    by_label = {p["label"]: p for p in photos}
    exterior_owner = [label for label in owner_labels
                      if by_label.get(label, {}).get("is_exterior")]
    exterior_owner.sort(key=lambda l: -by_label[l]["listing_appeal"])
    exterior_owner = exterior_owner[:MAX_EXTERIOR_OWNER]
    survey_images = {k: v for k, v in images.items() if not k.startswith(OWNER_PREFIX)}
    survey_images.update({label: owner[label] for label in exterior_owner})

    report = None
    raw_synthesis = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        f_report = (ex.submit(analysis.analyze_property, address, survey_images, client)
                    if survey_images else None)
        f_synth = ex.submit(synthesis.synthesise, client, address, opts.goal, photos,
                            images, None)
        if f_report:
            try:
                report = f_report.result()
            except Exception as exc:  # noqa: BLE001
                log.warning("Exterior survey failed: %s", exc)
                if not usable:
                    raise ScanFailed(f"Condition assessment failed: {exc}") from exc
        try:
            raw_synthesis = f_synth.result()
        except Exception as exc:  # noqa: BLE001
            log.warning("Synthesis failed, falling back to per-photo issues: %s", exc)

    if report is None and not usable:
        raise ScanFailed("None of the images could be assessed. Try clearer photos, or an "
                         "address with satellite and Street View coverage.")

    inspection = synthesis.merge(photos, raw_synthesis)
    seen = {s for p in usable for s in p["systems_visible"]}
    health = health_score(inspection["issues"], seen)
    equipment = synthesis.collect_equipment(usable)
    if report:
        log.info("Exterior grade %s (confidence %s)",
                 report.architectural_profile.overall_property_grade, report.imagery_confidence)
    log.info("Health %s (%s), %d issues", health["score"], health["grade"],
             len(inspection["issues"]))

    # ── Concepts ‖ makeovers ‖ listing ───────────────────────────────────────
    progress("renovation", "Designing concepts, restyling rooms and writing the listing")
    concepts, before_img, rendered, facade_label = None, None, [], ""
    makeover_results: List[Tuple[dict, Optional[Image.Image]]] = []
    listing = None

    facade_images = {k: v for k, v in images.items()
                     if k.startswith("street") or k in exterior_owner}
    candidates = (makeovers.pick_candidates(photos, inspection["spaces"], config.MAKEOVER_COUNT)
                  if config.ENABLE_MAKEOVERS and config.MAKEOVER_COUNT else [])

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        f_concepts = (ex.submit(_concepts, address, report, facade_images, client)
                      if config.ENABLE_RENOVATION and report is not None else None)
        f_makeovers = (ex.submit(makeovers.render_makeovers, client, candidates, images,
                                 opts.style, inspection["spaces"]) if candidates else None)
        f_listing = (ex.submit(synthesis.listing_kit, client, address, photos, report,
                               inspection) if config.ENABLE_LISTING and owner else None)
        if f_concepts:
            concepts, before_img, rendered, facade_label = f_concepts.result()
        if f_makeovers:
            try:
                makeover_results = f_makeovers.result()
            except Exception as exc:  # noqa: BLE001
                log.warning("Makeovers failed: %s", exc)
        if f_listing:
            try:
                listing = f_listing.result()
            except Exception as exc:  # noqa: BLE001
                log.warning("Listing kit failed: %s", exc)

    # ── Persist ──────────────────────────────────────────────────────────────
    progress("saving", "Writing the report")
    payload = _save(out_dir, address, lat, lng, images, report,
                    timeline_imgs, temporal, concepts, before_img, rendered,
                    facade_label)
    payload.update(_save_v2(out_dir, images, photos, inspection, health, equipment,
                            listing, makeover_results, solar, lat, lng, street_date, opts))

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


# ── Stages that run off the main line ───────────────────────────────────────
def _temporal(address: str, meta: Optional[dict], lat: float, lng: float, maps_key: str,
              client) -> Tuple[List[Tuple[str, Image.Image]], object]:
    """Historical Street View diffing. Runs in the background; never raises."""
    try:
        s_lat, s_lng = ((meta["location"]["lat"], meta["location"]["lng"]) if meta
                        else (lat, lng))
        panos = find_historical_panos(s_lat, s_lng, config.MAX_HISTORICAL_PANOS)
        if len(panos) < 2:
            log.info("Not enough historical coverage, skipping temporal")
            return [], None
        log.info("Using %d captures: %s", len(panos), [p["date"] for p in panos])
        frames = download_timeline_images(panos, lat, lng, maps_key)
        if len(frames) < 2:
            log.info("Too few timeline frames downloaded, skipping analysis")
            return [], None
        return frames, analysis.analyze_timeline(address, frames, client)
    except Exception as exc:  # noqa: BLE001
        log.warning("Temporal analysis failed: %s", exc)
        return [], None


def _concepts(address: str, report, facade_images: Dict[str, Image.Image], client):
    """Exterior renovation concepts and their renders. Never raises."""
    # The survey stage has seen every plate, so it nominates the one to render
    # from; pick_facade only falls back to its own ordering when that
    # nomination names an image we do not hold. Only exterior photos are
    # offered: a kitchen is not a facade.
    facade, label = pick_facade(facade_images, report.best_exterior_view)
    if facade is None:
        log.info("No facade image, skipping renovation")
        return None, None, [], ""
    log.info("Rendering concepts from '%s'%s", label,
             " (owner-supplied)" if label.startswith(OWNER_PREFIX) else "")
    try:
        concepts = analysis.build_renovation_concepts(
            address, facade, report, client, facade_label=label, images=facade_images,
        )
        rendered = analysis.render_all_variants(facade, concepts, client)
        log.info("%d/%d concepts rendered", sum(1 for r in rendered if r), len(rendered))
        return concepts, (facade if any(rendered) else None), rendered, label
    except Exception as exc:  # noqa: BLE001
        log.warning("Renovation stage failed: %s", exc)
        return None, None, [], ""


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


def _save(out_dir: Path, address: str, lat: Optional[float], lng: Optional[float],
          images: Dict[str, Image.Image], report, timeline_imgs, temporal,
          concepts, before_img, rendered, facade_label: str = "") -> dict:
    """Write every image to disk and assemble the payload that references them."""
    imagery: Dict[str, str] = {}
    for name, img in images.items():
        if img is None:
            continue
        fname = f"{name}.jpg"
        # Owner photos were written by the API on upload and Google's plates as
        # soon as they arrived; re-encoding either here would only grow it.
        if not (out_dir / fname).exists():
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
        "coordinates": {"lat": lat, "lng": lng} if lat is not None else None,
        "imagery": imagery,
        "timeline": timeline,
        "inspection": report.model_dump() if report else None,
        "temporal_analysis": temporal.model_dump() if temporal else None,
        "renovation": renovation,
        "pdf": None,
    }


def _save_v2(out_dir: Path, images: Dict[str, Image.Image], photos: List[dict],
             inspection: dict, health: dict, equipment: List[dict],
             listing: Optional[dict], makeover_results, solar: Optional[dict],
             lat: Optional[float], lng: Optional[float], street_date: Optional[str],
             opts: ScanOptions) -> dict:
    """The inspection layer: per-photo results, issues, scores, renders, and the
    derived pictures the PDF draws from."""
    findings = {f["id"]: f for p in photos for f in p["findings"]}

    for p in photos:
        img = images.get(p["label"])
        p["file"] = f"{p['label']}.jpg"
        p["width"], p["height"] = img.size if img is not None else (0, 0)
        p["thumb"] = None
        if img is not None:
            thumb = f"thumb_{p['label']}.jpg"
            annotate.thumbnail(img).save(out_dir / thumb, "JPEG", quality=84)
            p["thumb"] = thumb

    # Pre-drawn evidence for the PDF: each photo with its issues boxed, and a
    # close crop for every issue in the register.
    annotated: Dict[str, str] = {}
    crops: Dict[str, str] = {}
    for p in photos:
        img = images.get(p["label"])
        marks = annotate.issue_marks(p["label"], inspection["issues"], findings)
        if img is not None and marks:
            name = f"annot_{p['label']}.jpg"
            annotate.annotated(img, marks).save(out_dir / name, "JPEG", quality=86)
            annotated[p["label"]] = name
    for issue in inspection["issues"]:
        lead = next((findings[fid] for fid in issue["evidence"] if findings[fid].get("box")),
                    None)
        img = images.get(lead["photo"]) if lead else None
        if img is not None:
            name = f"crop_{issue['id']}.jpg"
            annotate.crop(img, lead["box"], issue["severity"]).save(out_dir / name, "JPEG",
                                                                    quality=86)
            crops[issue["id"]] = name

    made = []
    for i, (meta, img) in enumerate(makeover_results, 1):
        if img is None:
            continue
        name = f"makeover_{i}.jpg"
        img.save(out_dir / name, "JPEG", quality=92)
        made.append({**meta, "after": name})

    site = None
    roof_plate = None
    if lat is not None:
        site = {"lat": lat, "lng": lng, "zoom": SATELLITE_ZOOM, "size": SATELLITE_SIZE}
        sat = images.get("satellite_topdown")
        if solar and sat is not None:
            try:
                drawn = annotate.roof_overlay(sat, solar, (lat, lng), SATELLITE_ZOOM,
                                              SATELLITE_SIZE)
                drawn.save(out_dir / "satellite_roof.jpg", "JPEG", quality=90)
                roof_plate = "satellite_roof.jpg"
            except Exception as exc:  # noqa: BLE001
                log.warning("Roof overlay failed: %s", exc)

    return {
        "version": 2,
        "goal": opts.goal,
        "style": opts.style,
        "style_name": makeovers.style_name(opts.style),
        "street_view_date": street_date,
        "photos": photos,
        "inspection_report": inspection,
        "health": health,
        "equipment": equipment,
        "listing": listing,
        "makeovers": made,
        "solar": solar,
        "site": site,
        # Used by the PDF only; the web draws its own.
        "annotated": annotated,
        "crops": crops,
        "satellite_roof": roof_plate,
    }
