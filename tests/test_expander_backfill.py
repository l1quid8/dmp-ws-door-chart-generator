"""Tests for backfill_missing_expander_points (parse_zone_schedule).

OCR routinely drops SPARE rows from the large-format zone schedule — they carry
the least distinguishing text (just "SPARE"), so whole runs of unused points can
vanish (Shirley RSP1 lost Z508-Z514; Toluca lost Z557/Z573). Those points are
real: DMP addressing gives expander module N a contiguous block, and the door
chart/worksheet must list every physical point. Since the dropped rows can't be
recovered from text, we reconstruct them deterministically from the module's
point count — inferred from where its paired-power-supply supervisory zones sit
(the module's last two physical points), so an 8-point module is never over-filled.

Run: pytest tests/test_expander_backfill.py
"""
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from parse_zone_schedule import ZoneRecord, backfill_missing_expander_points  # noqa: E402


def _rec(num, rsp, **kw):
    return ZoneRecord(zone=f"Z{num}", rsp=rsp, **kw)


def _nums(records, rsp=None):
    return sorted(int(r.zone[1:]) for r in records if rsp is None or r.rsp == rsp)


def test_backfills_dropped_spares_on_16_point_module():
    """Shirley RSP1: 7 motions + PS AC/BATT survived; Z508-Z514 spares dropped.
    The BATT zone at Z516 (offset 15) proves a 16-point module -> fill 508-514."""
    zones = [_rec(n, 1) for n in range(501, 508)]              # Z501-Z507 motions
    zones.append(_rec(515, 1, is_ps_ac=True))                  # PS-1 A/C LOSS
    zones.append(_rec(516, 1, is_ps_batt=True))                # PS-1 BATT. TRBL
    out, added = backfill_missing_expander_points(zones, installed_rsps={1})
    assert added == 7
    assert _nums(out, rsp=1) == list(range(501, 517))          # full 501-516 block
    spares = {int(r.zone[1:]) for r in out if r.is_spare}
    assert spares == set(range(508, 515))                      # the 7 filled slots are SPARE


def test_eight_point_module_is_not_overfilled():
    """A 714-8: BATT at offset 7 (Z508) caps the block at 8 points. A missing
    spare inside 501-508 is filled; nothing at offset 8-15 is invented."""
    zones = [_rec(n, 1) for n in (501, 502, 503, 504, 505)]    # Z506 spare dropped
    zones.append(_rec(507, 1, is_ps_ac=True))                  # AC at offset 6
    zones.append(_rec(508, 1, is_ps_batt=True))                # BATT at offset 7 -> 8 points
    out, added = backfill_missing_expander_points(zones, installed_rsps={1})
    assert added == 1
    assert _nums(out, rsp=1) == list(range(501, 509))          # 501-508 only, no 509-516
    assert {int(r.zone[1:]) for r in out if r.is_spare} == {506}


def test_uninstalled_rsp_is_left_untouched():
    """Only expanders that COMBUS LINES actually installs get backfilled; stray
    zones for a non-installed RSP number must not spawn phantom spares."""
    zones = [_rec(501, 1), _rec(515, 1, is_ps_ac=True), _rec(516, 1, is_ps_batt=True),
             _rec(517, 2)]                                     # RSP2 not installed
    out, added = backfill_missing_expander_points(zones, installed_rsps={1})
    assert _nums(out, rsp=2) == [517]                          # untouched


def test_no_ps_zone_falls_back_by_max_offset():
    """If both PS zones were dropped too, infer size by rounding the highest
    surviving point up to 8 or 16 — a point at offset 10 implies a 16-point block."""
    zones = [_rec(501, 1), _rec(512, 1)]                       # offset 11 -> 16-point
    out, added = backfill_missing_expander_points(zones, installed_rsps={1})
    assert _nums(out, rsp=1) == list(range(501, 517))
