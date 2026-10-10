from scanner import scoring
from scanner.scoring import (
    equipment_age,
    health_score,
    normalise_box,
    normalise_severity,
    normalise_system,
    normalise_urgency,
)


def test_box_normalisation():
    assert normalise_box([100, 200, 300, 400]) == [100, 200, 300, 400]
    # corners swapped, out of range
    assert normalise_box([300, 400, 100, -20]) == [100, 0, 300, 400]
    # fractions
    assert normalise_box([0.1, 0.2, 0.3, 0.4]) == [100, 200, 300, 400]
    # too thin is widened, not dropped
    y0, x0, y1, x1 = normalise_box([500, 500, 502, 900])
    assert y1 - y0 >= 30 and x1 - x0 == 400
    assert normalise_box([1, 2, 3]) is None
    assert normalise_box(None) is None
    assert normalise_box(["a", 1, 2, 3]) is None


def test_vocabulary():
    assert normalise_system("Damp") == "water"
    assert normalise_system("Roofing & gutters") == "roof"
    assert normalise_system("water_heater pipework") == "plumbing"
    assert normalise_system("nonsense", "exterior") == "exterior"
    assert normalise_system("windows_doors") == "windows_doors"
    assert normalise_severity("Safety hazard") == "critical"
    assert normalise_severity("Significant") == "major"
    assert normalise_severity("weird") == "minor"
    assert normalise_urgency("within 6 months", "minor") == "within_6_months"
    assert normalise_urgency("", "critical") == "immediate"


def _issue(system, severity, confidence="high"):
    return {"system": system, "severity": severity, "urgency": "monitor",
            "confidence": confidence, "trade": "Roofer"}


def test_health_score_unassessed_systems_are_gaps_not_passes():
    h = health_score([], ["roof"])
    assert h["score"] == 100
    assert h["systems_assessed"] == 1
    assert h["provisional"] is True
    roof = next(s for s in h["systems"] if s["key"] == "roof")
    plumbing = next(s for s in h["systems"] if s["key"] == "plumbing")
    assert roof["score"] == 100 and plumbing["score"] is None and plumbing["shot"]


def test_health_score_compounds_and_caps_on_critical():
    h = health_score([_issue("roof", "major"), _issue("roof", "major")], ["roof", "interior"])
    roof = next(s for s in h["systems"] if s["key"] == "roof")
    assert roof["score"] == round(100 * 0.78 * 0.78)
    h2 = health_score([_issue("electrical", "critical")],
                      [k for k in scoring.SYSTEM_KEYS])
    assert h2["score"] <= scoring.CRITICAL_CAP
    assert h2["capped_by_critical"] is True
    assert h2["provisional"] is False
    assert h2["counts"]["critical"] == 1


def test_low_confidence_counts_for_less():
    hi = health_score([_issue("roof", "major", "high")], ["roof"])
    lo = health_score([_issue("roof", "major", "low")], ["roof"])
    assert lo["score"] > hi["score"]


def test_equipment_age():
    boiler = equipment_age({"kind": "Gas combi boiler", "manufacture_year": 2009}, 2026)
    assert boiler["age_years"] == 17 and boiler["status"] == "past_life"
    heater = equipment_age({"kind": "Water heater (tank)", "manufacture_year": 2017}, 2026)
    assert heater["status"] == "replacement_window"
    tankless = equipment_age({"kind": "Tankless water heater", "manufacture_year": 2020}, 2026)
    assert tankless["life_label"] == "Tankless water heater" and tankless["status"] == "within_life"
    unknown = equipment_age({"kind": "Smoke alarm", "manufacture_year": 0}, 2026)
    assert unknown["status"] == "unknown" and unknown["age_years"] is None
    future = equipment_age({"kind": "Furnace", "manufacture_year": 2099}, 2026)
    assert future["manufacture_year"] == 0
    solar = equipment_age({"kind": "Solar panel array", "manufacture_year": 2020}, 2026)
    assert solar["life_label"] == "Solar panels"
