"""Tests for the .xlsx-worksheet input path (GUI dispatches through this logic).

Covers the only genuinely new logic — the "is this really a DMP worksheet?"
validation gate — plus the parse->inject round-trip that the xlsx flow reuses.
GUI state transitions are verified manually (no headless-Tk harness exists).

Run: pytest tests/test_xlsx_input.py
"""
from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from parse_dmp_worksheet import (  # noqa: E402
    DMPDesign,
    RSP,
    ZoneInfo,
    _master_zones_from_point_info,
    parse_dmp_worksheet,
    worksheet_looks_like_dmp,
)
from inject_door_chart import inject  # noqa: E402

WORKSHEET_FIXTURE = REPO_ROOT / "input" / "O'melveny DMP Worksheet_MOE.xlsx"
DOOR_CHART_TEMPLATE = REPO_ROOT / "door_chart_template_blank.xlsx"

pytestmark = pytest.mark.skipif(
    not WORKSHEET_FIXTURE.exists() or not DOOR_CHART_TEMPLATE.exists(),
    reason="sample worksheet / door-chart template fixtures not present",
)


def test_gate_accepts_real_worksheet():
    """A real, populated DMP worksheet passes the validation gate."""
    design = parse_dmp_worksheet(WORKSHEET_FIXTURE)
    assert worksheet_looks_like_dmp(design) is True


def test_gate_rejects_empty_design():
    """An empty/unrelated workbook (parses to an empty DMPDesign) is rejected."""
    assert worksheet_looks_like_dmp(DMPDesign()) is False


def test_parse_inject_round_trip(tmp_path):
    """parse->inject (the chain the xlsx flow reuses) writes a non-empty .xlsx
    without raising on a real worksheet, and the design now carries zones (so the
    door chart's zone area is no longer blank for this old-format fixture)."""
    design = parse_dmp_worksheet(WORKSHEET_FIXTURE)
    assert design.master_zones, "expected reconstructed master_zones for the fixture"
    out_path = tmp_path / "door_chart.xlsx"
    inject(DOOR_CHART_TEMPLATE, design, out_path)
    assert out_path.exists()
    assert out_path.stat().st_size > 0


# -------- Master-zones fallback (no 'Master' sheet) --------

def test_fallback_reconstructs_master_zones():
    """The old-format fixture has no Master sheet; master_zones is reconstructed
    1:1 from the Point Info zones and flagged with its source."""
    design = parse_dmp_worksheet(WORKSHEET_FIXTURE)
    assert design.master_zones_source == "point_info"
    assert len(design.master_zones) == len(design.zones) > 0
    assert len(design.master_zones) == 64  # 4 RSPs x 16 zones in this fixture


def test_fallback_flags_and_phrases():
    """Reconstructed zones use the exact descriptions/flags the door chart needs."""
    design = parse_dmp_worksheet(WORKSHEET_FIXTURE)

    spares = [z for z in design.master_zones if z.is_spare]
    assert spares and all(z.description == "SPARE" for z in spares)

    ac = [z for z in design.master_zones if z.is_ps_ac]
    batt = [z for z in design.master_zones if z.is_ps_batt]
    assert ac and all("A/C LOSS" in z.description for z in ac)
    assert batt and all("BATT. TRBL" in z.description for z in batt)
    # supervisory rows must carry an RSP number so the door chart can resolve the
    # power-supply location.
    assert all(z.rsp_number is not None for z in ac + batt)


def test_master_zones_from_point_info_unit():
    """Pure unit test of the reconstruction helper (no file dependency)."""
    rsps = [RSP(number=1, location="Bldg A", zones=list(range(501, 517)))]
    zones = [
        ZoneInfo(number=501, location="Main Office", device_type="Motion"),
        ZoneInfo(number=510, location="Spare", device_type=None),
        ZoneInfo(number=515, location="AC Power Trouble", device_type="Supervisory"),
        ZoneInfo(number=516, location="Battery Trouble", device_type="Supervisory"),
    ]
    out = {z.number: z for z in _master_zones_from_point_info(zones, rsps)}

    assert out[501].description == "Main Office"
    assert out[501].rsp_number == 1 and not out[501].is_spare

    assert out[510].description == "SPARE"
    assert out[510].is_spare and out[510].rsp_number is None

    assert out[515].description == "PS-1: A/C LOSS"
    assert out[515].is_ps_ac and out[515].rsp_number == 1

    assert out[516].description == "PS-1: BATT. TRBL"
    assert out[516].is_ps_batt and out[516].rsp_number == 1
