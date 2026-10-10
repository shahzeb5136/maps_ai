"""
Property Scanner API.

Endpoints (all but /health and the signed file route require a Clerk bearer token):

    GET    /health                              liveness + config sanity
    GET    /api/credits                         wallet balance
    GET    /api/scans                           the caller's scans, newest first
    POST   /api/scans          {address}        charge 1 credit, queue a scan
    GET    /api/scans/{id}                      one scan, with signed asset URLs
    DELETE /api/scans/{id}                      remove a scan and its files
    GET    /api/scans/{id}/files/{name}         signed artifact (image or PDF)

    GET    /api/limits                          upload limits, goals, styles (no auth)
    POST   /api/scans/{id}/ask      {question}  ask the finished scan a question
    GET    /api/scans/{id}/questions            the scan's questions and answers
    POST   /api/scans/{id}/renders  {source, mode, style?, instruction?}
                                                render one photo in the studio
    GET    /api/scans/{id}/renders              the scan's studio renders
"""

import asyncio
import concurrent.futures
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
# Starlette's UploadFile, not FastAPI's: the form parser produces the former,
# and FastAPI's is a *subclass*, so isinstance against it silently misses
# every real upload.
from starlette.datastructures import UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

from scanner import ask as ask_stage
from scanner import config as scanner_config
from scanner import makeovers
from scanner.analysis import make_client
from scanner.photos import DEFAULT_GOAL, GOALS

from . import auth, config, db, storage, uploads, worker
from .auth import current_user

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("property-scanner")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.validate()
    scanner_config.require_keys()
    storage.ensure_dirs()
    await db.connect()
    await db.reclaim_orphans()
    worker.init()
    log.info("Property Scanner API ready — storage=%s origins=%s",
             config.STORAGE_DIR, config.ALLOWED_ORIGINS)
    yield
    await worker.shutdown()
    await db.disconnect()


app = FastAPI(title="Property Scanner API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=False,          # bearer tokens, not cookies
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# ── Schemas ──────────────────────────────────────────────────────────────────
def clean_address(raw: str) -> str:
    """Collapse whitespace and bound the length. Multipart form fields are
    plain strings, so this replaces what a pydantic body model used to do."""
    value = " ".join((raw or "").split())
    if len(value) < 3:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Enter an address with at least 3 characters.")
    if len(value) > 300:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "That address is too long.")
    return value


class ScanSummary(BaseModel):
    id: str
    address: str
    status: str
    #: How many owner-supplied photos were attached to this scan.
    owner_photos: int = 0
    #: What the scan is for ("buying", "selling", ...) and the makeover style.
    goal: Optional[str] = None
    style: Optional[str] = None
    stage: Optional[str] = None
    stage_detail: Optional[str] = None
    credits_spent: int
    error_message: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class ScanDetail(ScanSummary):
    result: Optional[Dict[str, Any]] = None
    #: While running: the per-photo feed, with signed image URLs.
    live: Optional[Dict[str, Any]] = None
    #: Once complete: how much of the included Q&A and studio allowance is used.
    allowances: Optional[Dict[str, int]] = None


class SubmitResponse(BaseModel):
    scan: ScanSummary
    credits_remaining: int


# ── Serialisation ────────────────────────────────────────────────────────────
def _iso(value) -> Optional[str]:
    return value.isoformat() if value else None


def _summary(row) -> ScanSummary:
    return ScanSummary(
        id=row["id"],
        address=row["address"],
        status=row["status"],
        stage=row["stage"],
        stage_detail=row["stage_detail"],
        credits_spent=row["credits_spent"],
        owner_photos=row["owner_photos"] if "owner_photos" in row.keys() else 0,
        goal=row["goal"] if "goal" in row.keys() else None,
        style=row["style"] if "style" in row.keys() else None,
        error_message=row["error_message"],
        created_at=_iso(row["created_at"]) or "",
        started_at=_iso(row["started_at"]),
        completed_at=_iso(row["completed_at"]),
    )


def _json(value):
    """asyncpg hands back jsonb as text."""
    return json.loads(value) if isinstance(value, str) else value


def _result(row) -> Optional[Dict[str, Any]]:
    return _json(row["result_json"]) if "result_json" in row.keys() else None


async def _detail(row) -> ScanDetail:
    result = _result(row)
    if result:
        result = storage.sign_payload(row["id"], result)

    live = None
    if row["status"] in db.ACTIVE_STATUSES and "live_json" in row.keys() and row["live_json"]:
        live = storage.sign_live(row["id"], _json(row["live_json"]))

    allowances = None
    if row["status"] == db.COMPLETED:
        allowances = {
            "ask_limit": config.ASK_LIMIT_PER_SCAN,
            "asks_used": await db.count_used("property_scan_questions", row["id"]),
            "render_limit": config.RENDER_LIMIT_PER_SCAN,
            "renders_used": await db.count_used("property_scan_renders", row["id"]),
        }
    return ScanDetail(**_summary(row).model_dump(), result=result, live=live,
                      allowances=allowances)


