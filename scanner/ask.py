"""
Ask the property.

After a scan, the user can put questions to the photographs — "is that damp
under the window active?", "how old is the boiler?", "what would a buyer's
surveyor pick up?" — and get an answer that points at its evidence: the photo
it read the answer from and a box around the spot.

The model sees every photograph again (downscaled — the per-photo stage
already did the close reading) plus the structured inspection, so it answers
from both the pixels and the findings. It is told to say plainly when the
photos cannot answer a question, and which photo would.
"""

import json
import logging
from typing import Dict, List, Optional

from google import genai
from google.genai import types
from PIL import Image

from . import gemini
from .models import AskAnswer
from .scoring import normalise_box, normalise_confidence

log = logging.getLogger(__name__)

ASK_EDGE = 1024
MAX_IMAGES = 60

SYSTEM = (
    "You are the building surveyor who inspected this property from the photographs supplied. "
    "Answer the owner's questions from the photographs and the inspection record only. Cite the "
    "photo labels your answer rests on, with a box around the exact evidence where there is "
    "one. If the photographs cannot answer a question, say so plainly and say which photograph "
    "would. Never state or estimate prices, costs, budgets or valuations; describe scope, "
    "effort and who to call instead. Be direct and practical, not chatty."
)


def _context(payload: dict) -> str:
    photos = payload.get("photos") or []
    inspection = payload.get("inspection_report") or {}
    health = payload.get("health") or {}
    record = {
        "address": payload.get("address"),
        "photos": [
            {"label": p["label"], "source": p["source"], "space": p["space_label"],
             "description": p["description"],
             "findings": [{"id": f["id"], "title": f["title"], "severity": f["severity"],
                           "detail": f["detail"]} for f in p["findings"] if not f.get("dismissed")]}
            for p in photos if not p.get("error")
        ],
        "issues": [{"id": i["id"], "title": i["title"], "severity": i["severity"],
                    "urgency": i["urgency"], "trade": i["trade"], "photos": i["photos"]}
                   for i in inspection.get("issues", [])],
        "health_score": health.get("score"),
        "equipment": [{"kind": e["kind"], "brand": e["brand"], "model": e["model"],
                       "year": e["manufacture_year"] or None, "status": e["status"],
                       "photo": e["photo"]} for e in payload.get("equipment") or []],
        "exterior_survey": {
            k: (payload.get("inspection") or {}).get(k)
            for k in ("imagery_confidence", "architectural_profile", "roof_inspection",
                      "risk_factors_or_deferred_maintenance")
        },
    }
    solar = payload.get("solar")
    if solar:
        record["roof_measurements"] = {
            "roof_area_m2": solar.get("roof_area_m2"), "planes": len(solar.get("segments") or []),
            "max_panels": solar.get("max_panels"),
            "max_sunshine_hours": solar.get("max_sunshine_hours"),
        }
    return json.dumps(record, ensure_ascii=False)


def answer(client: genai.Client, question: str, payload: dict,
           images: Dict[str, Image.Image], history: List[dict]) -> dict:
    contents: List[object] = []
    for label, img in list(images.items())[:MAX_IMAGES]:
        contents.append(f"PHOTO {label}")
        contents.append(gemini.shrink(img, ASK_EDGE))

    past = "\n".join(f"Q: {h['question']}\nA: {h['answer']}" for h in history[-6:])
    contents.append(
        f"INSPECTION RECORD:\n{_context(payload)}\n\n"
        + (f"EARLIER IN THIS CONVERSATION:\n{past}\n\n" if past else "")
        + f"QUESTION: {question}\n\nBoxes are [ymin, xmin, ymax, xmax] on a 0-1000 grid over "
          "the cited photo. Return strict JSON per the schema."
    )

    raw = gemini.structured(
        client, AskAnswer, contents, temperature=0.2, system=SYSTEM,
        media_resolution=types.MediaResolution.MEDIA_RESOLUTION_MEDIUM, label="ask",
    )

    citations = []
    for c in raw.citations[:8]:
        label = c.photo.strip()
        if label not in images:
            continue
        citations.append({"photo": label, "note": c.note.strip()[:300],
                          "box": normalise_box(c.box_2d)})
    return {
        "answer": raw.answer.strip(),
        "confidence": normalise_confidence(raw.confidence),
        "citations": citations,
        "follow_ups": [f.strip() for f in raw.follow_ups if f.strip()][:3],
    }


def load_images(scan_dir, payload: dict, edge: int = ASK_EDGE) -> Dict[str, Image.Image]:
    """Every image the scan holds, by label, already downscaled to `edge`:
    fifty full-size photos per question would be ~300 MB of pixels."""
    files: Dict[str, str] = {}
    for p in payload.get("photos") or []:
        if p.get("file"):
            files[p["label"]] = p["file"]
    for label, name in (payload.get("imagery") or {}).items():
        files.setdefault(label, name)

    out: Dict[str, Image.Image] = {}
    for label, name in files.items():
        path = scan_dir / name
        try:
            with Image.open(path) as img:
                img.draft("RGB", (edge, edge))     # JPEG decodes at reduced scale
                small = img.convert("RGB")
                small.thumbnail((edge, edge), Image.LANCZOS)
                out[label] = small
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not open %s for Q&A: %s", path, exc)
        if len(out) >= MAX_IMAGES:
            break
    return out


def image_for(scan_dir, payload: dict, label: str) -> Optional[Image.Image]:
    name = None
    for p in payload.get("photos") or []:
        if p["label"] == label:
            name = p.get("file")
    name = name or (payload.get("imagery") or {}).get(label)
    if not name:
        return None
    try:
        with Image.open(scan_dir / name) as img:
            return img.convert("RGB")
    except Exception:  # noqa: BLE001
        return None
