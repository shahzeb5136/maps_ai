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
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator

from scanner import config as scanner_config

from . import config, db, storage, worker
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
class ScanRequest(BaseModel):
    address: str = Field(min_length=3, max_length=300)

    @field_validator("address")
    @classmethod
    def _clean(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("Address cannot be empty")
        return v


class ScanSummary(BaseModel):
    id: str
    address: str
    status: str
    stage: Optional[str] = None
    stage_detail: Optional[str] = None
    credits_spent: int
    error_message: Optional[str] = None
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


class ScanDetail(ScanSummary):
    result: Optional[Dict[str, Any]] = None


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
        error_message=row["error_message"],
        created_at=_iso(row["created_at"]) or "",
        started_at=_iso(row["started_at"]),
        completed_at=_iso(row["completed_at"]),
    )


def _detail(row) -> ScanDetail:
    import json

    result = row["result_json"] if "result_json" in row.keys() else None
    if isinstance(result, str):          # asyncpg hands back jsonb as text
        result = json.loads(result)
    if result:
        result = storage.sign_payload(row["id"], result)
    return ScanDetail(**_summary(row).model_dump(), result=result)


# ── Routes ───────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "property-scanner",
        "credit_cost": config.CREDIT_COST,
        "storage_writable": os.access(config.STORAGE_DIR, os.W_OK),
    }


@app.get("/api/credits")
async def credits(user_id: str = Depends(current_user)):
    return {"credits": await db.get_credits(user_id), "credit_cost": config.CREDIT_COST}


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


@app.post("/api/scans", response_model=SubmitResponse, status_code=status.HTTP_201_CREATED)
async def submit_scan(body: ScanRequest, user_id: str = Depends(current_user)):
    # The check, the charge and the row all happen in one transaction — see
    # db.begin_scan. Nothing is queued unless the credit was actually taken.
    try:
        scan_id, remaining = await db.begin_scan(user_id, body.address, config.CREDIT_COST)
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

    worker.submit(scan_id, user_id, body.address)

    row = await db.get_scan(scan_id, user_id)
    return SubmitResponse(scan=_summary(row), credits_remaining=remaining)


@app.get("/api/scans/{scan_id}", response_model=ScanDetail)
async def get_scan(scan_id: str, user_id: str = Depends(current_user)):
    row = await db.get_scan(scan_id, user_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scan not found.")
    return _detail(row)


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
