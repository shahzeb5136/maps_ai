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
# Session tokens are verified against Clerk's public JWKS. Each issuer is the
# `iss` claim of a real token — Clerk → Configure → API keys → "Frontend API
# URL", which is also recoverable by base64-decoding a pk_ publishable key.
#
# Comma-separated, because one deployment of this service serves more than one
# Clerk instance: the live site runs on a production instance
# (https://clerk.your-domain.com) while local development runs on a development
# one (https://something-00.clerk.accounts.dev). A token is accepted only if
# its issuer is on this list AND its signature checks out against that
# issuer's own key set.
CLERK_ISSUERS: List[str] = [
    i.strip().rstrip("/") for i in _env("CLERK_ISSUER").split(",") if i.strip()
]


def jwks_url_for(issuer: str) -> str:
    """Where a given issuer publishes its keys. CLERK_JWKS_URL overrides this,
    but only makes sense when a single issuer is configured."""
    override = _env("CLERK_JWKS_URL")
    if override and len(CLERK_ISSUERS) == 1:
        return override
    return f"{issuer}/.well-known/jwks.json"

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

# ── Owner-supplied photos ────────────────────────────────────────────────────
# Attachments are re-encoded to JPEG before storage, so these bound the *input*.
# UPLOAD_MAX_EDGE also bounds what the vision model is billed for: a 4032px
# phone photo costs the same to analyse as a 1600px one, and reads no better.
MAX_UPLOAD_IMAGES: int = int(_env("MAX_UPLOAD_IMAGES", "6"))
MAX_UPLOAD_MB: float = float(_env("MAX_UPLOAD_MB", "12"))
UPLOAD_MAX_EDGE: int = int(_env("UPLOAD_MAX_EDGE", "1600"))

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
            ("CLERK_ISSUER", CLERK_ISSUERS),
            ("SIGNING_SECRET", SIGNING_SECRET),
        ) if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing required environment variable(s): " + ", ".join(missing)
        )
