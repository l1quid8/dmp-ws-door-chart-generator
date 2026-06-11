"""Tests for validation.py — the finalize-gate rule engine.

One positive and one negative case per rule, plus badge aggregation. These
rules are the contract for what 'FINAL' means; a FINAL worksheet must never
ship with a blank zone, a placeholder, or unreviewed auto-derived wiring.

Run: pytest tests/test_validation.py
"""
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from parse_dmp_worksheet import (  # noqa: E402
    DMPDesign,
    RSP,
    SiteInfo,
    Splitter,
    ZoneInfo,
)
from validation import (  # noqa: E402
    Issue,
    badge_counts,
    errors,
    finalize_ok,
    rsp_hyphen_fix,
    validate_design,
)


def _valid_design() -> DMPDesign:
    """A design that passes every rule (the baseline each test perturbs)."""
    return DMPDesign(
        site_info=SiteInfo(
            school_name="DARBY AVENUE ELEMENTARY SCHOOL",
            ip_address="10.101.148.96",
            default_gateway="10.101.148.1",
            install_date="2026-05-29",
            install_tech="T. CALDWELL",
        ),
        splitters=[Splitter(id="710-LX500-1", splitter_type="LX",
                            outputs=["RSP-1", "710-LX500-2", "SPARE"])],
        rsps=[RSP(number=1, location="FACP ROOM", zones=[501, 502])],
        zones=[
            ZoneInfo(number=501, location="FACP ROOM", device_type="Motion", partition=1),
            ZoneInfo(number=502, location="SPARE", device_type="Spare", partition=1),
        ],
        topology_source="riser",
    )


def _codes(issues: list[Issue]) -> set[str]:
    return {i.code for i in issues}


def test_clean_design_passes():
    issues = validate_design(_valid_design(), topology_confirmed=True)
    assert errors(issues) == []
    assert finalize_ok(issues)


# -------- site.required_missing --------

def test_missing_site_fields_error():
    design = _valid_design()
    design.site_info.ip_address = None
    design.site_info.default_gateway = "  "
    issues = validate_design(design, topology_confirmed=True)
    missing = [i for i in issues if i.code == "site.required_missing"]
    assert {i.ref for i in missing} == {"field:ip_address", "field:default_gateway"}
    assert all(i.tab == "SITE" and i.severity == "error" for i in missing)
    assert not finalize_ok(issues)


# -------- zone description rules --------

def test_blank_zone_desc_errors():
    design = _valid_design()
    design.zones[0].location = ""
    issues = validate_design(design, topology_confirmed=True)
    assert "zone.blank_desc" in _codes(issues)
    blank = next(i for i in issues if i.code == "zone.blank_desc")
    assert blank.ref == "zone:501" and blank.tab == "ZONES"


def test_new_placeholder_errors():
    design = _valid_design()
    design.zones[0].location = "NEW"
    issues = validate_design(design, topology_confirmed=True)
    assert "zone.new_placeholder" in _codes(issues)


def test_spare_case_errors():
    design = _valid_design()
    design.zones[1].location = "Spare"
    issues = validate_design(design, topology_confirmed=True)
    assert "zone.spare_case" in _codes(issues)


def test_uppercase_spare_passes():
    issues = validate_design(_valid_design(), topology_confirmed=True)
    assert "zone.spare_case" not in _codes(issues)
    assert "zone.blank_desc" not in _codes(issues)


# -------- naming.rsp_hyphen --------

def test_unhyphenated_rsp_in_splitter_output_errors():
    design = _valid_design()
    design.splitters[0].outputs[0] = "RSP 1"
    issues = validate_design(design, topology_confirmed=True)
    hits = [i for i in issues if i.code == "naming.rsp_hyphen"]
    assert hits and hits[0].tab == "SPLITTERS"
    assert hits[0].ref == "splitter:710-LX500-1"


def test_unhyphenated_rsp_in_zone_desc_errors():
    design = _valid_design()
    design.zones[0].location = "NEXT TO RSP3"
    issues = validate_design(design, topology_confirmed=True)
    hits = [i for i in issues if i.code == "naming.rsp_hyphen"]
    assert hits and hits[0].ref == "zone:501"


