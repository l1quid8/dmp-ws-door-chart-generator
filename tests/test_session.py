"""Tests for session.py — the .dmps project persistence layer.

The serialization round-trip is the load-bearing contract: every field of a
populated DMPDesign must survive design -> JSON -> design, including the two
JSON-hostile shapes (int dict keys on PowerSupply.relays, tuples in
LocationConflict.options). The zone-sync helpers pin the dual-representation
contract (zones is editable truth, master_zones is derived) before any UI
relies on it.

Run: pytest tests/test_session.py
"""
from pathlib import Path
import json
import os
import sys
import time

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from parse_dmp_worksheet import (  # noqa: E402
    DMPDesign,
    Keypad,
    PowerSupply,
    RSP,
    SiteInfo,
    Splitter,
    Zone,
    ZoneInfo,
)
import session as session_mod  # noqa: E402
from session import (  # noqa: E402
    SCHEMA_VERSION,
    Session,
    SessionLoadError,
    clear_recovery,
    default_session_path,
    design_from_dict,
    design_to_dict,
    ensure_editable_zones,
    list_recent_sessions,
    load_recovery,
    load_session,
    normalize_zone_descriptions,
    pending_recovery,
    save_session,
    sync_master_zones,
    write_recovery,
)


def _populated_design() -> DMPDesign:
    from generate_dmp_ws import LocationConflict
    return DMPDesign(
        site_info=SiteInfo(
            school_name="DARBY AVENUE ELEMENTARY SCHOOL",
            school_code="1234567",
            phone="(818) 555-0000",
            install_tech="T. CALDWELL",
            install_date="2026-05-29",
            ip_address="10.101.148.96",
            default_gateway="10.101.148.1",
            xr550_location="MAIN BLDG 1ST FLR FACP ROOM",
            address_line1="123 Darby Ave",
            address_line2="Northridge, CA 91325",
        ),
        splitters=[Splitter(
            id="710-LX500-1", splitter_type="LX",
            location="MAIN BUILDING 1ST FLR FACP ROOM",
            inputs={"LX-Bus In": "From XR550"},
            outputs=["RSP-1", "710-LX500-2", "Spare"],
        )],
        rsps=[
            RSP(number=1, location="FACP ROOM", zones=list(range(501, 517))),
            RSP(number=2, location="PLANT MANAGER", zones=list(range(517, 533))),
        ],
        keypads=[Keypad(number=1, source="MSP", location="MAIN ENTRY", global_keypad=True)],
        power_supplies=[PowerSupply(number=1, location="FACP ROOM",
                                    relays={1: "LOCKDOWN", 2: "SIREN"})],
        zones=[
            ZoneInfo(number=501, location="FACP ROOM", device_type="Motion", partition=1),
            ZoneInfo(number=502, location="SPARE", device_type="Spare", partition=1),
            ZoneInfo(number=515, location="PS-1: A/C LOSS", device_type="Supervisory", partition=1),
            ZoneInfo(number=516, location="PS-1: BATT. TRBL", device_type="Supervisory", partition=1),
        ],
        master_zones=[Zone(number=501, description="FACP ROOM", rsp_number=1)],
        conflicts=[LocationConflict(
            kind="RSP", number=2, label="RSP 2 location",
            options=[("PLANT MANAGER", "COMBUS LINES table"),
                     ("CLASSROOM 17", "supervisory zones (A/C-loss & battery)")],
        )],
        topology_source="riser",
        master_zones_source="master",
    )


@pytest.fixture
def tmp_sessions_dir(tmp_path, monkeypatch):
    """Point sessions_dir at a temp folder so tests never touch real data."""
    d = tmp_path / "Sessions"
    d.mkdir(parents=True)
    monkeypatch.setattr(session_mod, "sessions_dir", lambda: d)
    return d


# -------- serialization round-trip --------

def test_design_round_trip_equality():
    design = _populated_design()
    restored = design_from_dict(json.loads(json.dumps(design_to_dict(design))))
    assert restored.site_info == design.site_info
    assert restored.splitters == design.splitters
    assert restored.rsps == design.rsps
    assert restored.keypads == design.keypads
    assert restored.power_supplies == design.power_supplies
    assert restored.zones == design.zones
    assert restored.master_zones == design.master_zones
    assert restored.conflicts == design.conflicts
    assert restored.topology_source == design.topology_source
    assert restored.master_zones_source == design.master_zones_source


def test_relays_int_keys_restored():
    design = _populated_design()
    restored = design_from_dict(json.loads(json.dumps(design_to_dict(design))))
    assert restored.power_supplies[0].relays == {1: "LOCKDOWN", 2: "SIREN"}
    assert all(isinstance(k, int) for k in restored.power_supplies[0].relays)


def test_conflict_options_tuples_restored():
    design = _populated_design()
    restored = design_from_dict(json.loads(json.dumps(design_to_dict(design))))
    options = restored.conflicts[0].options
    assert options == [("PLANT MANAGER", "COMBUS LINES table"),
                       ("CLASSROOM 17", "supervisory zones (A/C-loss & battery)")]
    assert all(isinstance(o, tuple) for o in options)


