"""
Property Scanner pipeline.

Pure analysis code — no web framework, no database. `run_scan()` takes an
address and an output directory, writes its imagery there, and hands back a
JSON-serialisable payload describing everything it found.

The API layer in `api/` owns credits, auth and storage; this package owns
Google Maps, Gemini and the report.
"""

from .pipeline import ScanFailed, run_scan

__all__ = ["run_scan", "ScanFailed"]
