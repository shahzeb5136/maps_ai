"""
The owner's corrections to a finished inspection.

A photograph can show something real that is simply not a problem for this
owner — wires left dangling by the previous occupant that are about to go, a
stain they know is historic. The owner can mark such an issue "not an issue";
it then stops counting everywhere the inspection is summarised: the health
score, the action plan, the PDF. Nothing is deleted. The issue keeps its
evidence and is listed as dismissed, and restoring it puts everything back.

The score is recomputed with exactly the rules the scan used, from the issues
that remain and the systems the photos showed, so a dismissal can never make
a system look photographed when it was not.
"""

import logging
from pathlib import Path
from typing import List, Optional, Set

from PIL import Image

from . import annotate
from .scoring import health_score

log = logging.getLogger(__name__)


def active_issues(payload: dict) -> List[dict]:
    return [i for i in (payload.get("inspection_report") or {}).get("issues", [])
            if not i.get("dismissed_by_user")]


def dismissed_ids(payload: dict) -> List[str]:
    return [i["id"] for i in (payload.get("inspection_report") or {}).get("issues", [])
            if i.get("dismissed_by_user")]


def systems_seen(payload: dict) -> Set[str]:
    return {s for p in payload.get("photos") or [] if not p.get("error")
            for s in p.get("systems_visible") or []}


def set_dismissed(payload: dict, issue_id: str, dismissed: bool) -> bool:
    """
    Flag one issue and re-score. Returns False when the scan has no such
    issue (including scans made before issues existed). Mutates `payload`.
    """
    issues = (payload.get("inspection_report") or {}).get("issues")
    if not issues:
        return False
    issue = next((i for i in issues if i.get("id") == issue_id), None)
    if issue is None:
        return False
    issue["dismissed_by_user"] = bool(dismissed)
    payload["health"] = health_score(active_issues(payload), systems_seen(payload))
    payload["overrides_version"] = int(payload.get("overrides_version") or 0) + 1
    return True


def redraw_annotations(payload: dict, scan_dir: Path, labels: Set[str]) -> None:
    """
    Re-draw the PDF's annotated versions of `labels` (the photos of the issue
    that changed) from the issues still standing, so a dismissed issue's box
    no longer appears and a restored one comes back.
    """
    findings = {f["id"]: f for p in payload.get("photos") or [] for f in p.get("findings") or []}
    files = {p["label"]: p.get("file") for p in payload.get("photos") or []}
    annotated = payload.setdefault("annotated", {})
    active = active_issues(payload)

    for label in labels:
        marks = annotate.issue_marks(label, active, findings)
        name = files.get(label)
        if not marks:
            annotated.pop(label, None)
            continue
        if not name or not (scan_dir / name).exists():
            continue
        try:
            with Image.open(scan_dir / name) as img:
                out = annotate.annotated(img.convert("RGB"), marks)
            target = f"annot_{label}.jpg"
            out.save(scan_dir / target, "JPEG", quality=86)
            annotated[label] = target
        except Exception as exc:  # noqa: BLE001 - a stale picture beats a failed request
            log.warning("Could not redraw annotations for %s: %s", label, exc)


def rebuild_pdf(payload: dict, scan_dir: Path, labels: Set[str]) -> Optional[str]:
    """
    Build a fresh PDF for the corrected payload under a new, versioned name
    (a signed link to the old one may already be cached in a browser), and
    remove the superseded one. Returns the new filename, or None on failure,
    in which case the previous PDF stays in place.
    """
    from .report_pdf import build_pdf

    version = int(payload.get("overrides_version") or 0)
    name = f"report_r{version}.pdf"
    previous = payload.get("pdf")
    try:
        redraw_annotations(payload, scan_dir, labels)
        build_pdf(scan_dir / name, payload, scan_dir)
    except Exception as exc:  # noqa: BLE001
        log.exception("PDF rebuild failed: %s", exc)
        return None
    if previous and previous != name and previous.startswith("report_r"):
        (scan_dir / previous).unlink(missing_ok=True)
    return name