def test_forward_compat_missing_and_extra_keys():
    d = design_to_dict(_populated_design())
    d.pop("master_zones_source")          # field absent in an older file
    d["future_field"] = {"x": 1}          # field added by a newer app
    d["site_info"].pop("address_line2")
    d["site_info"]["future_site_field"] = "y"
    restored = design_from_dict(d)
    assert restored.master_zones_source == ""
    assert restored.site_info.address_line2 is None
    assert restored.site_info.school_name == "DARBY AVENUE ELEMENTARY SCHOOL"


def test_empty_design_round_trip():
    restored = design_from_dict(json.loads(json.dumps(design_to_dict(DMPDesign()))))
    assert restored == DMPDesign()


# -------- save / load / list --------

def test_save_load_session(tmp_sessions_dir):
    s = Session(design=_populated_design(), source_kind="pdf",
                source_name="DARBY_INTRUSION_DESIGN.pdf")
    path = save_session(s)
    assert path.suffix == ".dmps"
    assert path.parent == tmp_sessions_dir
    loaded = load_session(path)
    assert loaded.design.site_info == s.design.site_info
    assert loaded.source_kind == "pdf"
    assert loaded.source_name == "DARBY_INTRUSION_DESIGN.pdf"
    assert loaded.saved_at is not None
    assert loaded.path == path


def test_newer_schema_rejected(tmp_sessions_dir):
    s = Session(design=_populated_design())
    path = save_session(s)
    d = json.loads(path.read_text())
    d["schema_version"] = SCHEMA_VERSION + 1
    path.write_text(json.dumps(d))
    with pytest.raises(SessionLoadError, match="newer version"):
        load_session(path)


def test_corrupt_file_rejected(tmp_sessions_dir):
    path = tmp_sessions_dir / "BAD.dmps"
    path.write_text("{not json")
    with pytest.raises(SessionLoadError):
        load_session(path)


def test_atomic_save_leaves_no_tmp(tmp_sessions_dir):
    path = save_session(Session(design=_populated_design()))
    assert not list(tmp_sessions_dir.glob("*.tmp"))
    assert path.exists()


def test_list_recent_sessions_mtime_order(tmp_sessions_dir):
    d1 = _populated_design()
    d2 = _populated_design()
    d2.site_info.school_name = "THE ACADEMY OF ENRICHED SCIENCES"
    p1 = save_session(Session(design=d1))
    p2 = save_session(Session(design=d2))
    # Make ordering deterministic without sleeping.
    now = time.time()
    os.utime(p1, (now - 100, now - 100))
    os.utime(p2, (now, now))
    recents = list_recent_sessions()
    assert [r.school_name for r in recents] == [
        "THE ACADEMY OF ENRICHED SCIENCES",
        "DARBY AVENUE ELEMENTARY SCHOOL",
    ]
    assert all(r.saved_at for r in recents)


def test_list_skips_unreadable_files(tmp_sessions_dir):
    save_session(Session(design=_populated_design()))
    (tmp_sessions_dir / "JUNK.dmps").write_text("{broken")
    recents = list_recent_sessions()
    assert [r.school_name for r in recents] == ["DARBY AVENUE ELEMENTARY SCHOOL"]


def test_default_session_path_slug(tmp_sessions_dir):
    path = default_session_path(_populated_design())
    assert path.name == "DARBY_AVENUE_ELEMENTARY_SCHOOL.dmps"


# -------- crash recovery lifecycle --------

def test_recovery_written_then_cleared_on_save(tmp_sessions_dir):
    s = Session(design=_populated_design())
    path = save_session(s)
    rec = write_recovery(s)
    assert rec.exists()
    save_session(s)               # clean save clears recovery
    assert not rec.exists()


def test_pending_recovery_detected_when_newer(tmp_sessions_dir):
    s = Session(design=_populated_design())
    path = save_session(s)
    rec = write_recovery(s)
    now = time.time()
    os.utime(path, (now - 100, now - 100))
    os.utime(rec, (now, now))
    assert pending_recovery(path) is not None

    recovered = load_recovery(path)
    assert recovered.design.site_info.school_name == "DARBY AVENUE ELEMENTARY SCHOOL"
    assert recovered.path == path


def test_no_pending_recovery_when_older(tmp_sessions_dir):
    s = Session(design=_populated_design())
    path = save_session(s)
    rec = write_recovery(s)
    now = time.time()
    os.utime(rec, (now - 100, now - 100))
    os.utime(path, (now, now))
    assert pending_recovery(path) is None


def test_clear_recovery_missing_ok(tmp_sessions_dir):
    clear_recovery(tmp_sessions_dir / "NOPE.dmps")   # must not raise


# -------- zone sync helpers --------

