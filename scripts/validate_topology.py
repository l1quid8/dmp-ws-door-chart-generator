"""Riser-topology extraction validation harness.

Runs the riser edge extraction against a set of design PDFs, prints the derived
wiring, and pass/fails any fixture that has a recorded ground-truth edge set.

Usage:
    python validate_topology.py [extra_design.pdf ...]

Built-in fixtures are the corpus risers (Academy / Darby / O'Melveny). Extra
PDFs given on the command line are extracted and printed for manual review.
As more samples arrive, add them to FIXTURES with their ground-truth edges so
the harness keeps regression-checking them.
"""
from __future__ import annotations

import sys
import io
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from extract_topology import (extract_spans, merge_multiline_locations,
    cluster_devices, extract_line_segments, compute_device_footprints,
    reconstruct_edges)
from generate_dmp_ws import _detect_riser_page

_UPLOADS = r"C:\Users\tcald\.claude\uploads\58f04bd6-7966-4d8d-bd8e-10965f773ebe"
_INPUT = (r"C:\Users\tcald\OneDrive - ConvergeOne\Documents"
          r"\DMP Installation Worksheets\Generated DMP Worksheets\input")


def _norm(s: str) -> str:
    return s.replace(" ", "").upper()


# Each fixture: (name, pdf_path, ground_truth_edges or None).
# Ground-truth edges are (src, dst) device-id pairs read directly off the riser.
FIXTURES = [
    ("ACADEMY", _UPLOADS + r"\e02cb6f7-Academy_Enrichment_Science_20260506.pdf", {
        ("710-KP-1", "KEYPAD2"), ("710-KP-1", "KEYPAD3"), ("710-KP-1", "710-KP-2"),
        ("710-KP-2", "KEYPAD4"), ("710-KP-2", "KEYPAD5"),
        ("710-LX500-1", "RSP1"), ("710-LX500-1", "710-LX500-2"),
        ("710-LX500-2", "RSP2"), ("710-LX500-2", "RSP3"),
    }),
    ("DARBY", _UPLOADS + r"\daf52199-Darby_Ave_EL__3340_20260518_1779086462.pdf", None),
    ("OMELVENY", _INPUT + r"\O'MELVENY ES INTRUSION DESIGN 5-04-26.pdf", None),
]


def extract(pdf: str, page: int | None = None):
    pdf_p = Path(pdf)
    if page is None:
        page = _detect_riser_page(pdf_p)
    spans = merge_multiline_locations(extract_spans(pdf_p, page))
    devices = cluster_devices(spans)
    segs = extract_line_segments(pdf_p, page)
    footprints, son = compute_device_footprints(devices, segs, pdf_p, page)
    edges = reconstruct_edges(segs, devices, spans, footprints=footprints)
    return devices, edges, son, page


def run_one(name: str, pdf: str, truth: set | None):
    devices, edges, son, page = extract(pdf)
    print(f"\n{'=' * 66}\n{name}  (riser page {page + 1})\n{'=' * 66}")
    print(f"  devices={len(devices)}  splitter-on-RSP={son}")
    got = set()
    for e in edges:
        got.add((_norm(e.src.id), _norm(e.dst.id)))
        print(f"   {e.src.id:<14} -> {e.dst.id}")
    if truth is None:
        print("  (no ground truth — manual review)")
        return None
    tn = {(_norm(a), _norm(b)) for a, b in truth}
    hit = tn & got
    missed = tn - got
    ok = not missed
    print(f"  GROUND TRUTH: {len(hit)}/{len(tn)} edges found — "
          f"{'PASS' if ok else 'FAIL'}")
    if missed:
        print(f"   MISSED: {sorted(missed)}")
    return ok


def main() -> None:
    results = []
    for name, pdf, truth in FIXTURES:
        if not Path(pdf).exists():
            print(f"\n{name}: SKIP (not found: {pdf})")
            continue
        try:
            results.append((name, run_one(name, pdf, truth)))
        except Exception as e:
            print(f"\n{name}: ERROR — {e}")
            results.append((name, False))
    for extra in sys.argv[1:]:
        try:
            run_one(Path(extra).stem, extra, None)
        except Exception as e:
            print(f"\n{extra}: ERROR — {e}")
    graded = [ok for _, ok in results if ok is not None]
    print(f"\n{'=' * 66}")
    print(f"FIXTURES WITH GROUND TRUTH: {sum(1 for ok in graded if ok)}/"
          f"{len(graded)} pass")


if __name__ == "__main__":
    main()
