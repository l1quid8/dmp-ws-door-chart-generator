"""
Parses an OCR'd C1 intrusion-design PDF (must be searchable — run prepare_pdf.py first).

Extracts:
  - school_info: school name, address, location code, project name
  - combus_lines: RSP and Keypad location records (the 'COMBUS LINES (RSP & KEYPADS)' table)
  - zones: per-zone records from the 'MOTION DETECTOR ZONE SCHEDULE' table

OCR errors are corrected:
  - leading 'Z' often misread as '7' or '2' or 'Z7' — normalize to 'Z' + 3 digits
  - period in address misread as part of state name — normalize 'CA. 91340' -> 'CA 91340'
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF


# -------- data model --------

@dataclass
class ZoneRecord:
    zone: str                     # e.g. "Z501"
    rsp: int                      # 1..30
    building: Optional[str] = None
    floor: Optional[str] = None
    room: Optional[str] = None
    sensor_type: Optional[str] = None      # "NEW" / "EXISTING" / None
    cable_type: Optional[str] = None       # "(E)WP240R" etc
    is_spare: bool = False
    is_ps_ac: bool = False                 # AC POWER supervisory zone
    is_ps_batt: bool = False               # BATTERY TROUBLE supervisory zone


@dataclass
class CombusLine:
    kind: str          # "RSP" or "KEYPAD"
    n: int             # RSP number or keypad number
    building: str
    floor: str
    room: str
    fed_from: str      # MSP, RSP1, RSP2, etc
    cable_type: str


@dataclass
class ParsedDesign:
    school_info: dict = field(default_factory=dict)
    combus_lines: list[CombusLine] = field(default_factory=list)
    zones: list[ZoneRecord] = field(default_factory=list)


# -------- helpers --------

ZONE_NUMBER_RE = re.compile(
    r"""^
    (?:Z|7|2|1)?       # optional leading char (often misread for 'Z')
    (?:7|2|1)?         # sometimes a second leading digit-confused-for-Z
    (\d{3})            # the 3 actual zone digits
    /RSP(\d+)          # /RSP{n}
    \s*
    (.*)$              # the rest of the line (often empty when OCR puts each cell on its own line)
    """,
    re.VERBOSE,
)

# Identifier-only forms (when each table cell lands on its own line)
COMBUS_RSP_ID_RE = re.compile(r"^RSP\s*(\d+)\s*$", re.IGNORECASE)
COMBUS_KP_ID_RE  = re.compile(r"^KEYPAD\s+(\d+)\s*$", re.IGNORECASE)

# Sub-table label rows that appear interspersed in the zone schedule (e.g. "RSP 1", "RSp 4")
RSP_LABEL_RE = re.compile(r"^RS[Pp]\s+\d+\s*$")

# Zone-row split: try to recognize floor token within the row text
FLOOR_RE = re.compile(r"\b(\d+(?:st|ST|nd|ND|rd|RD|th|TH)\s+FLR)\b", re.IGNORECASE)
SENSOR_RE = re.compile(r"\b(NEW|EXISTING|N/A)\b", re.IGNORECASE)
CABLE_RE  = re.compile(r"\((?:N|E)\)[A-Z0-9]+(?:\([A-Z]\))?")

ZONE_RANGE_LO = 501
ZONE_RANGE_HI = 996


def normalize_zone_number(digits: str) -> str:
    """Returns 'Z' + the three-digit zone number; validates it's in expected range."""
    n = int(digits)
    if not (ZONE_RANGE_LO <= n <= ZONE_RANGE_HI):
        return ""
    return f"Z{n}"