def test_ensure_editable_zones_synthesis():
    design = DMPDesign(
        rsps=[RSP(number=1, zones=list(range(501, 517)))],
        master_zones=[
            Zone(number=501, description="FACP ROOM", rsp_number=1),
            Zone(number=502, description="SPARE", is_spare=True),
            Zone(number=515, description="PS-1: A/C LOSS", rsp_number=1, is_ps_ac=True),
            Zone(number=516, description="PS-1: BATT. TRBL", rsp_number=1, is_ps_batt=True),
        ],
    )
    ensure_editable_zones(design)
    by_num = {z.number: z for z in design.zones}
    assert by_num[501].location == "FACP ROOM" and by_num[501].device_type == "Motion"
    assert by_num[502].location == "SPARE" and by_num[502].device_type == "Spare"
    assert by_num[515].device_type == "Supervisory"
    assert by_num[516].device_type == "Supervisory"


def test_normalize_zone_descriptions_blank_to_spare():
    design = DMPDesign(
        zones=[
            ZoneInfo(number=501, location="CLASSROOM 1", device_type="Motion"),
            ZoneInfo(number=502, location="", device_type="Motion"),
            ZoneInfo(number=503, location="   ", device_type="Motion"),
            ZoneInfo(number=504, location=None, device_type="Motion"),
            ZoneInfo(number=505, location="new", device_type="Motion"),
        ],
    )
    normalize_zone_descriptions(design)
    by_num = {z.number: z for z in design.zones}
    assert by_num[501].location == "CLASSROOM 1"  # real room untouched
    for num in (502, 503, 504, 505):
        assert by_num[num].location == "SPARE"
        assert by_num[num].device_type == "Spare"


def test_normalize_zone_descriptions_supervisory_16zone_overrides_location():
    design = DMPDesign(
        rsps=[RSP(number=2, zones=list(range(501, 517)))],
        zones=[
            # last two carry equipment-location text that must be overridden
            ZoneInfo(number=515, location="ELECTRICAL ROOM", device_type="Motion"),
            ZoneInfo(number=516, location="ELECTRICAL ROOM", device_type="Motion"),
        ],
    )
    normalize_zone_descriptions(design)
    by_num = {z.number: z for z in design.zones}
    assert by_num[515].location == "PS-2: A/C LOSS"
    assert by_num[515].device_type == "Supervisory"
    assert by_num[516].location == "PS-2: BATT. TRBL"
    assert by_num[516].device_type == "Supervisory"


def test_normalize_zone_descriptions_supervisory_8zone():
    design = DMPDesign(
        rsps=[RSP(number=1, zones=list(range(501, 509)), model="714-8")],
        zones=[ZoneInfo(number=n, location="", device_type="Motion")
               for n in range(501, 509)],
    )
    normalize_zone_descriptions(design)
    by_num = {z.number: z for z in design.zones}
    assert by_num[507].location == "PS-1: A/C LOSS"
    assert by_num[508].location == "PS-1: BATT. TRBL"
    # the other six become SPARE, not supervisory
    assert by_num[501].location == "SPARE" and by_num[501].device_type == "Spare"
    assert by_num[506].location == "SPARE"


def test_normalize_zone_descriptions_supervisory_not_clobbered_by_spare():
    """Supervisory zones get labels in step 1 and survive the step-2 SPARE fill."""
    design = DMPDesign(
        rsps=[RSP(number=1, zones=list(range(501, 517)))],
        zones=[ZoneInfo(number=515, location="", device_type="Motion"),
               ZoneInfo(number=516, location="", device_type="Motion")],
    )
    normalize_zone_descriptions(design)
    by_num = {z.number: z for z in design.zones}
    assert by_num[515].location == "PS-1: A/C LOSS"
    assert by_num[516].location == "PS-1: BATT. TRBL"


def test_ensure_editable_zones_noop_when_zones_exist():
    design = _populated_design()
    before = list(design.zones)
    ensure_editable_zones(design)
    assert design.zones == before


def test_sync_master_zones_preserves_ps_phrases():
    """Round-trip: edited zones regenerate master rows with the exact phrases
    the door chart's conditional formatting requires."""
    design = _populated_design()
    sync_master_zones(design)
    by_num = {z.number: z for z in design.master_zones}
    assert by_num[502].is_spare and by_num[502].description == "SPARE"
    assert by_num[515].is_ps_ac and by_num[515].description == "PS-1: A/C LOSS"
    assert by_num[516].is_ps_batt and by_num[516].description == "PS-1: BATT. TRBL"
    assert by_num[501].description == "FACP ROOM" and by_num[501].rsp_number == 1
    assert design.master_zones_source == "point_info"


def test_sync_then_serialize_round_trip():
    design = _populated_design()
    design.zones[0].location = "RENAMED ROOM"
    sync_master_zones(design)
    restored = design_from_dict(json.loads(json.dumps(design_to_dict(design))))
    assert restored.master_zones == design.master_zones
    assert {z.number: z.description for z in restored.master_zones}[501] == "RENAMED ROOM"
