"""Maps a parsed DMP worksheet (DMPDesign) onto the records to stage in DBISAM.

Two layers live here:

1. A *normalized* staging model (StagingAccount / StagingArea / StagingZone) —
   plain dataclasses, no DBISAM/ODBC knowledge. This is driver-independent and
   fully testable now.

2. The DBISAM column mapping (FIELD names) used by dbisam_writer. The column
   *names* below are taken from decrypted table headers and must be confirmed
   against the live schema via ODBC during Phase A/C — see VERIFY markers.

The DMP zone-TYPE codes are the panel's enumerated zone types. The CAD design
only distinguishes motion zones, supervisory zones (A/C-loss & battery), and
spares — so that is all this mapping derives. Exit/entry zones are not marked
in the CAD and default to Night; the tech adjusts those few in Remote Link.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .config import DEFAULT_AREA, DEFAULT_PANEL_MODEL
from parse_dmp_worksheet import DMPDesign, Zone


# --- DMP zone types ---------------------------------------------------------
# Codes as shown in Remote Link's "Programming Information" report / ZoneInfo.TYPE.
# VERIFY: confirm the exact stored representation against C:\Link\Db\ZoneInfo.dat
# (the report prints "NT"/"EX"/"SV"; the DB column may store the same 2-char
# code or an integer enum — settle this from a real row before writing).
ZONE_TYPE_NIGHT = "NT"        # Night — standard motion/intrusion zone
ZONE_TYPE_EXIT = "EX"         # Exit — entry/exit-delay zone
ZONE_TYPE_SUPERVISORY = "SV"  # Supervisory — A/C-loss & battery-trouble zones
ZONE_TYPE_SPARE = "--"        # Unused zone slot


def derive_zone_type(zone: Zone) -> str:
    """Pick the DMP zone TYPE for a parsed worksheet zone.

    The worksheet/CAD only tells us: supervisory (A/C or battery) vs spare vs
    'a motion zone'. Everything that is a real motion zone becomes Night; the
    handful of true exit zones are not distinguishable here and are left for
    the technician to flip in Remote Link.
    """
    if zone.is_spare:
        return ZONE_TYPE_SPARE
    if zone.is_ps_ac or zone.is_ps_batt:
        return ZONE_TYPE_SUPERVISORY
    return ZONE_TYPE_NIGHT


def derive_zone_room(zone: Zone) -> str:
    """The room/label part of a zone name (no 'Z###' prefix — sql_writer adds it).

    Supervisory rows carry a 'PS-N: ...' description in the worksheet; Remote
    Link stores them as plain 'A/C LOSS' / 'BATT. TRBL'. Spares are 'SPARE'.
    """
    if zone.is_spare:
        return "SPARE"
    if zone.is_ps_ac:
        return "A/C LOSS"
    if zone.is_ps_batt:
        return "BATT. TRBL"
    return (zone.description or "").strip()


# --- normalized staging model ----------------------------------------------

@dataclass
class StagingZone:
    number: int
    name: str
    zone_type: str
    area: str = DEFAULT_AREA
    is_spare: bool = False


@dataclass
class StagingArea:
    number: str
    name: str


@dataclass
class StagingAccount:
    account_num: str
    receiver_num: str
    name: str
    panel_model: str = DEFAULT_PANEL_MODEL
    address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    phone: str = ""
    areas: list[StagingArea] = field(default_factory=list)
    zones: list[StagingZone] = field(default_factory=list)
    keypads: list[int] = field(default_factory=list)   # keypad bus numbers

    @property
    def real_zone_count(self) -> int:
        return sum(1 for z in self.zones if not z.is_spare)

    @property
    def spare_zone_count(self) -> int:
        return sum(1 for z in self.zones if z.is_spare)


# --- address parsing --------------------------------------------------------

# SiteInfo.address_line2 looks like "ENCINO, CA 91316" (the parser normalizes it).
_ADDR2_RE = re.compile(r"^\s*(.+?)\s*,\s*([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)\s*$")


def _split_address_line2(line2: Optional[str]) -> tuple[str, str, str]:
    """Return (city, state, zip) from an 'CITY, ST 12345' string; blanks if unparseable."""
    if not line2:
        return "", "", ""
    m = _ADDR2_RE.match(line2)
    if not m:
        return line2.strip(), "", ""
    return m.group(1).strip(), m.group(2).upper(), m.group(3)


# --- DMPDesign -> StagingAccount -------------------------------------------

def build_staging_account(
    design: DMPDesign,
    account_num: str,
    receiver_num: str,
) -> StagingAccount:
    """Flatten a parsed worksheet into the account/area/zone records to stage.

    account_num is the school LOC CODE (parsed into site_info.school_code, but
    passed in explicitly so the CLI/operator can confirm or override it).
    receiver_num is operator-supplied (not present in the CAD design).
    """
    info = design.site_info
    city, state, zip_code = _split_address_line2(info.address_line2)

    acct = StagingAccount(
        account_num=str(account_num).strip(),
        receiver_num=str(receiver_num).strip(),
        name=(info.school_name or "").strip(),
        panel_model=DEFAULT_PANEL_MODEL,
        address=(info.address_line1 or "").strip(),
        city=city,
        state=state,
        zip_code=zip_code,
        phone=(info.phone or "").strip(),
    )

    # Single area/partition — C1's school designs put every zone in Area 01.
    acct.areas.append(StagingArea(number=DEFAULT_AREA, name=""))

    # The worksheet's Master sheet is a full-panel template that pre-lists zone
    # slots (and supervisory zones) for all 30 possible RSPs. Stage only the
    # zones that belong to an RSP actually present in this design — i.e. whose
    # number falls in an installed RSP's point range.
    real_zone_numbers: set[int] = set()
    for rsp in design.rsps:
        real_zone_numbers.update(rsp.zones)

    for z in sorted(design.master_zones, key=lambda x: x.number):
        if real_zone_numbers and z.number not in real_zone_numbers:
            continue
        acct.zones.append(StagingZone(
            number=z.number,
            name=derive_zone_room(z),
            zone_type=derive_zone_type(z),
            area=DEFAULT_AREA,
            is_spare=z.is_spare,
        ))

    # Keypad bus numbers — used to regenerate the DeviceInfo table for the
    # new account (the clone does not copy the template's devices).
    acct.keypads = sorted({k.number for k in design.keypads if k.number})

    return acct
