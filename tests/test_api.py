"""
The API end to end: upload, credits, the worker, the live feed, signed files,
questions and studio renders — against a real Postgres and the fake model.

Needs TEST_DATABASE_URL pointing at a disposable database; skipped otherwise.
"""

import io
import os
import time
from urllib.parse import urlparse

import pytest

if not os.environ.get("TEST_DATABASE_URL"):
    pytest.skip("TEST_DATABASE_URL not set", allow_module_level=True)

from fastapi.testclient import TestClient  # noqa: E402

from api import config, db, main  # noqa: E402
from api.auth import current_user  # noqa: E402
from scanner import analysis  # noqa: E402
from tests import fakes  # noqa: E402

USER = "user_test_1"


class SlowModels(fakes.FakeModels):
    """Slow enough per photo that the test can watch the live feed fill."""

    def generate_content(self, model=None, contents=None, config=None):
        if getattr(config, "response_schema", None) is not None and \
                config.response_schema.__name__ == "PhotoInspection":
            time.sleep(0.4)
        return super().generate_content(model=model, contents=contents, config=config)


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STORAGE_DIR", tmp_path)
    monkeypatch.setattr(config, "SCANS_DIR", tmp_path / "scans")
    fake = fakes.FakeClient()
    fake.models = SlowModels()
    monkeypatch.setattr(analysis, "make_client", lambda *a, **k: fake)
    monkeypatch.setattr(main, "make_client", lambda *a, **k: fake)
    monkeypatch.setattr(main, "_client", None)
    fakes.patch_network(monkeypatch)
    main.app.dependency_overrides[current_user] = lambda: USER
    with TestClient(main.app) as client:
        pool = db.pool()
        portal = client.portal
        portal.call(pool.execute, "DELETE FROM property_scans WHERE user_id = $1", USER)
        portal.call(pool.execute, """
            INSERT INTO users (id, credits, created_at) VALUES ($1, 3, 'now')
            ON CONFLICT (id) DO UPDATE SET credits = 3""", USER)
        client.sql = lambda q, *a: portal.call(pool.fetchval, q, *a)
        yield client
    main.app.dependency_overrides.clear()


def _jpeg(seed):
    return fakes.jpeg_bytes(fakes.make_photo(seed, (2400, 1800)))


def _local(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.path}?{parsed.query}"


