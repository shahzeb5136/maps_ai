"""
Deterministic scoring.

The model reads photographs; it does not get to invent the headline number.
Every score in the report is computed here, from the findings, by rules a
reader can check: each issue knocks a fixed fraction off its system according
to its severity, systems are combined by fixed weights, and a system nobody
photographed is reported as "not assessed" instead of being quietly assumed
fine. The same rule turns a legible data-plate year into an age against a
published service-life range.

Also home to the vocabulary normalisers, because the model's free-text
labels ("Moisture", "damp", "Leak") have to land on one set of keys before
anything can be counted.
"""

import re
from collections import Counter
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ── Building systems ─────────────────────────────────────────────────────────
# (key, label, weight, what to photograph to assess it). Weights reflect what
# costs most when it goes wrong: a failing structure or an active leak matters
# more than a scuffed floor.
SYSTEMS: List[Tuple[str, str, float, str]] = [
    ("structure", "Structure & foundations", 3.0,
     "Foundations and any visible cracking in walls, ceilings or chimney stacks"),
    ("roof", "Roof, gutters & chimneys", 2.5,
     "The roof covering from a distance, gutters and downpipes, and the chimney"),
    ("water", "Water, damp & mould", 2.5,
     "Ceilings under bathrooms, around windows, basement walls and under sinks"),
    ("electrical", "Electrical", 2.0,
     "The consumer unit / breaker panel with its door open, and a few outlets"),
    ("safety", "Safety & fire", 2.0,
     "Smoke and CO alarms, stair balustrades and handrails, external steps"),
    ("plumbing", "Plumbing & drainage", 1.5,
     "Under the kitchen and bathroom sinks, the water heater, visible pipework"),
    ("hvac", "Heating & cooling", 1.5,
     "The boiler or furnace and its data plate, radiators or vents, AC units"),
    ("exterior", "Walls, cladding & trim", 1.5,
     "Each elevation of the building, close enough to see the wall surface"),
    ("windows_doors", "Windows & doors", 1.0,
     "Window frames and sills inside and out, and the external doors"),
    ("kitchen_bath", "Kitchens & bathrooms", 1.0,
     "The kitchen and every bathroom, including the shower or bath surround"),
    ("interior", "Interior finishes", 0.75,
     "Each room's walls, ceiling and floor in a wide shot"),
    ("grounds", "Grounds & outbuildings", 0.75,
     "The garden, paths, driveway, fences, retaining walls and outbuildings"),
]
SYSTEM_KEYS = [k for k, *_ in SYSTEMS]
SYSTEM_LABEL = {k: label for k, label, *_ in SYSTEMS}
SYSTEM_WEIGHT = {k: w for k, _, w, _ in SYSTEMS}
SYSTEM_SHOT = {k: shot for k, _, _, shot in SYSTEMS}

SYSTEMS_PROMPT = ", ".join(f"'{k}' ({label})" for k, label, *_ in SYSTEMS)

# Order matters: the first matching pattern wins, so the specific ones lead.
_SYSTEM_SYNONYMS: List[Tuple[str, str]] = [
    (r"damp|moist|mou?ld|leak|water ?damage|condensation|stain|efflorescence|rising", "water"),
    (r"struct|foundation|subsid|settle|lintel|beam|joist|load.?bearing", "structure"),
    (r"roof|gutter|chimney|flashing|fascia|soffit|downpipe|downspout|shingle", "roof"),
    (r"electr|wiring|outlet|socket|breaker|panel|consumer unit|fuse|switch|light fixture", "electrical"),
    (r"safe|fire|smoke|carbon|\bco\b|hazard|trip|handrail|balust|railing|guard|egress", "safety"),
    (r"plumb|pipe|drain|sewer|toilet|tap|faucet|valve|water heater|sink", "plumbing"),
    (r"hvac|heat|cool|boiler|furnace|radiator|vent|air ?con|\bac\b|duct|thermostat", "hvac"),
    (r"window|door|glaz|frame|sill", "windows_doors"),
    (r"kitchen|bath|shower|tub|vanity|cabinet|counter|worktop|appliance", "kitchen_bath"),
    (r"ground|garden|yard|landscap|drive|path|fence|retaining|deck|patio|shed|garage|pool|tree", "grounds"),
    (r"exterior|cladding|siding|render|stucco|brick|masonry|facade|façade|paint|trim|wall", "exterior"),
    (r"interior|floor|ceiling|plaster|drywall|finish|carpet|skirting|stair", "interior"),
]