def extract_school_info(text: str) -> dict:
    """Pulls school name, address, location code from the OCR'd text."""
    info: dict[str, str] = {}

    m = re.search(r"SCHOOL NAME:\s*([^\n]+)", text)
    if m:
        info["school_name"] = m.group(1).strip()

    # Address typically on two lines: "728 WOODWORTH ST," and "SAN FERNANDO. CA 91340"
    addr_m = re.search(r"(\d+\s+[A-Z][A-Z0-9 ]+ST,?)\s*\n([A-Z][A-Z ]+[.,]\s*[A-Z]{2}\s+\d{5})", text)
    if addr_m:
        info["address_line1"] = addr_m.group(1).rstrip(",").strip()
        # OCR sometimes puts a period before state — clean up
        info["address_line2"] = re.sub(r"([A-Z][A-Z ]+)[.,]\s*([A-Z]{2}\s+\d{5})", r"\1, \2", addr_m.group(2)).strip()

    # Location code: usually a 4-5 digit number near "LOC CODE".
    # OCR splits it across lines: "5726\nLOC \nCODE", so allow whitespace (incl. newlines) between LOC and CODE.
    m = re.search(r"(\d{4,5})\s*\n+\s*LOC\s+CODE", text, re.IGNORECASE)
    if m:
        info["loc_code"] = m.group(1).strip()
    else:
        # Try the other order
        m2 = re.search(r"LOC\s+CODE\s*[:\-]?\s*(\d{4,5})", text, re.IGNORECASE)
        if m2:
            info["loc_code"] = m2.group(1).strip()

    return info


# Single-line full address pattern (used against the title-block / page-1 text).
# Matches lines like '17551 MIRANDA ST. , ENCINO , CA 91316' or
# '728 WOODWORTH ST, SAN FERNANDO, CA 91340'.
TITLE_ADDRESS_RE = re.compile(
    r"^\s*(\d+\s+[A-Z][A-Z0-9. ]+?(?:ST|AVE|BLVD|RD|WAY|DR|LN|CT)\.?)\s*,\s*"
    r"([A-Z][A-Z ]+?)\s*,\s*([A-Z]{2})\s+(\d{5})\s*$",
    re.MULTILINE,
)
# Cities to skip (the contractor's address shares the title block with the site's).
_CONTRACTOR_CITIES = {"RANCHO CUCAMONGA"}


def extract_address_from_title_block(text: str) -> dict:
    """Find the school's site address in the title block (page-1 text).

    The title block lists the contractor's address (e.g. 'RANCHO CUCAMONGA') and
    the site's address. Skip any match whose city is a known contractor city so
    the door chart picks up the school's address, not the contractor's.
    """
    out: dict[str, str] = {}
    for m in TITLE_ADDRESS_RE.finditer(text):
        street = re.sub(r"\s+", " ", m.group(1)).strip()
        city = re.sub(r"\s+", " ", m.group(2)).strip()
        state = m.group(3)
        zip_code = m.group(4)
        if city.upper() in _CONTRACTOR_CITIES:
            continue
        out["address_line1"] = street
        out["address_line2"] = f"{city}, {state} {zip_code}"
        break
    return out


