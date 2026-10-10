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

from scanner import ScanFailed, ScanOptions, run_scan  # noqa: E402
from scanner.geo import sanitize_folder_name  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan a property from public imagery.")
    parser.add_argument("address", help="Street address or place name")
    parser.add_argument("--out", default="./property_scans",
                        help="Output directory (default: ./property_scans)")
    parser.add_argument("--photo", action="append", default=[],
                        help="A photo of the property; repeat for several")
    parser.add_argument("--goal", default="buying",
                        choices=["buying", "selling", "owning", "renovating"])
    parser.add_argument("--style", default="modern_organic", help="Makeover style key")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    out_dir = Path(args.out) / sanitize_folder_name(args.address)
    print(f"\n=== SCANNING: {args.address} ===\n")

    def progress(stage: str, detail: str) -> None:
        print(f"[{stage:<10}] {detail}")

    # Photos go through the same normalisation the API applies on upload.
    names = []
    if args.photo:
        from api.uploads import _normalise
        out_dir.mkdir(parents=True, exist_ok=True)
        for i, path in enumerate(args.photo, start=1):
            photo, thumb = _normalise(Path(path).read_bytes(), path)
            (out_dir / f"owner_photo_{i}.jpg").write_bytes(photo)
            (out_dir / f"thumb_owner_photo_{i}.jpg").write_bytes(thumb)
            names.append(f"owner_photo_{i}.jpg")

    try:
        payload = run_scan(args.address, out_dir, progress, names,
                           ScanOptions(goal=args.goal, style=args.style))
    except ScanFailed as exc:
        print(f"\nScan failed: {exc}", file=sys.stderr)
        return 1

    health = payload.get("health") or {}
    print(f"\nHealth: {health.get('score')}/100 ({health.get('grade')}), "
          f"{health.get('systems_assessed')} of {health.get('systems_total')} systems assessed")
    for issue in (payload.get("inspection_report") or {}).get("issues", [])[:10]:
        print(f"  {issue['id']} [{issue['severity']:<8}] {issue['title']} -> {issue['trade']}")

    inspection = payload.get("inspection")
    if inspection:
        print(f"\nExterior grade: {inspection['architectural_profile']['overall_property_grade']} "
              f"(imagery confidence: {inspection['imagery_confidence']})")

    reno = payload.get("renovation")
    if reno:
        print(f"\nConcepts (from {reno.get('before_source') or 'the facade image'}):")
        for v in reno["variants"]:
            print(f"  - {v['concept_name']} ({v['tier']}): "
                  f"{v.get('overall_effort', '?')} effort, "
                  f"{len(v.get('scope_items') or [])} scope items")
        print(f"  recommended: {reno['recommended_concept_name']}")

    print(f"\nDone -> {out_dir.resolve()}")
    if payload.get("pdf"):
        print(f"PDF:  {(out_dir / payload['pdf']).resolve()}")
    print(f"JSON: {(out_dir / 'report.json').resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