def test_hyphenated_rsp_passes():
    issues = validate_design(_valid_design(), topology_confirmed=True)
    assert "naming.rsp_hyphen" not in _codes(issues)


def test_rsp_hyphen_fix():
    assert rsp_hyphen_fix("RSP 3") == "RSP-3"
    assert rsp_hyphen_fix("RSP3") == "RSP-3"
    assert rsp_hyphen_fix("RSP-3") == "RSP-3"
    assert rsp_hyphen_fix("To RSP 12 and RSP4") == "To RSP-12 and RSP-4"


# -------- conflicts.unresolved --------

def test_unresolved_conflict_errors():
    from generate_dmp_ws import LocationConflict
    design = _valid_design()
    design.conflicts.append(LocationConflict(
        kind="RSP", number=1, label="RSP 1 location",
        options=[("A", "src1"), ("B", "src2")],
    ))
    issues = validate_design(design, topology_confirmed=True)
    assert "conflicts.unresolved" in _codes(issues)
    assert not finalize_ok(issues)


# -------- topology.unconfirmed --------

def test_auto_derived_unconfirmed_is_error():
    design = _valid_design()
    design.topology_source = "auto-derived"
    issues = validate_design(design, topology_confirmed=False)
    topo = next(i for i in issues if i.code == "topology.unconfirmed")
    assert topo.severity == "error"
    assert not finalize_ok(issues)


def test_riser_unconfirmed_is_warning():
    issues = validate_design(_valid_design(), topology_confirmed=False)
    topo = next(i for i in issues if i.code == "topology.unconfirmed")
    assert topo.severity == "warning"
    assert finalize_ok(issues)  # warnings don't block


def test_confirmed_topology_silent():
    issues = validate_design(_valid_design(), topology_confirmed=True)
    assert "topology.unconfirmed" not in _codes(issues)


# -------- capacity.exceeded --------

def test_over_capacity_errors():
    from hardware import MAX_EXPANDERS, MAX_KEYPADS, MAX_SPLITTERS_PER_TYPE
    from parse_dmp_worksheet import Keypad
    design = _valid_design()
    design.rsps = [RSP(number=n, location="X", zones=[501 + 16 * (n - 1)])
                   for n in range(1, MAX_EXPANDERS + 2)]
    design.splitters = [Splitter(id=f"710-LX500-{n}", splitter_type="LX",
                                 outputs=["Spare"] * 3)
                        for n in range(1, MAX_SPLITTERS_PER_TYPE + 2)]
    design.keypads = [Keypad(number=n, source="MSP")
                      for n in range(1, MAX_KEYPADS + 2)]
    issues = validate_design(design, topology_confirmed=True)
    caps = [i for i in issues if i.code == "capacity.exceeded"]
    assert {i.tab for i in caps} == {"POWER", "SPLITTERS", "KEYPADS"}
    assert not finalize_ok(issues)


def test_within_capacity_silent():
    issues = validate_design(_valid_design(), topology_confirmed=True)
    assert "capacity.exceeded" not in _codes(issues)


# -------- keypad.source_missing --------

def test_sourceless_keypad_errors():
    from parse_dmp_worksheet import Keypad
    design = _valid_design()
    design.keypads = [Keypad(number=1, source="MSP"),
                      Keypad(number=2, source=None)]
    issues = validate_design(design, topology_confirmed=True)
    hits = [i for i in issues if i.code == "keypad.source_missing"]
    assert len(hits) == 1 and hits[0].ref == "keypad:2"
    assert hits[0].tab == "KEYPADS"


def test_sourced_keypads_silent():
    from parse_dmp_worksheet import Keypad
    design = _valid_design()
    design.keypads = [Keypad(number=1, source="MSP")]
    issues = validate_design(design, topology_confirmed=True)
    assert "keypad.source_missing" not in _codes(issues)


# -------- aggregation --------

def test_badge_counts_group_errors_by_tab():
    design = _valid_design()
    design.site_info.ip_address = None          # 1 SITE error
    design.zones[0].location = "NEW"            # 1 ZONES error
    design.zones[1].location = ""               # 1 ZONES error
    issues = validate_design(design, topology_confirmed=False)  # riser warning: no badge
    counts = badge_counts(issues)
    assert counts == {"SITE": 1, "ZONES": 2}
