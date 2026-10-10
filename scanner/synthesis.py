"""
From fifty inspected photographs to one inspection.

Per-photo findings overlap — the same crack shot from two corners, the same
stain from the doorway and from under it — and a register that lists it twice
double-counts it in the score. This stage looks at every photo at once (as
small thumbnails, which is plenty to tell rooms apart) alongside every
finding, and merges them into issues, groups the photos into distinct spaces,
and names what nobody photographed.

The model proposes; `merge()` disposes. Every evidence ID is checked against
the findings that exist, every photo is placed in exactly one space, and any
finding the model forgot to place becomes an issue of its own. A synthesis
call that fails outright degrades to one issue per finding and spaces grouped
by label — a plainer register, never a lost one.

Also builds the listing kit, from the same per-photo results.
"""

import json
import logging
from typing import Dict, List, Optional, Tuple

from google import genai
from google.genai import types
from PIL import Image

from . import gemini
from .models import InspectionSynthesis, ListingKit, PropertyInspectionReport
from .photos import GOALS, DEFAULT_GOAL, SPACES
from .scoring import (
    CONFIDENCES,
    SEVERITIES,
    SYSTEM_LABEL,
    URGENCIES,
    normalise_confidence,
    normalise_severity,
    normalise_system,
    normalise_urgency,
)

log = logging.getLogger(__name__)

THUMB_EDGE = 512


def _digest(photos: List[dict]) -> str:
    """Compact JSON of every photo's findings — the model's index of evidence."""
    rows = []
    for p in photos:
        if p.get("error"):
            continue
        rows.append({
            "label": p["label"],
            "source": p["source"],
            "space": p["space_label"],
            "caption": p["caption"],
            "systems": p["systems_visible"],
            "findings": [
                {"id": f["id"], "title": f["title"], "system": f["system"],
                 "severity": f["severity"], "urgency": f["urgency"],
                 "confidence": f["confidence"], "detail": f["detail"]}
                for f in p["findings"]
            ],
            "equipment": [
                {"kind": e["kind"], "brand": e["brand"], "year": e["manufacture_year"] or None}
                for e in p["equipment"]
            ],
        })
    return json.dumps(rows, ensure_ascii=False)


def synthesise(client: genai.Client, address: str, goal: str, photos: List[dict],
               images: Dict[str, Image.Image],
               exterior: Optional[PropertyInspectionReport]) -> Optional[InspectionSynthesis]:
    usable = [p for p in photos if not p.get("error")]
    if not usable:
        return None

    contents: List[object] = []
    for p in usable:
        img = images.get(p["label"])
        if img is None:
            continue
        contents.append(f"PHOTO {p['label']} ({p['source']}, {p['space_label']})")
        contents.append(gemini.shrink(img, THUMB_EDGE))

    seen = {s for p in usable for s in p["systems_visible"]}
    unseen = [SYSTEM_LABEL[k] for k in SYSTEM_LABEL if k not in seen]
    exterior_note = ""
    if exterior is not None:
        ap = exterior.architectural_profile
        exterior_note = (f"An exterior survey from satellite and Street View rated it "
                         f"{ap.overall_property_grade}: {ap.primary_style}, roof condition "
                         f"{exterior.roof_inspection.condition_score_1_to_10}/10.")

    contents.append(f"""You are the lead surveyor writing up the inspection of "{address}".
Each image above has already been inspected on its own; the findings, with IDs, are:

{_digest(usable)}

{exterior_note}
{GOALS.get(goal, GOALS[DEFAULT_GOAL])[1]}

Your job:
1. MERGE findings into issues. The same defect photographed more than once (one crack seen
   from two angles, one stain seen from the doorway and from below) is ONE issue citing every
   finding ID that shows it. Different defects stay separate, even when alike - damp in two
   different rooms is two issues, unless one cause is plausible, in which case say so in the
   summary. Place every finding ID in exactly one issue, except findings you judge false
   positives on review (a shadow read as a stain), which go in `dismissed`. You may raise or
   lower severity and urgency when the wider picture justifies it - damp in several rooms
   suggests something systemic.
2. GROUP the photos into distinct spaces. Use the thumbnails to tell rooms apart: two photos of
   one bedroom from different corners are one space; two different bedrooms are 'Bedroom 1'
   and 'Bedroom 2'. Put every photo label in exactly one space.
3. COVERAGE: name the important areas and systems the photos do not show at all, why each
   matters, and the exact photograph that would close the gap. Systems with no evidence in any
   photo: {', '.join(unseen) or 'none'}.
4. Strengths, questions to ask, the verdict and the executive summary - each tied to
   something actually seen.

Never mention prices, costs, budgets or valuations. Return strict JSON per the schema.""")

    return gemini.structured(
        client, InspectionSynthesis, contents, temperature=0.2,
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
        label="synthesis",
    )