def extract_combus_lines(text: str) -> list[CombusLine]:
    """Reads the small COMBUS LINES (RSP & KEYPADS) table at top of the schedule page.

    PyMuPDF emits each table cell on its own line. Two layout variants exist across
    designs:
      Variant A (e.g. O'Melveny):  ID, building, floor, room, FED_FROM, CABLE_TYPE
      Variant B (e.g. Academy):    ID, building, floor, room, CABLE_TYPE, [FED_FROM]

    Plus the OCR may insert noise lines between rows (page sidebar text, addresses).

    Strategy:
      1. Take the first 3 cells positionally (building, floor, room).
      2. For the remaining cells, classify each by content (cable_type vs fed_from
         patterns) and skip lines that match neither.
      3. Stop at the next ID line or after consuming a bounded number of attempts.
    """
    lines = [ln.strip() for ln in text.splitlines()]

    # Bound the search to the COMBUS LINES section
    start = 0
    end = len(lines)
    for idx, s in enumerate(lines):
        if "COMBUS LINES" in s.upper():
            start = idx + 1
            break
    for idx in range(start, len(lines)):
        if "MOTION DETECTOR ZONE SCHEDULE" in lines[idx].upper():
            end = idx
            break

    cable_re = re.compile(r"^\([NE]\)[A-Z0-9]+", re.IGNORECASE)
    fed_re = re.compile(r"^(MSP|RSP\s*\d+)\s*$", re.IGNORECASE)
    msp_re = re.compile(r"^MSP\s*$", re.IGNORECASE)

    out: list[CombusLine] = []
    i = start
    while i < end:
        s = lines[i]
        if not s:
            i += 1
            continue

        m_rsp = COMBUS_RSP_ID_RE.match(s)
        m_kp = COMBUS_KP_ID_RE.match(s)
        if not (m_rsp or m_kp):
            i += 1
            continue

        # Take the first 3 positional cells (building, floor, room).
        positional: list[str] = []
        j = i + 1
        while j < end and len(positional) < 3:
            t = lines[j]
            if t:
                positional.append(t)
            j += 1

        # For cells 4 and 5 (cable_type and fed_from in either order): consume cells
        # that match cable or fed patterns. Stop on the FIRST line that's neither
        # — it's either noise (Academy) or the next entry's ID (which fed_re might
        # otherwise eat). Continue past empty lines.
        cable_type = ""
        fed_from = ""
        while j < end:
            t = lines[j]
            if not t:
                j += 1
                continue
            if cable_re.match(t):
                if not cable_type:
                    cable_type = t
                j += 1
                continue
            if fed_re.match(t):
                if fed_from:
                    # Already have a fed — this RSPn must be the next entry. Stop.
                    break
                # If we've already collected cable, "RSPn" here is ambiguous — could be
                # a fed_from in [cable, fed] layouts, or the next entry's ID. We can't
                # tell, so play safe: only accept MSP (unambiguous) once cable is set.
                if cable_type and not msp_re.match(t):
                    break
                fed_from = t.rstrip()
                j += 1
                continue
            # Neither cable nor fed — stop. Could be noise (Academy: 'PROJECT MGMT')
            # or a next-entry ID (caught on next outer-loop iteration).
            break

        kind = "RSP" if m_rsp else "KEYPAD"
        n = int((m_rsp or m_kp).group(1))
        out.append(CombusLine(
            kind=kind,
            n=n,
            building=positional[0] if len(positional) > 0 else "",
            floor=positional[1] if len(positional) > 1 else "",
            room=positional[2] if len(positional) > 2 else "",
            fed_from=fed_from,
            cable_type=cable_type,
        ))
        i = j
    return out


def extract_zones(text: str) -> list[ZoneRecord]:
    """Walks lines, picks out zone-schedule entries.

    PyMuPDF emits each table cell on its own line. A typical entry is 6 lines:
        Z7501/RSP1
        ADMIN BLDG
        1ST FLR
        CORRIDOR (N)
        EXISTING
        (E)WP240R
    Special cases:
      - SPARE rows have just "SPARE" after the zone-id
      - AC POWER / BATTERY TROUBLE supervisory rows have those tokens in the cable-type slot
      - Stray sub-table label lines like "RSP 1" / "RSp 4" appear interspersed and must be skipped
    """
    lines = [ln.strip() for ln in text.splitlines()]
    n_lines = len(lines)

    # Collect the indices of every line that introduces a zone record
    zone_starts: list[tuple[int, str, int]] = []  # (line_idx, zone_str, rsp_num)
    for idx, s in enumerate(lines):
        if not s:
            continue
        m = ZONE_NUMBER_RE.match(s)
        if not m:
            continue
        zone_num = normalize_zone_number(m.group(1))
        if not zone_num:
            continue
        zone_starts.append((idx, zone_num, int(m.group(2))))

    out: list[ZoneRecord] = []
    for k, (start_idx, zone_num, rsp) in enumerate(zone_starts):
        end_idx = zone_starts[k + 1][0] if k + 1 < len(zone_starts) else n_lines

        # Body = subsequent non-empty lines up to the next zone-id, skipping stray RSP labels
        body: list[str] = []
        for j in range(start_idx + 1, end_idx):
            t = lines[j]
            if not t:
                continue
            if RSP_LABEL_RE.match(t):
                continue
            body.append(t)

        rec = ZoneRecord(zone=zone_num, rsp=rsp)

        # SPARE: body's first non-noise cell is "SPARE"
        if body and body[0].upper().startswith("SPARE"):
            rec.is_spare = True
            out.append(rec)
            continue

        # Detect supervisory rows. Different designs use different terminology for the
        # AC-loss supervisory: "AC POWER" (O'Melveny), "AC TROUBLE" (Academy), or
        # "A/C LOSS". Battery is consistently "BATTERY TROUBLE".
        joined_upper = " ".join(body).upper()
        if re.search(r"\bA/?C\s+(POWER|TROUBLE|LOSS)\b", joined_upper):
            rec.is_ps_ac = True
        if "BATTERY TROUBLE" in joined_upper:
            rec.is_ps_batt = True

        # Positional mapping: body[0]=building, [1]=floor, [2]=room, [3]=sensor, [4]=cable
        if len(body) >= 1:
            rec.building = body[0]
        if len(body) >= 2 and FLOOR_RE.search(body[1]):
            rec.floor = body[1]
        if len(body) >= 3:
            rec.room = body[2].rstrip(",")
        if len(body) >= 4:
            sm = SENSOR_RE.match(body[3])
            if sm:
                rec.sensor_type = sm.group(1).upper()
        if len(body) >= 5:
            cm = CABLE_RE.search(body[4])
            if cm:
                rec.cable_type = cm.group(0)

        out.append(rec)
    return out


