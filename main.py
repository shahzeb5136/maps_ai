"""
Local CLI for a single scan — the original standalone script, now a thin
wrapper over the `scanner` package.

    python main.py "Maze Tower, Dubai UAE"
    python main.py "1600 Amphitheatre Parkway, Mountain View CA" --out ./scans

Credentials come from the environment (or a local `.env`), never from this
file. See `.env.example`.

The hosted version of this lives in `api/` and is what the website calls.
"""

import argparse
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from scanner import ScanFailed, run_scan  # noqa: E402
from scanner.geo import sanitize_folder_name  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan a property from public imagery.")
    parser.add_argument("address", help="Street address or place name")
    parser.add_argument("--out", default="./property_scans",
                        help="Output directory (default: ./property_scans)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    out_dir = Path(args.out) / sanitize_folder_name(args.address)
    print(f"\n=== SCANNING: {args.address} ===\n")

    def progress(stage: str, detail: str) -> None:
        print(f"[{stage:<10}] {detail}")

    try:
        payload = run_scan(args.address, out_dir, progress)
    except ScanFailed as exc:
        print(f"\nScan failed: {exc}", file=sys.stderr)
        return 1

    inspection = payload["inspection"]
    print(f"\nGrade: {inspection['architectural_profile']['overall_property_grade']} "
          f"(imagery confidence: {inspection['imagery_confidence']})")

    reno = payload.get("renovation")
    if reno:
        for v in reno["variants"]:
            print(f"  - {v['concept_name']} ({v['tier']}): "
                  f"${v['total_estimated_cost_usd']:,} -> {v['estimated_roi_pct']}% ROI")
        print(f"  recommended: {reno['recommended_concept_name']}")

    print(f"\nDone -> {out_dir.resolve()}")
    if payload.get("pdf"):
        print(f"PDF:  {(out_dir / payload['pdf']).resolve()}")
    print(f"JSON: {(out_dir / 'report.json').resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
