"""
Scan artifacts on the Railway volume, plus the signed URLs that expose them.

Why signed URLs rather than an authenticated endpoint: the browser renders
these through `<img src>` and a plain download link, neither of which can carry
an Authorization header. A short-lived HMAC in the query string gives the same
guarantee — a link only works if this service minted it, and only until it
expires.
"""

import hashlib
import hmac
import re
import shutil
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from . import config

# Deliberately strict: these names only ever come from our own pipeline.
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def scan_dir(scan_id: str) -> Path:
    return config.SCANS_DIR / scan_id


def ensure_dirs() -> None:
    config.SCANS_DIR.mkdir(parents=True, exist_ok=True)


def delete_scan_files(scan_id: str) -> None:
    shutil.rmtree(scan_dir(scan_id), ignore_errors=True)


def resolve(scan_id: str, filename: str) -> Optional[Path]:
    """
    Map (scan_id, filename) to a real file, or None.

    Both components are validated against SAFE_NAME and the result is checked to
    be inside the scan directory, so `..` and absolute paths cannot escape the
    volume even if the signature were somehow forged.
    """
    if not SAFE_NAME.match(scan_id) or not SAFE_NAME.match(filename):
        return None

    base = scan_dir(scan_id).resolve()
    target = (base / filename).resolve()
    if base != target.parent or not target.is_file():
        return None
    return target


# ── Signing ──────────────────────────────────────────────────────────────────
def _signature(scan_id: str, filename: str, expires: int) -> str:
    message = f"{scan_id}/{filename}/{expires}".encode()
    digest = hmac.new(config.SIGNING_SECRET.encode(), message, hashlib.sha256)
    return digest.hexdigest()[:32]


def sign(scan_id: str, filename: str, ttl: Optional[int] = None) -> str:
    """Absolute, signed, time-limited URL for one artifact."""
    expires = int(time.time()) + (ttl or config.SIGNED_URL_TTL_SECONDS)
    sig = _signature(scan_id, filename, expires)
    path = (
        f"/api/scans/{quote(scan_id)}/files/{quote(filename)}"
        f"?expires={expires}&signature={sig}"
    )
    return f"{config.PUBLIC_BASE_URL}{path}" if config.PUBLIC_BASE_URL else path


def verify(scan_id: str, filename: str, expires: int, signature: str) -> bool:
    if expires < int(time.time()):
        return False
    return hmac.compare_digest(_signature(scan_id, filename, expires), signature)


def sign_payload(scan_id: str, payload: dict) -> dict:
    """
    Return a copy of a scan payload with every filename replaced by a signed URL.

    The stored `result_json` keeps bare filenames — signatures expire, so baking
    them into the row would mean re-writing it on every read.
    """
    signed = dict(payload)

    signed["imagery"] = {
        name: sign(scan_id, filename)
        for name, filename in (payload.get("imagery") or {}).items()
    }
    signed["timeline"] = [
        {**frame, "url": sign(scan_id, frame["file"])}
        for frame in (payload.get("timeline") or [])
        if frame.get("file")
    ]

    reno = payload.get("renovation")
    if reno:
        signed["renovation"] = {
            **reno,
            "before_url": sign(scan_id, reno["before"]) if reno.get("before") else None,
            "variants": [
                {**v, "after_url": sign(scan_id, v["after"]) if v.get("after") else None}
                for v in (reno.get("variants") or [])
            ],
        }

    signed["pdf_url"] = sign(scan_id, payload["pdf"]) if payload.get("pdf") else None
    return signed