# ── Merge ────────────────────────────────────────────────────────────────────
_SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
_URG_RANK = {u: i for i, u in enumerate(URGENCIES)}
_CONF_RANK = {c: i for i, c in enumerate(CONFIDENCES)}


def _issue_from(evidence: List[dict], title: str = "", system: str = "", severity: str = "",
                urgency: str = "", trade: str = "", summary: str = "",
                recommendation: str = "") -> dict:
    lead = evidence[0]
    sev = normalise_severity(severity) if severity else min(
        (f["severity"] for f in evidence), key=lambda s: _SEV_RANK[s])
    return {
        "title": (title or lead["title"]).strip()[:140],
        "system": normalise_system(system, lead["system"]) if system else lead["system"],
        "severity": sev,
        "urgency": normalise_urgency(urgency, sev) if urgency else lead["urgency"],
        "trade": (trade or lead["trade"]).strip()[:60],
        "summary": (summary or lead["detail"]).strip()[:900],
        "recommendation": (recommendation or lead["recommendation"]).strip()[:600],
        # The best evidence carries the issue: one clear photo outweighs two vague ones.
        "confidence": min((f["confidence"] for f in evidence), key=lambda c: _CONF_RANK[c]),
        "evidence": [f["id"] for f in evidence],
        "photos": list(dict.fromkeys(f["photo"] for f in evidence)),
    }


def merge(photos: List[dict], raw: Optional[InspectionSynthesis]) -> dict:
    """Validated issues, spaces and coverage, whatever the model returned."""
    findings: Dict[str, dict] = {f["id"]: f for p in photos for f in p["findings"]}
    labels = [p["label"] for p in photos]
    by_label = {p["label"]: p for p in photos}

    dismissed = set()
    if raw is not None:
        dismissed = {d.strip() for d in raw.dismissed if d.strip() in findings}
    for fid in dismissed:
        findings[fid]["dismissed"] = True

    used = set(dismissed)
    issues: List[dict] = []
    if raw is not None:
        for it in raw.issues:
            ids = [e.strip() for e in it.evidence]
            ev = [findings[i] for i in dict.fromkeys(ids) if i in findings and i not in used]
            if not ev:
                continue
            used.update(f["id"] for f in ev)
            issues.append(_issue_from(ev, it.title, it.system, it.severity, it.urgency,
                                      it.trade, it.summary, it.recommendation))

    # Anything the model did not place still counts. Silence is not a verdict.
    for fid, f in findings.items():
        if fid not in used:
            issues.append(_issue_from([f]))

    issues.sort(key=lambda i: (_SEV_RANK[i["severity"]], _URG_RANK[i["urgency"]],
                               _CONF_RANK[i["confidence"]], i["evidence"][0]))
    for n, issue in enumerate(issues, 1):
        issue["id"] = f"I{n:02d}"
        for fid in issue["evidence"]:
            findings[fid]["issue"] = issue["id"]

    # ── Spaces ──
    spaces: List[dict] = []
    placed = set()
    if raw is not None:
        for sp in raw.spaces:
            members = [l for l in dict.fromkeys(x.strip() for x in sp.photos)
                       if l in by_label and l not in placed]
            if not members:
                continue
            placed.update(members)
            kind = (sp.kind or "").strip().lower()
            spaces.append({
                "name": sp.name.strip()[:60] or by_label[members[0]]["space_label"],
                "kind": kind if kind in SPACES else by_label[members[0]]["space"],
                "photos": members,
                "summary": sp.summary.strip()[:500],
                "condition": sp.condition.strip()[:30] or "Not enough to judge",
            })
    for label in labels:
        if label in placed:
            continue
        p = by_label[label]
        name = p["space_label"]
        home = next((s for s in spaces if s["name"].lower() == name.lower()), None)
        if home is None:
            home = {"name": name, "kind": p["space"], "photos": [], "summary": p["caption"],
                    "condition": "Not enough to judge"}
            spaces.append(home)
        home["photos"].append(label)
        placed.add(label)

    order = {label: i for i, label in enumerate(labels)}
    spaces.sort(key=lambda s: min(order[l] for l in s["photos"]))
    issue_by_photo: Dict[str, set] = {}
    for issue in issues:
        for label in issue["photos"]:
            issue_by_photo.setdefault(label, set()).add(issue["id"])
    for sp in spaces:
        ids = set().union(*(issue_by_photo.get(l, set()) for l in sp["photos"]))
        sp["issues"] = sorted(ids)
        sp["is_exterior"] = all(by_label[l]["is_exterior"] for l in sp["photos"])

    gaps = []
    if raw is not None:
        for g in raw.coverage_gaps[:12]:
            if g.area.strip():
                gaps.append({"area": g.area.strip()[:80], "why": g.why_it_matters.strip()[:300],
                             "shot": g.shot.strip()[:300]})

    return {
        "verdict": raw.verdict.strip() if raw else "",
        "executive_summary": raw.executive_summary.strip() if raw else "",
        "issues": issues,
        "spaces": spaces,
        "strengths": [s.strip() for s in (raw.strengths if raw else []) if s.strip()][:10],
        "questions_to_ask": [q.strip() for q in (raw.questions_to_ask if raw else [])
                             if q.strip()][:12],
        "coverage_gaps": gaps,
        "dismissed": sorted(dismissed),
        "synthesised": raw is not None,
    }


