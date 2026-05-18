"""
Generates a C1 door chart from a DMP worksheet (xlsx).

The DMP must have already been produced by generate_dmp_ws.py from the design PDF.
The DMP is the single source of truth for the door chart — zones, RSPs, keypads,
splitters, and site info all flow through it.

Usage:
    python scripts/generate_door_chart.py <dmp_xlsx>
        [--template <door_chart_template_blank.xlsx>]
        [--output-dir <dir>]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# Make sibling modules importable when run from project root
sys.path.insert(0, str(Path(__file__).parent))

from inject_door_chart import inject, _slugify  # noqa: E402
from parse_dmp_worksheet import parse_dmp_worksheet  # noqa: E402


from paths import resource_path, output_dir

DEFAULT_TEMPLATE = resource_path("door_chart_template_blank.xlsx")
DEFAULT_OUTPUT_DIR = output_dir()


def main():
    ap = argparse.ArgumentParser(
        description="Generate C1 door chart from a generated DMP worksheet."
    )
    ap.add_argument("dmp", help="Path to the DMP worksheet xlsx (produced by generate_dmp_ws.py).")
    ap.add_argument("--template", default=str(DEFAULT_TEMPLATE),
                    help="Path to blank door chart template.")
    ap.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR),
                    help="Output directory.")
    args = ap.parse_args()

    dmp_path = Path(args.dmp).resolve()
    if not dmp_path.exists():
        sys.exit(f"DMP worksheet not found: {dmp_path}")

    template_path = Path(args.template).resolve()
    if not template_path.exists():
        sys.exit(f"Template not found: {template_path}")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(exist_ok=True)

    print(f"[1/2] Parsing DMP worksheet: {dmp_path.name}")
    dmp = parse_dmp_worksheet(dmp_path)
    print(f"      School: {dmp.site_info.school_name or '?'}")
    print(f"      Splitters: {len(dmp.splitters)}, RSPs: {len(dmp.rsps)}, "
          f"Keypads: {len(dmp.keypads)}, Master zones: {len(dmp.master_zones)}")

    print(f"[2/2] Injecting into door chart...")
    school = dmp.site_info.school_name or "OUTPUT"
    out_name = f"{_slugify(school)}_door_chart_{date.today().isoformat()}.xlsx"
    out_path = output_dir / out_name
    inject(template_path, dmp, out_path)

    print(f"\n✓ Done. Chart written to: {out_path}")


if __name__ == "__main__":
    main()