def normalise_system(raw: Optional[str], fallback: str = "interior") -> str:
    key = (raw or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in SYSTEM_LABEL:
        return key
    text = (raw or "").lower()
    for pattern, system in _SYSTEM_SYNONYMS:
        if re.search(pattern, text):
            return system
    return fallback


# ── Severity / urgency / confidence ──────────────────────────────────────────
SEVERITIES = ["critical", "major", "minor", "cosmetic"]
SEVERITY_LABEL = {"critical": "Critical", "major": "Major", "minor": "Minor",
                  "cosmetic": "Cosmetic"}

URGENCIES = ["immediate", "within_6_months", "within_2_years", "monitor"]
URGENCY_LABEL = {"immediate": "Immediately", "within_6_months": "Within 6 months",
                 "within_2_years": "Within 1-2 years", "monitor": "Monitor"}
_DEFAULT_URGENCY = {"critical": "immediate", "major": "within_6_months",
                    "minor": "within_2_years", "cosmetic": "within_2_years"}

CONFIDENCES = ["high", "medium", "low"]


def normalise_severity(raw: Optional[str]) -> str:
    t = (raw or "").strip().lower()
    if t in SEVERITIES:
        return t
    if re.search(r"crit|urgent|danger|hazard|safety|severe|emergency", t):
        return "critical"
    if re.search(r"major|significant|serious|high", t):
        return "major"
    if re.search(r"cosmet|aesthetic|appearance|trivial", t):
        return "cosmetic"
    return "minor"


def normalise_urgency(raw: Optional[str], severity: str) -> str:
    t = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if t in URGENCIES:
        return t
    if re.search(r"immedi|urgent|now|asap|emergency", t):
        return "immediate"
    if re.search(r"6|six|soon|short|month", t):
        return "within_6_months"
    if re.search(r"year|plan|2|two|medium|long|conven", t):
        return "within_2_years"
    if re.search(r"monitor|watch|observ", t):
        return "monitor"
    return _DEFAULT_URGENCY.get(severity, "within_2_years")


def normalise_confidence(raw: Optional[str]) -> str:
    t = (raw or "").strip().lower()
    for c in CONFIDENCES:
        if t.startswith(c):
            return c
    return "medium"


def normalise_box(raw: Optional[Sequence]) -> Optional[List[int]]:
    """
    A model box as [ymin, xmin, ymax, xmax] on a 0-1000 grid, or None.

    Tolerates the usual slips: fractions instead of thousandths, corners in
    the wrong order, values a hair outside the frame. A box too thin to see is
    widened rather than dropped — the finding is still worth pointing at.
    """
    if not raw or len(raw) != 4:
        return None
    try:
        vals = [float(v) for v in raw]
    except (TypeError, ValueError):
        return None
    if all(0.0 <= v <= 1.0 for v in vals) and any(v > 0 for v in vals):
        vals = [v * 1000 for v in vals]
    y0, x0, y1, x1 = (max(0.0, min(1000.0, v)) for v in vals)
    y0, y1 = sorted((y0, y1))
    x0, x1 = sorted((x0, x1))
    if y1 - y0 < 1 and x1 - x0 < 1:
        return None
    # Minimum visible size: 30 thousandths on each axis.
    def widen(a: float, b: float) -> Tuple[float, float]:
        if b - a >= 30:
            return a, b
        mid = (a + b) / 2
        a, b = mid - 15, mid + 15
        if a < 0:
            a, b = 0, 30
        if b > 1000:
            a, b = 970, 1000
        return a, b
    y0, y1 = widen(y0, y1)
    x0, x1 = widen(x0, x1)
    return [int(round(y0)), int(round(x0)), int(round(y1)), int(round(x1))]


def clamp_int(value, lo: int, hi: int, default: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


# ── Health score ─────────────────────────────────────────────────────────────
# Fraction of a system's 100 points each open issue removes, compounding: two
# majors leave 100 * 0.78 * 0.78 = 61, not 56. Low-confidence findings count
# for less, because "possibly" should not weigh as much as "clearly".
PENALTY = {"critical": 0.40, "major": 0.22, "minor": 0.08, "cosmetic": 0.03}
CONFIDENCE_WEIGHT = {"high": 1.0, "medium": 0.75, "low": 0.4}

# While a critical issue is open the overall grade cannot read better than C:
# a pristine kitchen does not offset live wiring.
CRITICAL_CAP = 69

GRADES = [(85, "A", "Excellent"), (70, "B", "Good"), (55, "C", "Fair"),
          (40, "D", "Poor"), (0, "E", "Serious concerns")]


def grade_for(score: Optional[int]) -> Tuple[str, str]:
    if score is None:
        return "—", "Not enough evidence"
    for floor, letter, word in GRADES:
        if score >= floor:
            return letter, word
    return "E", "Serious concerns"


def health_score(issues: Iterable[dict], systems_seen: Iterable[str]) -> dict:
    """
    Score the property from its merged issues.

    `systems_seen` is every system at least one photograph gave evidence about.
    A system with no evidence gets `score: None` and is left out of the
    weighted mean — it is reported as a gap, never as a pass.
    """
    issues = list(issues)
    seen = set(systems_seen) | {i["system"] for i in issues}

    systems = []
    for key, label, weight, shot in SYSTEMS:
        mine = [i for i in issues if i["system"] == key]
        assessed = key in seen
        score = None
        if assessed:
            remaining = 1.0
            for issue in mine:
                p = PENALTY.get(issue["severity"], 0.08)
                remaining *= 1 - p * CONFIDENCE_WEIGHT.get(issue.get("confidence", "medium"), 0.75)
            score = int(round(100 * remaining))
        worst = next((s for s in SEVERITIES if any(i["severity"] == s for i in mine)), None)
        systems.append({
            "key": key, "label": label, "weight": weight, "assessed": assessed,
            "score": score, "issues": len(mine), "worst": worst,
            "shot": None if assessed else shot,
        })

    assessed = [s for s in systems if s["assessed"]]
    overall: Optional[int] = None
    capped = False
    if assessed:
        total_w = sum(s["weight"] for s in assessed)
        overall = int(round(sum(s["score"] * s["weight"] for s in assessed) / total_w))
        if any(i["severity"] == "critical" for i in issues) and overall > CRITICAL_CAP:
            overall, capped = CRITICAL_CAP, True

    letter, word = grade_for(overall)
    counts = Counter(i["severity"] for i in issues)
    urgency = Counter(i["urgency"] for i in issues)
    trades = Counter(i.get("trade") or "General contractor" for i in issues)

    # Most scans carry a handful of photos, which evidence three or four systems
    # at best. The score is still worth stating — it is honest about what was
    # seen — but it is flagged provisional until it covers half the building.
    provisional = len(assessed) < len(SYSTEMS) / 2

    return {
        "score": overall,
        "grade": letter,
        "grade_label": word,
        "provisional": provisional,
        "capped_by_critical": capped,
        "coverage_pct": int(round(100 * len(assessed) / len(SYSTEMS))),
        "systems_assessed": len(assessed),
        "systems_total": len(SYSTEMS),
        "systems": systems,
        "counts": {s: counts.get(s, 0) for s in SEVERITIES},
        "by_urgency": {u: urgency.get(u, 0) for u in URGENCIES},
        "trades": [{"trade": t, "issues": n} for t, n in trades.most_common()],
        "method": ("Each issue removes a fixed share of its system's score by severity "
                   "(critical 40%, major 22%, minor 8%, cosmetic 3%, compounding; "
                   "low-confidence findings count for less). Systems are combined by "
                   "fixed weights; systems with no photographic evidence are excluded "
                   "and listed as not assessed. Any open critical issue caps the "
                   f"overall score at {CRITICAL_CAP}."),
    }


# ── Equipment service life ───────────────────────────────────────────────────
# Typical service lives in years, from the ranges home-inspection bodies
# publish (InterNACHI, NAHB). The first matching row wins, so the specific
# ones lead: "tankless water heater" must not fall through to "water heater".
_SERVICE_LIFE: List[Tuple[Tuple[str, ...], str, int, int]] = [
    (("combi",), "Combi boiler", 10, 15),
    (("tankless", "on-demand", "instantaneous"), "Tankless water heater", 18, 25),
    (("boiler",), "Boiler", 15, 20),
    (("water heater", "hot water cylinder", "cylinder", "geyser", "hot water tank"), "Water heater (tank)", 8, 12),
    (("furnace",), "Furnace", 15, 20),
    (("heat pump",), "Heat pump", 12, 16),
    (("condenser", "air condition", "a/c", "ac unit", "mini-split", "mini split", "split system",
      "air handler"), "Air conditioning", 10, 15),
    (("carbon monoxide", "co alarm", "co detector"), "CO alarm", 5, 7),
    (("smoke",), "Smoke alarm", 8, 10),
    (("solar panel", "pv module", "photovoltaic"), "Solar panels", 25, 30),
    (("inverter",), "Solar inverter", 10, 15),
    (("panel", "consumer unit", "breaker", "fuse box", "fusebox", "distribution board"),
     "Electrical panel", 25, 40),
    (("dishwasher",), "Dishwasher", 9, 12),
    (("refrigerator", "fridge", "freezer"), "Refrigerator", 10, 15),
    (("washing machine", "washer"), "Washing machine", 10, 14),
    (("dryer",), "Clothes dryer", 10, 14),
    (("oven", "range", "cooker", "stove", "hob", "cooktop"), "Cooker / range", 13, 18),
    (("microwave",), "Microwave", 8, 10),
    (("garage door opener", "garage door motor"), "Garage door opener", 10, 15),
    (("water softener",), "Water softener", 10, 15),
    (("sump pump",), "Sump pump", 7, 10),
    (("well pump", "pressure tank"), "Well pump", 8, 15),
    (("ev charger", "wallbox"), "EV charger", 10, 15),
]


def service_life(kind: str) -> Optional[Tuple[str, int, int]]:
    k = (kind or "").lower()
    for keys, label, lo, hi in _SERVICE_LIFE:
        if any(key in k for key in keys):
            return label, lo, hi
    return None


def equipment_age(item: dict, now_year: Optional[int] = None) -> dict:
    """Attach age, service-life range and a status to one equipment reading."""
    now_year = now_year or datetime.now(timezone.utc).year
    year = item.get("manufacture_year") or 0
    life = service_life(item.get("kind", ""))
    out = dict(item)
    out["age_years"] = None
    out["life_label"] = life[0] if life else None
    out["life_low"] = life[1] if life else None
    out["life_high"] = life[2] if life else None
    out["status"] = "unknown"
    if isinstance(year, int) and 1950 <= year <= now_year:
        age = now_year - year
        out["age_years"] = age
        if life:
            if age > life[2]:
                out["status"] = "past_life"
            elif age >= life[1]:
                out["status"] = "replacement_window"
            else:
                out["status"] = "within_life"
        else:
            out["status"] = "dated"
    else:
        out["manufacture_year"] = 0
    return out