def collect_equipment(photos: List[dict]) -> List[dict]:
    """Every equipment reading, with repeat sightings of one unit folded together."""
    out: List[dict] = []
    seen: Dict[Tuple[str, str, str], dict] = {}
    for p in photos:
        for e in p["equipment"]:
            ident = (e["model"] or e["serial"]).lower()
            key = (e["kind"].lower(), e["brand"].lower(), ident)
            if ident and key in seen:
                prior = seen[key]
                prior.setdefault("also_in", []).append(e["photo"])
                if not prior["manufacture_year"] and e["manufacture_year"]:
                    prior.update({k: e[k] for k in ("manufacture_year", "age_basis",
                                                    "age_years", "status")})
                continue
            item = dict(e)
            out.append(item)
            if ident:
                seen[key] = item
    rank = {"past_life": 0, "replacement_window": 1, "within_life": 2, "dated": 3, "unknown": 4}
    out.sort(key=lambda e: rank.get(e["status"], 5))
    return out


# ── Listing kit ──────────────────────────────────────────────────────────────
def listing_kit(client: genai.Client, address: str, photos: List[dict],
                exterior: Optional[PropertyInspectionReport],
                inspection: Optional[dict]) -> Optional[dict]:
    """Marketing copy and photo running order, from the owner's photos only."""
    owner = [p for p in photos if p["source"] == "owner" and not p.get("error")]
    if not owner:
        return None

    rows = [{
        "label": p["label"], "space": p["space_label"], "caption": p["caption"],
        "features": p["features"], "materials": p["materials"],
        "listing_appeal": p["listing_appeal"], "photo_quality": p["quality"],
        "photo_issues": p["photo_issues"], "retake_tip": p["retake_tip"],
        "visible_issues": [f["title"] for f in p["findings"]
                           if f["severity"] in ("minor", "cosmetic") and not f["dismissed"]],
    } for p in owner]

    facts = ""
    if exterior is not None:
        ap = exterior.architectural_profile
        facts = (f"Exterior survey: {ap.primary_style}, about {ap.estimated_stories} storeys, "
                 f"{', '.join(ap.exterior_siding_materials) or 'materials unclear'}; selling "
                 f"points seen from the street: {'; '.join(exterior.key_selling_points)}.")
    spaces = ", ".join(s["name"] for s in (inspection or {}).get("spaces", []))

    prompt = f"""You are a top-performing estate agent preparing the listing for "{address}".
You have these photographs (already described by an inspector):

{json.dumps(rows, ensure_ascii=False)}

Spaces photographed: {spaces or 'see above'}.
{facts}

Write the listing from what the photographs show:
- Never invent facts the photos cannot support: no square footage, no bedroom or bathroom
  counts beyond the distinct rooms photographed, no school, transport or neighbourhood claims,
  no prices. Say 'at least' where a count comes from photos.
- photo_order: every photo label worth using, strongest opener first, then the natural
  walk-through order. Leave out photos that would hurt the listing.
- retake: photos worth reshooting, with a concrete tip.
- pre_listing_fixes: quick visible fixes from the photos that would lift first impressions.
Return strict JSON per the schema."""

    raw = gemini.structured(client, ListingKit, [prompt], temperature=0.6, label="listing")

    valid = {p["label"] for p in owner}
    order = [l for l in dict.fromkeys(x.strip() for x in raw.photo_order) if l in valid]
    captions = {c.photo.strip(): c.note.strip() for c in raw.captions if c.photo.strip() in valid}
    for p in owner:
        captions.setdefault(p["label"], p["caption"])
    return {
        "headline": raw.headline.strip()[:120],
        "description": raw.description.strip(),
        "highlights": [h.strip() for h in raw.highlights if h.strip()][:10],
        "photo_order": order,
        "captions": captions,
        "retake": [{"photo": r.photo.strip(), "note": r.note.strip()}
                   for r in raw.retake if r.photo.strip() in valid][:12],
        "pre_listing_fixes": [f.strip() for f in raw.pre_listing_fixes if f.strip()][:10],
        "social_caption": raw.social_caption.strip(),
    }