# ── Routes ───────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    """
    Liveness plus a config self-check. Everything reported here is
    non-sensitive — an issuer URL, an origin list, a row count — and it turns
    the three misconfigurations that actually happen (wrong Clerk instance,
    unmounted volume, wrong database) into something you can see in one curl
    instead of inferring from a 401.
    """
    db_ok, db_error, user_rows = True, None, None
    try:
        user_rows = await db.pool().fetchval("SELECT count(*) FROM users")
    except Exception as exc:
        db_ok, db_error = False, str(exc)[:200]

    return {
        "status": "ok",
        "service": "property-scanner",
        "credit_cost": config.CREDIT_COST,
        "storage_writable": os.access(config.STORAGE_DIR, os.W_OK),
        "public_base_url": config.PUBLIC_BASE_URL or "(derived per-request)",
        "allowed_origins": config.ALLOWED_ORIGINS,
        "clerk": auth.probe_jwks(),
        "database": {"connected": db_ok, "user_rows": user_rows, "error": db_error},
    }


@app.get("/api/credits")
async def credits(user_id: str = Depends(current_user)):
    return {"credits": await db.get_credits(user_id), "credit_cost": config.CREDIT_COST}


@app.get("/api/limits")
async def limits():
    """
    What the submit form needs to know before it sends anything. No auth: none
    of it is sensitive, and the form wants it before the user has done anything.
    """
    return {
        "max_photos": config.MAX_UPLOAD_IMAGES,
        "max_upload_mb": config.MAX_UPLOAD_MB,
        "upload_max_edge": config.UPLOAD_MAX_EDGE,
        "credit_cost": config.CREDIT_COST,
        "ask_limit": config.ASK_LIMIT_PER_SCAN,
        "render_limit": config.RENDER_LIMIT_PER_SCAN,
        "goals": [{"key": k, "label": v[0]} for k, v in GOALS.items()],
        "default_goal": DEFAULT_GOAL,
        "styles": [{"key": k, "label": v[0]} for k, v in makeovers.STYLES.items()],
        "default_style": makeovers.DEFAULT_STYLE,
        "modes": [{"key": k, "label": v} for k, v in makeovers.MODES.items()],
    }


@app.get("/api/scans", response_model=Dict[str, Any])
async def list_scans(
    user_id: str = Depends(current_user),
    limit: int = Query(50, ge=1, le=200),
):
    rows = await db.list_scans(user_id, limit)
    return {
        "scans": [_summary(r).model_dump() for r in rows],
        "credits": await db.get_credits(user_id),
        "credit_cost": config.CREDIT_COST,
    }


class Submission(BaseModel):
    address: str
    images: List[Any] = []
    goal: str = DEFAULT_GOAL
    style: str = makeovers.DEFAULT_STYLE

    model_config = {"arbitrary_types_allowed": True}


def _option(raw, allowed, default: str) -> str:
    """Unknown or missing options fall back to the default rather than 422 —
    an older form that never sent them must keep working."""
    value = str(raw or "").strip().lower()
    return value if value in allowed else default


async def _read_submission(request: Request) -> Submission:
    """
    Pull the address and any attachments out of the request.

    Accepts multipart (address + images) and plain JSON ({"address": ...}).

    The JSON form exists because the website and this service deploy
    independently — Vercel and Railway are never in step — so there is always a
    window where one is newer than the other. Reading both shapes means that
    window is uneventful instead of a stream of 422s on the live site. It costs
    a dozen lines; a deploy-order dependency between two separately hosted
    services costs an outage every time.
    """
    content_type = request.headers.get("content-type", "").lower()

    if content_type.startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Could not read the request body.")
        if not isinstance(body, dict):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Expected an object with an 'address'.")
        return Submission(
            address=str(body.get("address") or ""),
            goal=_option(body.get("goal"), GOALS, DEFAULT_GOAL),
            style=_option(body.get("style"), makeovers.STYLES, makeovers.DEFAULT_STYLE),
        )

    try:
        # max_files sits above the photo limit so the friendlier "you attached
        # 61, the limit is 50" message is the one users see.
        form = await request.form(max_files=config.MAX_UPLOAD_IMAGES + 20)
    except Exception:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Could not read the submitted form.")

    # getlist so one field name can carry several files; plain text fields
    # sharing the name are not uploads and are skipped.
    files = [f for f in form.getlist("images") if isinstance(f, UploadFile)]
    return Submission(
        address=str(form.get("address") or ""),
        images=files,
        goal=_option(form.get("goal"), GOALS, DEFAULT_GOAL),
        style=_option(form.get("style"), makeovers.STYLES, makeovers.DEFAULT_STYLE),
    )


@app.post("/api/scans", response_model=SubmitResponse, status_code=status.HTTP_201_CREATED,
          openapi_extra={
              "requestBody": {
                  "required": True,
                  "content": {
                      "multipart/form-data": {
                          "schema": {
                              "type": "object",
                              "required": ["address"],
                              "properties": {
                                  "address": {"type": "string"},
                                  "goal": {"type": "string",
                                           "enum": list(GOALS)},
                                  "style": {"type": "string",
                                            "enum": list(makeovers.STYLES)},
                                  "images": {
                                      "type": "array",
                                      "items": {"type": "string", "format": "binary"},
                                  },
                              },
                          }
                      },
                      "application/json": {
                          "schema": {
                              "type": "object",
                              "required": ["address"],
                              "properties": {"address": {"type": "string"}},
                          }
                      },
                  },
              }
          })
async def submit_scan(request: Request, user_id: str = Depends(current_user)):
    """
    Queue a scan, optionally with the owner's own photographs.

    Multipart keeps the address and its attachments in one request — one
    charge, and no half-created scan waiting on files that never arrive. A
    JSON body without attachments is also accepted; see `_read_submission`.
    """
    submission = await _read_submission(request)
    address = clean_address(submission.address)

    # Attachments are validated and re-encoded BEFORE the credit is taken, so a
    # rejected photo costs nothing and leaves nothing on the volume.
    try:
        attachments = await uploads.collect(submission.images)
    except uploads.UploadRejected as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))

    # The check, the charge and the row all happen in one transaction — see
    # db.begin_scan. Nothing is queued unless the credit was actually taken.
    try:
        scan_id, remaining = await db.begin_scan(
            user_id, address, config.CREDIT_COST, owner_photos=len(attachments),
            goal=submission.goal, style=submission.style,
        )
    except db.ScanAlreadyRunning:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "You already have a scan running. Wait for it to finish before starting another.",
        )
    except db.InsufficientCredits:
        raise HTTPException(
            status.HTTP_402_PAYMENT_REQUIRED,
            f"This scan costs {config.CREDIT_COST} credit. You don't have enough.",
        )

    # Past the charge, so any failure here has to hand the credit back itself —
    # the worker that normally does that has not been started yet.
    try:
        names = uploads.write(attachments, storage.scan_dir(scan_id))
    except Exception as exc:
        log.exception("Could not store attachments for scan %s: %s", scan_id, exc)
        await db.mark_failed(scan_id, "Could not store the attached photos. "
                                      "Your credit has been refunded.")
        await db.refund_credits(scan_id, user_id, config.CREDIT_COST)
        storage.delete_scan_files(scan_id)
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            "Could not store the attached photos. No credit was charged.")

    worker.submit(scan_id, user_id, address, names, submission.goal, submission.style)

    row = await db.get_scan(scan_id, user_id)
    return SubmitResponse(scan=_summary(row), credits_remaining=remaining)


@app.get("/api/scans/{scan_id}", response_model=ScanDetail)
async def get_scan(scan_id: str, user_id: str = Depends(current_user)):
    row = await db.get_scan(scan_id, user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scan not found.")
    return await _detail(row)


@app.delete("/api/scans/{scan_id}")
async def delete_scan(scan_id: str, user_id: str = Depends(current_user)):
    row = await db.get_scan(scan_id, user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scan not found.")
    if row["status"] in db.ACTIVE_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This scan is still running. Wait for it to finish.")
    await db.delete_scan(scan_id, user_id)
    storage.delete_scan_files(scan_id)
    return {"deleted": True, "id": scan_id}


@app.get("/api/scans/{scan_id}/files/{filename}")
async def get_file(scan_id: str, filename: str, expires: int = Query(...),
                   signature: str = Query(...)):
    """
    Signed artifact download. No bearer token — the signature *is* the
    authorisation, because <img> tags and download links cannot send headers.
    """
    if not storage.verify(scan_id, filename, expires, signature):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Link expired or invalid.")

    path = storage.resolve(scan_id, filename)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "File not found.")

    is_pdf = path.suffix.lower() == ".pdf"
    return FileResponse(
        path,
        media_type="application/pdf" if is_pdf else "image/jpeg",
        # Links are already scoped by the signature's TTL, so let the browser
        # cache aggressively — the report view loads a dozen of these.
        headers={"Cache-Control": "private, max-age=3600"},
        filename=f"property-report-{scan_id[:8]}.pdf" if is_pdf else None,
    )


# ── After the scan: ask, and the studio ─────────────────────────────────────
# Both run a model call inside the request — 5-40 seconds — rather than through
# the job queue: they are short enough to wait for, and the user is watching.
#
# They run on their own thread pool, not the default one the scans use, so a
# burst of questions can never leave a queued scan waiting for a thread while
# its clock runs. Service-wide semaphores bound how many run at once, and a
# slot is only released when the work has actually finished: if the request
# gives up first, the thread is still billing Gemini and still counts.
_ask_slots = asyncio.Semaphore(config.MAX_CONCURRENT_ASKS)
_render_slots = asyncio.Semaphore(config.MAX_CONCURRENT_RENDERS)
_interactive_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=config.MAX_CONCURRENT_ASKS + config.MAX_CONCURRENT_RENDERS,
    thread_name_prefix="interactive",
)
_client = None

# A user is waiting on these, so a single hung call is cut off sooner than in
# a scan (where waiting out a slow call beats losing the stage).
INTERACTIVE_TIMEOUT_SECONDS = 75


def _gemini():
    global _client
    if _client is None:
        _client = make_client(scanner_config.GEMINI_API_KEY, INTERACTIVE_TIMEOUT_SECONDS)
    return _client


async def _run_held(slots: asyncio.Semaphore, fn, timeout: float):
    """
    Run `fn` on the interactive pool under `slots`, waiting at most `timeout`.

    On timeout the caller gets asyncio.TimeoutError, but the slot stays taken
    until the thread really finishes — otherwise the concurrency cap would
    only bound the requests, not the work.
    """
    await slots.acquire()
    loop = asyncio.get_running_loop()
    try:
        fut = loop.run_in_executor(_interactive_pool, fn)
    except Exception:
        slots.release()
        raise
    fut.add_done_callback(lambda _: slots.release())
    return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)


async def _completed_scan(scan_id: str, user_id: str) -> dict:
    row = await db.get_scan(scan_id, user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scan not found.")
    if row["status"] != db.COMPLETED:
        raise HTTPException(status.HTTP_409_CONFLICT, "This scan has not finished yet.")
    return _result(row) or {}


def _question_out(row) -> dict:
    return {
        "id": row["id"],
        "question": row["question"],
        "status": row["status"],
        "answer": _json(row["answer_json"]),
        "error_message": row["error_message"],
        "created_at": _iso(row["created_at"]),
    }


class AskBody(BaseModel):
    question: str


@app.get("/api/scans/{scan_id}/questions")
async def list_questions(scan_id: str, user_id: str = Depends(current_user)):
    await _completed_scan(scan_id, user_id)
    rows = await db.list_questions(scan_id, user_id)
    return {
        "questions": [_question_out(r) for r in rows],
        "limit": config.ASK_LIMIT_PER_SCAN,
        "used": sum(1 for r in rows if r["status"] != db.FAILED),
    }


@app.post("/api/scans/{scan_id}/ask")
async def ask_scan(scan_id: str, body: AskBody, user_id: str = Depends(current_user)):
    question = " ".join((body.question or "").split())
    if len(question) < 3:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Ask a question first.")
    if len(question) > 500:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Keep questions under 500 characters.")

    payload = await _completed_scan(scan_id, user_id)
    try:
        question_id = await db.begin_question(scan_id, user_id, question,
                                              config.ASK_LIMIT_PER_SCAN)
    except db.ScanNotReady:
        raise HTTPException(status.HTTP_409_CONFLICT, "This scan has not finished yet.")
    except db.LimitReached:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"You have used all {config.ASK_LIMIT_PER_SCAN} questions included with this scan.",
        )

    history = [
        {"question": r["question"], "answer": (_json(r["answer_json"]) or {}).get("answer", "")}
        for r in await db.list_questions(scan_id, user_id)
        if r["status"] == db.COMPLETED
    ]

    scan_dir = storage.scan_dir(scan_id)

    def work():
        images = ask_stage.load_images(scan_dir, payload)
        return ask_stage.answer(_gemini(), question, payload, images, history)

    try:
        answer = await _run_held(_ask_slots, work, timeout=150)
    except Exception as exc:  # noqa: BLE001
        log.warning("Question on scan %s failed: %s", scan_id, exc)
        await db.fail_question(question_id, "Could not answer just now.")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "Couldn't answer that just now. It didn't count against your "
                            "questions — try again in a moment.")

    await db.finish_question(question_id, answer)
    return {"id": question_id, "question": question, "status": db.COMPLETED,
            "answer": answer, "error_message": None, "created_at": None}


class RenderBody(BaseModel):
    source: str
    mode: str = "restyle"
    style: Optional[str] = None
    instruction: Optional[str] = None


def _render_out(row) -> dict:
    return {
        "id": row["id"],
        "source": row["source"],
        "mode": row["mode"],
        "style": row["style"],
        "style_name": makeovers.style_name(row["style"]) if row["style"] else None,
        "instruction": row["instruction"],
        "status": row["status"],
        "url": storage.sign(row["scan_id"], row["file"]) if row["file"] else None,
        "error_message": row["error_message"],
        "created_at": _iso(row["created_at"]),
    }


@app.get("/api/scans/{scan_id}/renders")
async def list_renders(scan_id: str, user_id: str = Depends(current_user)):
    await _completed_scan(scan_id, user_id)
    rows = await db.list_renders(scan_id, user_id)
    return {
        "renders": [_render_out(r) for r in rows],
        "limit": config.RENDER_LIMIT_PER_SCAN,
        "used": sum(1 for r in rows if r["status"] != db.FAILED),
    }


def _photo_meta(payload: dict, label: str) -> Optional[dict]:
    """What the studio needs to know about one image: whether it is outside,
    what room it shows, and what the inspection found in it. None for anything
    a render cannot be made from, which includes the overhead satellite plate."""
    if label.startswith("satellite"):
        return None
    for p in payload.get("photos") or []:
        if p["label"] == label:
            space = next((sp["name"] for sp in (payload.get("inspection_report") or {})
                          .get("spaces", []) if label in sp["photos"]), p["space_label"])
            return {"is_exterior": p["is_exterior"], "space": space,
                    "defects": [f["title"] for f in p["findings"] if not f.get("dismissed")]}
    if label in (payload.get("imagery") or {}) and not label.startswith("satellite"):
        # Version-1 scans: no per-photo record, so judge by where it came from.
        outside = not label.startswith("owner_photo")
        return {"is_exterior": outside, "space": "exterior" if outside else "room",
                "defects": []}
    return None


@app.post("/api/scans/{scan_id}/renders")
async def create_render(scan_id: str, body: RenderBody, user_id: str = Depends(current_user)):
    payload = await _completed_scan(scan_id, user_id)

    mode = (body.mode or "").strip().lower()
    if mode not in makeovers.MODES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown render mode.")
    style = (body.style or "").strip().lower() or payload.get("style") or makeovers.DEFAULT_STYLE
    if style not in makeovers.STYLES:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Unknown style.")
    instruction = " ".join((body.instruction or "").split())
    if mode == "custom" and not 4 <= len(instruction) <= 300:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Describe the change in 4 to 300 characters.")

    meta = _photo_meta(payload, body.source)
    if meta is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "That photo isn't part of this scan.")

    try:
        render_id = await db.begin_render(
            scan_id, user_id, body.source, mode,
            style if mode in ("restyle", "stage") else None,
            instruction if mode == "custom" else None,
            config.RENDER_LIMIT_PER_SCAN,
        )
    except db.ScanNotReady:
        raise HTTPException(status.HTTP_409_CONFLICT, "This scan has not finished yet.")
    except db.LimitReached:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"You have used all {config.RENDER_LIMIT_PER_SCAN} studio renders included "
            "with this scan.",
        )

    text = makeovers.instruction(mode, meta["space"], meta["is_exterior"], style,
                                 meta["defects"], instruction)
    scan_dir = storage.scan_dir(scan_id)
    name = f"studio_{render_id}.jpg"

    def work():
        source = ask_stage.image_for(scan_dir, payload, body.source)
        if source is None:
            raise RuntimeError("source image missing")
        image = makeovers.render(_gemini(), source, text, f"studio {mode}")
        if image is None:
            raise RuntimeError("no image returned")
        image.save(scan_dir / name, "JPEG", quality=92)

    try:
        await _run_held(_render_slots, work, timeout=180)
    except Exception as exc:  # noqa: BLE001
        log.warning("Studio render on scan %s failed: %s", scan_id, exc)
        await db.fail_render(render_id, "The image model did not return a render.")
        raise HTTPException(status.HTTP_502_BAD_GATEWAY,
                            "That render didn't come back. It didn't count against your "
                            "allowance — try again, or try another photo.")

    await db.finish_render(render_id, name)
    row = await db.get_render(render_id, user_id)
    if row is None:
        # The scan was deleted while the render was being made.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scan not found.")
    return _render_out(row)