def parse_searchable_pdf(pdf_path: str | Path) -> ParsedDesign:
    """Top-level: open the OCR'd PDF and return structured records."""
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    doc = fitz.open(str(pdf_path))
    # Concatenate text from all pages — the zone schedule is on the page that contains it
    full_text_per_page: dict[int, str] = {i: p.get_text("text") for i, p in enumerate(doc)}
    doc.close()

    # Find the page containing the zone schedule (look for "MOTION DETECTOR ZONE SCHEDULE" header)
    schedule_text = ""
    for i, t in full_text_per_page.items():
        if "MOTION DETECTOR ZONE SCHEDULE" in t.upper():
            schedule_text = t
            break
    if not schedule_text:
        # Fall back: assume page 10 (index 9) per the C1 standard sheet ordering
        schedule_text = full_text_per_page.get(9, "")

    design = ParsedDesign()
    design.school_info = extract_school_info(schedule_text)
    # The address lives on the title-block page (typically page 1), not the
    # schedule page. Run a separate extraction there and merge without overwriting
    # any address fields the schedule-page parser may have already populated.
    title_text = full_text_per_page.get(0, "")
    for k, v in extract_address_from_title_block(title_text).items():
        design.school_info.setdefault(k, v)
    design.combus_lines = extract_combus_lines(schedule_text)
    design.zones = extract_zones(schedule_text)
    return design


# -------- CLI for quick testing --------

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python parse_zone_schedule.py <searchable_pdf>")
        sys.exit(1)
    design = parse_searchable_pdf(sys.argv[1])

    print("=== School info ===")
    for k, v in design.school_info.items():
        print(f"  {k}: {v!r}")

    print(f"\n=== Combus lines ({len(design.combus_lines)}) ===")
    for cl in design.combus_lines:
        print(f"  {cl.kind}-{cl.n}: {cl.building} | {cl.floor} | {cl.room} | fed={cl.fed_from} | cable={cl.cable_type}")

    print(f"\n=== Zones ({len(design.zones)}) ===")
    for z in design.zones[:30]:
        flags = []
        if z.is_spare: flags.append("SPARE")
        if z.is_ps_ac: flags.append("PS-AC")
        if z.is_ps_batt: flags.append("PS-BATT")
        flag_str = f"[{','.join(flags)}] " if flags else ""
        print(f"  {z.zone} RSP{z.rsp}: {flag_str}{z.building or '-'} | {z.floor or '-'} | {z.room or '-'} | {z.sensor_type or '-'} | {z.cable_type or '-'}")
    if len(design.zones) > 30:
        print(f"  ... ({len(design.zones)-30} more)")

    # Validation summary
    by_rsp: dict[int, int] = {}
    for z in design.zones:
        by_rsp[z.rsp] = by_rsp.get(z.rsp, 0) + 1
    print(f"\n=== Validation ===")
    print(f"  Total zones: {len(design.zones)}")
    print(f"  Zones per RSP: {dict(sorted(by_rsp.items()))}")
    print(f"  PS-AC supervisories: {sum(1 for z in design.zones if z.is_ps_ac)}")
    print(f"  PS-BATT supervisories: {sum(1 for z in design.zones if z.is_ps_batt)}")
    print(f"  SPAREs: {sum(1 for z in design.zones if z.is_spare)}")
