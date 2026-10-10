"""The whole scan, end to end, against fake Gemini and fake Google Maps."""

import json

import pytest


from scanner import ScanFailed, ScanOptions, analysis, pipeline
from tests import fakes


@pytest.fixture
def client(monkeypatch):
    c = fakes.FakeClient(fail_once=("PhotoInspection:owner_photo_2",))
    monkeypatch.setattr(analysis, "make_client", lambda key: c)
    return c


def _write_photos(out_dir, n):
    out_dir.mkdir(parents=True, exist_ok=True)
    names = []
    for i in range(1, n + 1):
        img = fakes.make_photo(i)
        img.save(out_dir / f"owner_photo_{i}.jpg", "JPEG")
        names.append(f"owner_photo_{i}.jpg")
    return names


def test_full_scan_with_photos(tmp_path, monkeypatch, client):
    fakes.patch_network(monkeypatch)
    names = _write_photos(tmp_path, 6)
    stages, live = [], []
    payload = pipeline.run_scan(
        "1 Test Street, London", tmp_path, lambda s, d: stages.append((s, d)), names,
        ScanOptions(goal="buying", style="japandi", on_live=live.append),
    )

    # Live feed: one snapshot per image, monotonic versions.
    assert [s["v"] for s in live] == list(range(1, 9))
    assert live[-1]["done"] == 8 and live[-1]["total"] == 8
    assert any(s == "photos" and "of 8" in d for s, d in stages)

    assert payload["version"] == 2
    labels = [p["label"] for p in payload["photos"]]
    assert labels[:6] == [f"owner_photo_{i}" for i in range(1, 7)]
    assert labels[6:] == ["satellite_topdown", "street_facade_front"]
    # The transient 503 on photo 2 was retried, not lost.
    assert payload["photos"][1]["error"] is None
    assert payload["photos"][1]["findings"][0]["id"] == "P02-1"

    report = payload["inspection_report"]
    ids = {i["title"]: i for i in report["issues"]}
    # The duplicate crack (owner photo + Street View) became one issue.
    crack = ids["Cracked render on front elevation"]
    assert set(crack["evidence"]) == {"P04-1", "SVF-1"}
    assert set(crack["photos"]) == {"owner_photo_4", "street_facade_front"}
    # Bogus evidence ID ignored; every real finding placed exactly once.
    assert "Phantom" not in ids
    placed = [e for i in report["issues"] for e in i["evidence"]]
    assert len(placed) == len(set(placed)) == 8
    # Sorted by severity: the critical electrical issue leads.
    assert report["issues"][0]["severity"] == "critical"
    assert report["issues"][0]["id"] == "I01"
    # Normalised vocabulary from messy model labels.
    mould = ids["Black mould at ceiling corner"]
    assert mould["system"] == "water" and mould["severity"] == "major"
    assert mould["urgency"] == "within_6_months"
    # Fraction box converted to thousandths.
    p3 = payload["photos"][2]
    assert p3["findings"][0]["box"] == [50, 700, 200, 950]

    # Every photo in exactly one space.
    in_spaces = [l for sp in report["spaces"] for l in sp["photos"]]
    assert sorted(in_spaces) == sorted(labels)
    front = next(sp for sp in report["spaces"] if sp["name"] == "Front elevation")
    assert set(front["photos"]) == {"owner_photo_4", "street_facade_front"}

    health = payload["health"]
    assert health["capped_by_critical"] is True and health["score"] <= 69
    assert health["counts"]["critical"] == 1

    eq = payload["equipment"]
    assert eq and eq[0]["status"] == "past_life" and eq[0]["life_label"] == "Combi boiler"

    # Makeovers: interior, high-potential, one per space, in the chosen style.
    assert 1 <= len(payload["makeovers"]) <= 3
    assert all(m["style"] == "japandi" for m in payload["makeovers"])
    assert {m["source"] for m in payload["makeovers"]} <= {"owner_photo_1", "owner_photo_2",
                                                           "owner_photo_6"}

    listing = payload["listing"]
    assert "not_a_photo" not in listing["photo_order"]
    assert listing["photo_order"][0] == "owner_photo_6"

    assert payload["solar"]["max_panels"] == 18
    assert payload["site"]["zoom"] == 20
    assert payload["renovation"] and payload["renovation"]["before_source"] == "owner_photo_4"
    assert payload["temporal_analysis"] is not None

    # Artifacts on disk.
    for p in payload["photos"]:
        assert (tmp_path / p["file"]).exists() and (tmp_path / p["thumb"]).exists()
    for name in payload["crops"].values():
        assert (tmp_path / name).exists()
    assert (tmp_path / "satellite_roof.jpg").exists()
    assert payload["pdf"] == "report.pdf" and (tmp_path / "report.pdf").stat().st_size > 50_000
    json.loads((tmp_path / "report.json").read_text())


def test_address_only_scan_still_gets_pinpoint_inspection(tmp_path, monkeypatch, client):
    fakes.patch_network(monkeypatch)
    payload = pipeline.run_scan("1 Test Street", tmp_path, None, [], ScanOptions())
    labels = [p["label"] for p in payload["photos"]]
    assert labels == ["satellite_topdown", "street_facade_front"]
    assert payload["health"]["score"] is not None
    assert payload["health"]["provisional"] is True
    assert payload["listing"] is None and payload["makeovers"] == []
    assert payload["pdf"] == "report.pdf"


def test_photos_only_when_address_cannot_be_found(tmp_path, monkeypatch, client):
    fakes.patch_network(monkeypatch, geocode_ok=False)
    names = _write_photos(tmp_path, 3)
    payload = pipeline.run_scan("Old Mill, somewhere", tmp_path, None, names, ScanOptions())
    assert payload["coordinates"] is None and payload["site"] is None
    assert payload["solar"] is None
    assert [p["source"] for p in payload["photos"]] == ["owner"] * 3
    assert payload["pdf"] == "report.pdf"


def test_no_address_and_no_photos_fails(tmp_path, monkeypatch, client):
    fakes.patch_network(monkeypatch, geocode_ok=False)
    with pytest.raises(ScanFailed):
        pipeline.run_scan("nowhere", tmp_path, None, [], ScanOptions())


def test_synthesis_failure_degrades_to_per_photo_issues(tmp_path, monkeypatch, client):
    fakes.patch_network(monkeypatch)
    from scanner import synthesis

    def boom(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(synthesis, "synthesise", boom)
    names = _write_photos(tmp_path, 4)
    payload = pipeline.run_scan("1 Test Street", tmp_path, None, names, ScanOptions())
    report = payload["inspection_report"]
    assert report["synthesised"] is False
    assert len(report["issues"]) == sum(len(p["findings"]) for p in payload["photos"])
    assert sorted(l for sp in report["spaces"] for l in sp["photos"]) == \
        sorted(p["label"] for p in payload["photos"])
