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

# Wall-clock ceiling for a single scan, enforced by the API worker.
SCAN_TIMEOUT_SECONDS: int = int(_env("SCANNER_TIMEOUT_SECONDS", "900"))


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
