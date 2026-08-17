"""
Service configuration.

Every value here is a Railway service variable. The scanner's own keys live in
`scanner/config.py`; this module covers auth, storage, the database and CORS.
"""

import os
from pathlib import Path
from typing import List


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


# ── Database ─────────────────────────────────────────────────────────────────
# The same Postgres the website uses. `users` already exists and is owned by the
# Next.js app; this service only ever decrements `users.credits` and owns its
# own `property_scans` table.
DATABASE_URL: str = _env("DATABASE_URL")

# ── Clerk ────────────────────────────────────────────────────────────────────
# Session tokens are verified against Clerk's public JWKS. CLERK_ISSUER is the
# `iss` claim of a real token, e.g. https://clerk.your-domain.com — find it in
# Clerk → Configure → API keys → "Frontend API URL".
CLERK_ISSUER: str = _env("CLERK_ISSUER").rstrip("/")
CLERK_JWKS_URL: str = _env("CLERK_JWKS_URL") or (
    f"{CLERK_ISSUER}/.well-known/jwks.json" if CLERK_ISSUER else ""
)

# ── Storage ──────────────────────────────────────────────────────────────────
# Point this at the Railway volume mount path. Scans are large (5-10 images
# each) and must survive redeploys, so the container filesystem is not enough.
STORAGE_DIR: Path = Path(_env("STORAGE_DIR", "./data")).resolve()
SCANS_DIR: Path = STORAGE_DIR / "scans"

# ── Signed asset URLs ────────────────────────────────────────────────────────
# Images are referenced by <img src> and the PDF by a plain link, so neither can
# carry an Authorization header. Instead the API hands out URLs signed with this
# secret. Any long random string; changing it invalidates outstanding links.
SIGNING_SECRET: str = _env("SIGNING_SECRET") or _env("SECRET_KEY")
SIGNED_URL_TTL_SECONDS: int = int(_env("SIGNED_URL_TTL_SECONDS", str(24 * 3600)))

# Absolute base for the URLs handed to the browser. Railway sets
# RAILWAY_PUBLIC_DOMAIN automatically, so this usually needs no configuration.
_railway_domain = _env("RAILWAY_PUBLIC_DOMAIN")
PUBLIC_BASE_URL: str = (
    _env("PUBLIC_BASE_URL")
    or (f"https://{_railway_domain}" if _railway_domain else "")
).rstrip("/")

# ── CORS ─────────────────────────────────────────────────────────────────────
ALLOWED_ORIGINS: List[str] = [
    o.strip().rstrip("/")
    for o in _env("ALLOWED_ORIGINS", "http://localhost:3000").split(",")
    if o.strip()
]

# ── Billing ──────────────────────────────────────────────────────────────────
CREDIT_COST: int = int(_env("SCAN_CREDIT_COST", "1"))

# How many scans may run at once. Each one holds a thread and makes ~10
# outbound calls, so this is really a rate limit on the Gemini quota.
MAX_CONCURRENT_SCANS: int = int(_env("MAX_CONCURRENT_SCANS", "2"))

PORT: int = int(_env("PORT", "8000"))


def validate() -> None:
    """Startup check — a misconfigured service should fail immediately, not on
    the first user request."""
    missing = [
        name for name, value in (
            ("DATABASE_URL", DATABASE_URL),
            ("CLERK_JWKS_URL (or CLERK_ISSUER)", CLERK_JWKS_URL),
            ("SIGNING_SECRET", SIGNING_SECRET),
        ) if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): " + ", ".join(missing)
        )
