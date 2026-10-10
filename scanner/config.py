"""
Scanner configuration.

Everything the pipeline needs comes from the environment so the same code runs
locally (`.env`) and on Railway (service variables). Nothing here is a secret
literal — the keys that used to be hardcoded now live in MAPS_API_KEY and
GEMINI_API_KEY.
"""

import os
from typing import List, Tuple


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _flag(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


# ── Credentials ──────────────────────────────────────────────────────────────
MAPS_API_KEY: str = _env("MAPS_API_KEY") or _env("GOOGLE_MAPS_API_KEY")
GEMINI_API_KEY: str = _env("GEMINI_API_KEY") or _env("GOOGLE_API_KEY")

# ── Models ───────────────────────────────────────────────────────────────────
TEXT_MODEL: str = _env("SCANNER_TEXT_MODEL", "gemini-3.7-flash")
IMAGE_MODEL: str = _env("SCANNER_IMAGE_MODEL", "gemini-3.1-flash-image")

# ── Pipeline switches ────────────────────────────────────────────────────────
ENABLE_TEMPORAL: bool = _flag("SCANNER_ENABLE_TEMPORAL", True)
ENABLE_RENOVATION: bool = _flag("SCANNER_ENABLE_RENOVATION", True)
MAX_HISTORICAL_PANOS: int = int(_env("SCANNER_MAX_PANOS", "4"))

# Three concepts, generated in ONE text call, rendered in parallel. The tiers
# escalate by how much of the envelope is touched — not by price, which a
# handful of photographs cannot tell anyone.
RENOVATION_TIERS: List[Tuple[str, str]] = [
    ("Cosmetic Refresh", "Lightest touch, quickest wins: paint, lighting, door, "
                         "landscaping, cleaning. No material replacement, no structural work."),
    ("Signature Transformation", "Mid-tier repositioning: replace key facade materials, "
                                 "upgrade windows/doors, add architectural accents and hardscaping."),
    ("Full Repositioning", "Premium rebuild of the exterior envelope: high-end cladding, "
                           "feature glazing, integrated lighting design, full landscape redesign."),
]

# Wall-clock ceiling for a single scan, enforced by the API worker. A 50-photo
# inspection is a few hundred model calls' worth of work, so this is generous.
SCAN_TIMEOUT_SECONDS: int = int(_env("SCANNER_TIMEOUT_SECONDS", "1200"))

# ── Photo inspection ─────────────────────────────────────────────────────────
# Every photograph is inspected on its own call, because bounding boxes are
# only tight when the model is looking at one picture. This many run at once;
# it is really a rate limit on the Gemini quota.
VISION_CONCURRENCY: int = max(1, int(_env("SCANNER_VISION_CONCURRENCY", "6")))

# Per-request ceiling on any single Gemini call, seconds. Retries sit on top.
GEMINI_TIMEOUT_SECONDS: int = int(_env("SCANNER_GEMINI_TIMEOUT_SECONDS", "180"))
GEMINI_ATTEMPTS: int = max(1, int(_env("SCANNER_GEMINI_ATTEMPTS", "3")))

# ── Optional stages ──────────────────────────────────────────────────────────
# Google Solar API: real roof geometry and a panel layout. Needs "Solar API"
# enabled on the Maps key's project; without it the stage logs once and skips.
ENABLE_SOLAR: bool = _flag("SCANNER_ENABLE_SOLAR", True)
# Interior redesign renders made during the scan (on-demand ones are separate).
ENABLE_MAKEOVERS: bool = _flag("SCANNER_ENABLE_MAKEOVERS", True)
MAKEOVER_COUNT: int = max(0, int(_env("SCANNER_MAKEOVER_COUNT", "3")))
ENABLE_LISTING: bool = _flag("SCANNER_ENABLE_LISTING", True)


def require_keys() -> None:
    """Fail loudly at startup rather than three minutes into a paid scan."""
    missing = [
        name
        for name, value in (("MAPS_API_KEY", MAPS_API_KEY), ("GEMINI_API_KEY", GEMINI_API_KEY))
        if not value
    ]
    if missing:
        raise RuntimeError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Set them in Railway → service → Variables (or in a local .env file)."
        )
