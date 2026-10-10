"""Test configuration: fake keys before any module reads the environment."""

import os
import sys
from pathlib import Path

os.environ.setdefault("MAPS_API_KEY", "test-maps-key")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ.setdefault("CLERK_ISSUER", "https://clerk.test")
os.environ.setdefault("SIGNING_SECRET", "test-signing-secret")
os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", "postgresql://x@localhost/x"))

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from scanner import gemini  # noqa: E402


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """Retries are tested, not waited for."""
    monkeypatch.setattr(gemini, "_sleep", lambda attempt: None)
