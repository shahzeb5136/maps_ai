"""The PDF builds for every payload shape the service has ever stored."""

from scanner.report_pdf import build_pdf
from tests import fakes


def _plates(tmp_path, names):
    for name in names:
        fakes.make_photo(len(name), (640, 480)).save(tmp_path / f"{name}.jpg")


def test_version_1_payload(tmp_path):
    """Scans from before the inspection layer must still produce a PDF."""
    _plates(tmp_path, ["satellite_topdown", "street_facade_front", "owner_photo_1"])
    payload = {
        "address": "9 Old Road", "scanned_at": "2026-01-01T00:00:00+00:00",
        "coordinates": {"lat": 1.0, "lng": 2.0},
        "imagery": {"satellite_topdown": "satellite_topdown.jpg",
                    "street_facade_front": "street_facade_front.jpg",
                    "owner_photo_1": "owner_photo_1.jpg"},
        "timeline": [], "inspection": fakes.property_report(),
        "temporal_analysis": None, "renovation": None, "pdf": None,
    }
    out = build_pdf(tmp_path / "r.pdf", payload, tmp_path)
    assert out.stat().st_size > 10_000


def test_photo_only_payload_without_exterior_survey(tmp_path):
    """No geocode, no Google imagery, no exterior survey: only the inspection."""
    _plates(tmp_path, ["owner_photo_1"])
    photo = {**fakes.photo_inspection("owner_photo_1"), "label": "owner_photo_1", "ref": "P01",
             "source": "owner", "file": "owner_photo_1.jpg", "thumb": None,
             "width": 0, "height": 0, "findings": [], "equipment": [], "error": None,
             "quality": 7, "listing_appeal": 6, "makeover_potential": 2}
    payload = {
        "address": "Old Mill", "scanned_at": "2026-01-01T00:00:00+00:00",
        "coordinates": None, "imagery": {"owner_photo_1": "owner_photo_1.jpg"},
        "timeline": [], "inspection": None, "temporal_analysis": None, "renovation": None,
        "pdf": None, "version": 2, "photos": [photo],
        "inspection_report": {"verdict": "", "executive_summary": "", "issues": [],
                              "spaces": [{"name": "Room", "kind": "other",
                                          "photos": ["owner_photo_1"], "summary": "",
                                          "condition": "Good", "issues": [],
                                          "is_exterior": False}],
                              "strengths": [], "questions_to_ask": [], "coverage_gaps": [],
                              "dismissed": [], "synthesised": False},
        "health": {"score": None, "grade": "—", "grade_label": "Not enough evidence",
                   "provisional": True, "capped_by_critical": False, "coverage_pct": 0,
                   "systems_assessed": 0, "systems_total": 12, "systems": [],
                   "counts": {}, "by_urgency": {}, "trades": [], "method": ""},
        "equipment": [], "listing": None, "makeovers": [], "solar": None, "site": None,
        "annotated": {}, "crops": {}, "satellite_roof": None,
    }
    out = build_pdf(tmp_path / "r.pdf", payload, tmp_path)
    assert out.stat().st_size > 5_000


def test_very_tall_photo_does_not_lose_the_pdf(tmp_path):
    """A 300x1600 photo in a 2-up grid used to raise LayoutError."""
    from PIL import Image
    Image.new("RGB", (300, 1600), (120, 120, 120)).save(tmp_path / "owner_photo_1.jpg")
    Image.new("RGB", (300, 1600), (90, 120, 160)).save(tmp_path / "makeover_1.jpg")
    photo = {**fakes.photo_inspection("owner_photo_1"), "label": "owner_photo_1", "ref": "P01",
             "source": "owner", "file": "owner_photo_1.jpg", "thumb": None, "width": 300,
             "height": 1600, "findings": [], "equipment": [], "error": None}
    payload = {
        "address": "Tall House", "scanned_at": "2026-01-01T00:00:00+00:00", "coordinates": None,
        "imagery": {"owner_photo_1": "owner_photo_1.jpg"}, "timeline": [], "inspection": None,
        "temporal_analysis": None, "renovation": None, "pdf": None, "version": 2,
        "photos": [photo], "style_name": "Japandi",
        "makeovers": [{"source": "owner_photo_1", "space": "Hall", "style": "japandi",
                       "style_name": "Japandi", "title": "Japandi hall", "fixes": [],
                       "after": "makeover_1.jpg"}],
    }
    assert build_pdf(tmp_path / "r.pdf", payload, tmp_path).exists()