def _wait(client, scan_id, until=("completed", "failed"), timeout=60, seen=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/scans/{scan_id}").json()
        if seen is not None and body.get("live"):
            seen.append(body["live"])
        if body["status"] in until:
            return body
        time.sleep(0.1)
    raise AssertionError(f"scan did not reach {until}: {body['status']} {body.get('stage')}")


def test_limits_endpoint(api):
    body = api.get("/api/limits").json()
    assert body["max_photos"] == 50
    assert {g["key"] for g in body["goals"]} == {"buying", "selling", "owning", "renovating"}
    assert body["default_style"] in {s["key"] for s in body["styles"]}


def test_scan_lifecycle(api):
    files = [("images", (f"IMG_{i}.jpg", _jpeg(i), "image/jpeg")) for i in range(1, 5)]
    res = api.post("/api/scans", data={"address": "1 Test Street, London", "goal": "selling",
                                       "style": "coastal"}, files=files)
    assert res.status_code == 201, res.text
    scan = res.json()["scan"]
    assert scan["owner_photos"] == 4 and scan["goal"] == "selling" and scan["style"] == "coastal"
    assert res.json()["credits_remaining"] == 2

    # One at a time per user.
    again = api.post("/api/scans", data={"address": "2 Test Street"})
    assert again.status_code == 409

    live = []
    done = _wait(api, scan["id"], seen=live)
    assert done["status"] == "completed", done.get("error_message")
    assert live, "live feed never appeared while running"
    assert live[-1]["urls"], "live feed carried no image URLs"
    assert all(u.startswith("/api/scans/") for u in live[-1]["urls"].values())
    # The live feed is cleared once the result exists.
    assert done["live"] is None
    assert done["allowances"] == {"ask_limit": config.ASK_LIMIT_PER_SCAN, "asks_used": 0,
                                  "render_limit": config.RENDER_LIMIT_PER_SCAN,
                                  "renders_used": 0}

    result = done["result"]
    assert result["version"] == 2 and result["style"] == "coastal"
    assert "crops" not in result and "annotated" not in result
    photo = result["photos"][0]
    img = api.get(_local(photo["thumb_url"]))
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert api.get(_local(photo["url"]).replace("signature=", "signature=0")).status_code == 403
    assert api.get(_local(result["pdf_url"])).headers["content-type"] == "application/pdf"
    for m in result["makeovers"]:
        assert api.get(_local(m["after_url"])).status_code == 200

    sid = scan["id"]
    # ── Ask ──
    ans = api.post(f"/api/scans/{sid}/ask", json={"question": "Any damp?"})
    assert ans.status_code == 200, ans.text
    body = ans.json()["answer"]
    assert body["citations"][0]["photo"] == "owner_photo_3"
    assert body["citations"][0]["box"] == [50, 700, 200, 950]
    assert len(body["citations"]) == 1   # the unknown label was dropped
    assert api.post(f"/api/scans/{sid}/ask", json={"question": "x"}).status_code == 422
    qs = api.get(f"/api/scans/{sid}/questions").json()
    assert qs["used"] == 1 and qs["questions"][0]["answer"]["answer"]

    # ── Studio ──
    r = api.post(f"/api/scans/{sid}/renders", json={"source": "owner_photo_1",
                                                    "mode": "stage", "style": "japandi"})
    assert r.status_code == 200, r.text
    render = r.json()
    assert render["status"] == "completed" and render["style_name"] == "Japandi"
    assert api.get(_local(render["url"])).status_code == 200
    assert api.post(f"/api/scans/{sid}/renders",
                    json={"source": "owner_photo_1", "mode": "bogus"}).status_code == 422
    assert api.post(f"/api/scans/{sid}/renders",
                    json={"source": "owner_photo_1", "mode": "custom",
                          "instruction": "x"}).status_code == 422
    assert api.post(f"/api/scans/{sid}/renders",
                    json={"source": "satellite_topdown", "mode": "restyle"}).status_code == 422
    twi = api.post(f"/api/scans/{sid}/renders",
                   json={"source": "street_facade_front", "mode": "twilight"})
    assert twi.status_code == 200
    assert api.get(f"/api/scans/{sid}/renders").json()["used"] == 2

    # ── Allowances ──
    old = config.ASK_LIMIT_PER_SCAN
    config.ASK_LIMIT_PER_SCAN = 1
    try:
        capped = api.post(f"/api/scans/{sid}/ask", json={"question": "And the roof?"})
        assert capped.status_code == 429
    finally:
        config.ASK_LIMIT_PER_SCAN = old

    # ── Delete cascades ──
    assert api.delete(f"/api/scans/{sid}").json()["deleted"] is True
    assert api.sql("SELECT count(*) FROM property_scan_questions WHERE scan_id = $1", sid) == 0
    assert api.sql("SELECT count(*) FROM property_scan_renders WHERE scan_id = $1", sid) == 0


def test_rejects_bad_upload_without_charging(api):
    files = [("images", ("notes.txt", b"not an image at all", "text/plain"))]
    res = api.post("/api/scans", data={"address": "1 Test Street"}, files=files)
    assert res.status_code == 422
    assert api.sql("SELECT credits FROM users WHERE id = $1", USER) == 3


def test_too_many_photos(api, monkeypatch):
    monkeypatch.setattr(config, "MAX_UPLOAD_IMAGES", 2)
    files = [("images", (f"{i}.jpg", _jpeg(i), "image/jpeg")) for i in range(3)]
    res = api.post("/api/scans", data={"address": "1 Test Street"}, files=files)
    assert res.status_code == 422 and "limit is 2" in res.json()["detail"]


def test_heic_upload_is_accepted(api):
    pillow_heif = pytest.importorskip("pillow_heif")
    img = fakes.make_photo(3)
    buf = io.BytesIO()
    pillow_heif.from_pillow(img).save(buf, quality=80)
    res = api.post("/api/scans", data={"address": "1 Test Street"},
                   files=[("images", ("IMG_0001.HEIC", buf.getvalue(), "image/heic"))])
    assert res.status_code == 201, res.text
    _wait(api, res.json()["scan"]["id"])


def test_json_submission_still_works(api):
    res = api.post("/api/scans", json={"address": "1 Test Street", "goal": "nonsense"})
    assert res.status_code == 201
    assert res.json()["scan"]["goal"] == "buying"
    _wait(api, res.json()["scan"]["id"])


def test_insufficient_credits(api):
    api.sql("UPDATE users SET credits = 0 WHERE id = $1 RETURNING 1", USER)
    assert api.post("/api/scans", data={"address": "1 Test Street"}).status_code == 402


def test_failed_renders_hit_a_hard_ceiling(api, monkeypatch):
    """Refusals do not use the allowance, but cannot be retried for ever."""
    from scanner import gemini as gem
    res = api.post("/api/scans", json={"address": "1 Test Street"})
    sid = res.json()["scan"]["id"]
    _wait(api, sid)
    monkeypatch.setattr(config, "RENDER_LIMIT_PER_SCAN", 1)
    monkeypatch.setattr(gem, "edit_image", lambda *a, **k: None)
    body = {"source": "street_facade_front", "mode": "twilight"}
    assert api.post(f"/api/scans/{sid}/renders", json=body).status_code == 502
    assert api.post(f"/api/scans/{sid}/renders", json=body).status_code == 502
    assert api.post(f"/api/scans/{sid}/renders", json=body).status_code == 429


def test_signed_urls_are_stable_within_the_hour(api):
    from api import storage
    assert storage.sign("abc", "x.jpg") == storage.sign("abc", "x.jpg")


def test_owner_marks_an_issue_not_an_issue(api):
    """Dismissing takes the issue out of the score and the PDF; restoring puts it back."""
    files = [("images", (f"IMG_{i}.jpg", _jpeg(i), "image/jpeg")) for i in range(1, 5)]
    sid = api.post("/api/scans", data={"address": "1 Test Street"}, files=files).json()["scan"]["id"]
    before = _wait(api, sid)["result"]
    critical = next(i for i in before["inspection_report"]["issues"] if i["severity"] == "critical")
    assert before["health"]["capped_by_critical"] is True

    res = api.post(f"/api/scans/{sid}/issues/{critical['id']}", json={"dismissed": True})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["dismissed_ids"] == [critical["id"]]
    assert body["health"]["capped_by_critical"] is False
    assert body["health"]["counts"]["critical"] == 0
    assert body["health"]["score"] > before["health"]["score"]
    assert "report_r1.pdf" in body["pdf_url"] and body["version"] == 1
    assert api.get(_local(body["pdf_url"])).headers["content-type"] == "application/pdf"

    # It sticks: a fresh read of the scan carries the correction.
    after = api.get(f"/api/scans/{sid}").json()["result"]
    flagged = next(i for i in after["inspection_report"]["issues"] if i["id"] == critical["id"])
    assert flagged["dismissed_by_user"] is True
    assert after["health"]["score"] == body["health"]["score"]

    restored = api.post(f"/api/scans/{sid}/issues/{critical['id']}", json={"dismissed": False}).json()
    assert restored["dismissed_ids"] == []
    assert restored["health"]["score"] == before["health"]["score"]
    assert "report_r2.pdf" in restored["pdf_url"] and restored["version"] == 2
    # The superseded PDF is gone; the original from the scan itself is kept.
    assert api.get(_local(body["pdf_url"])).status_code == 404
    assert api.post(f"/api/scans/{sid}/issues/I99", json={"dismissed": True}).status_code == 404


def test_dismissing_every_issue_still_builds_the_report(api):
    """An empty register, an empty plan and the summary note must all still render."""
    files = [("images", (f"IMG_{i}.jpg", _jpeg(i), "image/jpeg")) for i in range(1, 4)]
    sid = api.post("/api/scans", data={"address": "2 Test Street"}, files=files).json()["scan"]["id"]
    issues = _wait(api, sid)["result"]["inspection_report"]["issues"]
    assert issues

    for n, issue in enumerate(issues, start=1):
        body = api.post(f"/api/scans/{sid}/issues/{issue['id']}", json={"dismissed": True}).json()
        assert f"report_r{n}.pdf" in body["pdf_url"], body["pdf_url"]
    assert sorted(body["dismissed_ids"]) == sorted(i["id"] for i in issues)
    assert sum(body["health"]["counts"].values()) == 0
    assert api.get(_local(body["pdf_url"])).headers["content-type"] == "application/pdf"
