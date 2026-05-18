"""
Populates the C1 door chart template from a DMPDesign (parsed from the DMP worksheet).

The DMP worksheet is the single source of truth — zones, RSPs, splitters, and site
info all flow through it. The template (door_chart_template_blank.xlsx) is read-only;
this module copies it, writes into Master + Header sheets, and saves to output/.

Master sheet write surface (everything else inherits via formulas):
  Header!B3                — school name (from dmp_design.site_info.school_name)

  XR-550 CONFIG sub-table (rows 2-25, cols A-B):
    B3 = MSP location string (xr550_location, with first-splitter fallback)
    B8 = first KP splitter location
    B11..B15 = LX500..LX900 bus terminals (location of first splitter on each bus)

  710 BUS SPLITTER/REPEATER TOPOLOGY sub-table (rows 29-63, cols A-G):
    Rows 29-63 with pre-seeded slot IDs in col A (e.g. '710-LX500-1', '710-KP-1')
    Cols B-G = LOCATION, SECTION TITLE, COMBUS INPUT, OUTPUT 1, OUTPUT 2, OUTPUT 3

  Zone area (rows 67-562, cols B/C/D):
    Col B = ZONE DESCRIPTION (room name OR "SPARE" OR "PS-N: ..." label)
    Col C = RSP/PS LOCATION (composed from RSP location prefix)
    Col D = 66 BLOCK LOCATION (composed from RSP location prefix; only on first zone of each RSP block)
"""
from __future__ import annotations

import re
import shutil
from datetime import date
from pathlib import Path

import openpyxl
from openpyxl.workbook.properties import CalcProperties

from parse_dmp_worksheet import DMPDesign, Splitter, RSP


# Mapping: zone number → master row (bus-aligned)
def zone_to_master_row(zone_num: int) -> int:
    """501 -> 67, 596 -> 162, 601 -> 167, etc.  Returns 0 if out of range."""
    if not (501 <= zone_num <= 996):
        return 0
    # Linear: zone N (501..996) → row 67 + (N - 501). Door chart Master sheet's
    # zone area starts at row 67 and runs to row 562.
    return 67 + (zone_num - 501)


def _find_rsp(rsps: list[RSP], number: int) -> RSP | None:
    return next((r for r in rsps if r.number == number), None)


def format_rsp_location(rsp: RSP) -> str:
    """RSP/PS LOCATION string: '<DMP RSP location> - Remote Service Panel #N'."""
    return f"{rsp.location} - Remote Service Panel #{rsp.number}"


def format_66_block_location(rsp: RSP) -> str:
    """66 BLOCK LOCATION string: '<DMP RSP location> - Main SecTC 66 Block(s) - RSP N'."""
    return f"{rsp.location} - Main SecTC 66 Block(s) - RSP {rsp.number}"


def format_ps_supervisory_location(rsp: RSP) -> str:
    """PS supervisory row's RSP/PS LOCATION: '<DMP RSP location> - Power Supply #N'."""
    return f"{rsp.location} - Power Supply #{rsp.number}"


# -------- XR-550 CONFIG and SPLITTER TOPOLOGY population --------


def _build_slot_to_splitter_map(dmp_design: DMPDesign) -> dict[str, Splitter]:
    """Map template slot IDs to DMP splitters by direct ID equality.

    Both the door chart template's column-A slot IDs and the DMP's splitter
    IDs follow the IA-diagram convention ('710-LX500-N', '710-KP-N'), so
    the mapping is now trivial — no bus-resolution walk needed.
    """
    return {s.id: s for s in dmp_design.splitters}


def _populate_xr550_config(master, dmp_design: DMPDesign) -> int:
    """Populate Master rows 2-25 (XR-550 CONFIG sub-table).

    Populated cells:
      B3                — XR-550 panel location
      B8                — Keypad Bus terminal: location of first KP splitter
      B11..B15          — LX500..LX900 bus terminals: location of the FIRST
                          splitter on each bus (e.g. 710-LX500-1, 710-LX600-1)

    Left blank (no clean DMP source):
      B5, B6, B7, B9, B10  (AC, Battery, Bell, Ethernet, SMK/GND)
      B16..B25             (onboard zones Z1..Z10)
    """
    _LX500_BUS_RE = re.compile(r"^710-LX(\d{3})-(\d+)$")
    n_populated = 0

    # B3 — XR-550 panel location
    msp_location = dmp_design.site_info.xr550_location
    if not msp_location and dmp_design.splitters:
        msp_location = dmp_design.splitters[0].location
    if msp_location:
        master["B3"] = msp_location
        n_populated += 1

    # B8 — Keypad Bus: first KP splitter's location
    first_kp = next((s for s in dmp_design.splitters if s.splitter_type == "KP"), None)
    if first_kp and first_kp.location:
        master["B8"] = first_kp.location
        n_populated += 1

    # B11..B15 — LX bus terminals: location of the first 710 splitter on each bus
    bus_to_row = {500: 11, 600: 12, 700: 13, 800: 14, 900: 15}
    bus_first_loc: dict[int, str] = {}
    for s in dmp_design.splitters:
        if s.splitter_type != "LX":
            continue
        m = _LX500_BUS_RE.match(s.id)
        if not m:
            continue
        bus = int(m.group(1))
        slot = int(m.group(2))
        # Only the FIRST splitter on each bus (slot 1) supplies the bus-terminal label
        if slot == 1 and bus not in bus_first_loc and s.location:
            bus_first_loc[bus] = s.location
    for bus, loc in bus_first_loc.items():
        row = bus_to_row.get(bus)
        if row:
            master[f"B{row}"] = loc
            n_populated += 1

    return n_populated


def _populate_splitter_topology(master, dmp_design: DMPDesign) -> tuple[int, int]:
    """Populate Master rows 29-63 (SPLITTER TOPOLOGY sub-table).

    Strategy:
      1. Build a {slot_id: splitter} map by direct ID equality (both the template
         slot IDs in column A and the DMP splitter IDs follow the IA-diagram
         convention '710-LX500-N' / '710-KP-N').
      2. For each pre-seeded slot ID in template column A (rows 29-63), fill cols
         B (location), C (combined section title), D (combus input), and E/F/G
         (combus outputs 1/2/3) from the matching DMP splitter.
    """
    n_splitters = 0
    n_cells = 0
    slot_to_splitter = _build_slot_to_splitter_map(dmp_design)

    for row in range(29, 64):
        slot_id = master[f"A{row}"].value
        if not slot_id:
            continue
        s = slot_to_splitter.get(str(slot_id).strip())
        if not s:
            continue
        n_splitters += 1

        # Col B — LOCATION
        if s.location:
            master[f"B{row}"] = s.location
            n_cells += 1

        # Col C — SECTION TITLE (joined input descriptions, useful for at-a-glance routing)
        section_parts = [v for v in s.inputs.values() if v]
        if section_parts:
            master[f"C{row}"] = " | ".join(section_parts)
            n_cells += 1

        # Col D — COMBUS INPUT (the primary input description)
        if s.inputs:
            first_input = next((v for v in s.inputs.values() if v), None)
            if first_input:
                master[f"D{row}"] = first_input
                n_cells += 1

        # Cols E/F/G — COMBUS OUTPUT 1/2/3
        for i, out in enumerate(s.outputs[:3]):
            if out:
                col = chr(ord("E") + i)  # 'E', 'F', 'G'
                master[f"{col}{row}"] = out
                n_cells += 1

    return n_splitters, n_cells


def inject(template_path: Path, dmp_design: DMPDesign, output_path: Path) -> None:
    """Loads template, populates from DMPDesign, saves to output_path.

    Args:
        template_path: Path to the blank door chart template (will be copied, not modified).
        dmp_design:    DMPDesign from parse_dmp_worksheet.
        output_path:   Where to write the populated workbook.
    """
    # Defensive: never write to the template file
    template_path = Path(template_path).resolve()
    output_path = Path(output_path).resolve()
    if template_path == output_path:
        raise ValueError("Refusing to overwrite template — output path must differ")

    # Open the template directly with openpyxl. We DON'T copy template→output here
    # because the final save uses a binary-overlay strategy that preserves embedded
    # drawings/media (logos) which openpyxl would otherwise strip on save.
    wb = openpyxl.load_workbook(template_path)

    # Force recalc on next Excel open so all formulas refresh
    wb.calculation = CalcProperties(fullCalcOnLoad=True)

    # 1. Header sheet — school name + address (address comes from DMP custom doc props,
    # populated by generate_dmp_ws.py from the PDF title block).
    header = wb["Header"]
    if dmp_design.site_info.school_name:
        header["B3"] = dmp_design.site_info.school_name
    if dmp_design.site_info.address_line1:
        header["B4"] = dmp_design.site_info.address_line1
    if dmp_design.site_info.address_line2:
        header["B5"] = dmp_design.site_info.address_line2

    # 2. Master sheet — zone area (rows 67-562)
    master = wb["Master"]
    n_rooms = 0
    n_spares = 0
    n_ps = 0
    rsp_first_zone_filled: dict[int, bool] = {}

    for z in dmp_design.master_zones:
        row = zone_to_master_row(z.number)
        if row == 0:
            continue

        # Col B — zone description (already formatted in the DMP)
        master[f"B{row}"] = z.description

        if z.is_spare:
            n_spares += 1
            continue

        if z.is_ps_ac or z.is_ps_batt:
            n_ps += 1
            rsp = _find_rsp(dmp_design.rsps, z.rsp_number) if z.rsp_number else None
            if rsp and rsp.location:
                master[f"C{row}"] = format_ps_supervisory_location(rsp)
            continue

        n_rooms += 1
        if z.rsp_number is not None and not rsp_first_zone_filled.get(z.rsp_number):
            rsp = _find_rsp(dmp_design.rsps, z.rsp_number)
            if rsp and rsp.location:
                master[f"C{row}"] = format_rsp_location(rsp)
                master[f"D{row}"] = format_66_block_location(rsp)
                rsp_first_zone_filled[z.rsp_number] = True

    # 3. XR-550 CONFIG + SPLITTER TOPOLOGY (always populated)
    n_xr550_cells = _populate_xr550_config(master, dmp_design)
    n_splitters, n_splitter_cells = _populate_splitter_topology(master, dmp_design)

    # openpyxl's wb.save strips parts it doesn't natively understand — including
    # xl/drawings/* and xl/media/* (the LAUSD + ConvergeOne logos). Save openpyxl's
    # output to a temp file, then start from a binary template copy and overlay
    # only the parts we need:
    #   - sheet1.xml (Header) and sheet2.xml (Master) — the two sheets we modify
    #   - workbook.xml — carries fullCalcOnLoad=True so Excel recomputes on open
    #     (sheets 3-7 are formula sheets like '=Master!B5'; without recompute
    #     they show stale cached values from the template's last save)
    #
    # Sheets 3-7 (MSP, Terminal Cans, RSPs, Power Supplies, LX-KP-710s) have
    # <tablePart> + <drawing> references whose rels point at template files —
    # we leave their XML untouched to avoid desync (which causes "repair file").
    #
    # We also strip xl/calcChain.xml: it's a cached calculation order. With it
    # present + cached values in the template's formula cells, Excel skips the
    # recompute even with fullCalcOnLoad=True. Drop it (plus its declarations
    # in Content_Types and workbook rels) and Excel rebuilds the chain on open.
    import re as _re
    import zipfile
    from tempfile import NamedTemporaryFile

    OVERLAY_FILES = {
        "xl/worksheets/sheet1.xml",  # Header
        "xl/worksheets/sheet2.xml",  # Master
        "xl/workbook.xml",           # fullCalcOnLoad=True flag
    }
    DROP_FILES = {"xl/calcChain.xml"}

    with NamedTemporaryFile(delete=False, suffix=".xlsx") as tmpf:
        openpyxl_tmp_path = Path(tmpf.name)
    try:
        wb.save(openpyxl_tmp_path)

        overlays: dict[str, bytes] = {}
        with zipfile.ZipFile(openpyxl_tmp_path) as zop:
            for name in OVERLAY_FILES:
                if name in zop.namelist():
                    overlays[name] = zop.read(name)

        shutil.copy2(template_path, output_path)
        with NamedTemporaryFile(delete=False, suffix=".xlsx") as tmpf2:
            rebuild_path = Path(tmpf2.name)
        with zipfile.ZipFile(output_path, "r") as zin, \
             zipfile.ZipFile(rebuild_path, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.namelist():
                if item in DROP_FILES:
                    continue
                if item == "[Content_Types].xml":
                    # Drop the calcChain Override declaration (otherwise Excel
                    # complains about a missing part). Use [^>]*? to span attribute
                    # values containing slashes (e.g. ContentType MIME values).
                    ct = zin.read(item).decode("utf-8")
                    ct = _re.sub(
                        r'<Override\s+PartName="/xl/calcChain\.xml"[^>]*?/>',
                        "",
                        ct,
                    )
                    zout.writestr(item, ct.encode("utf-8"))
                    continue
                if item == "xl/_rels/workbook.xml.rels":
                    # Drop the calcChain Relationship for the same reason
                    rels = zin.read(item).decode("utf-8")
                    rels = _re.sub(
                        r'<Relationship\b[^>]*?Target="calcChain\.xml"[^>]*?/>',
                        "",
                        rels,
                    )
                    zout.writestr(item, rels.encode("utf-8"))
                    continue
                data = overlays.get(item, zin.read(item))
                zout.writestr(item, data)
        shutil.move(rebuild_path, output_path)
    finally:
        if openpyxl_tmp_path.exists():
            openpyxl_tmp_path.unlink()

    print(f"Injected: {n_rooms} rooms, {n_spares} spares, {n_ps} PS supervisory rows")
    print(f"  XR-550 CONFIG: {n_xr550_cells} cells populated")
    print(f"  SPLITTER TOPOLOGY: {n_splitters} splitters ({n_splitter_cells} cells populated)")
    print(f"Output: {output_path}")


# -------- CLI --------

def _slugify(name: str) -> str:
    """Make a file-safe, simple slug from school name."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return s or "OUTPUT"


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("usage: python inject_door_chart.py <dmp_xlsx> <template_xlsx> [output_dir]")
        sys.exit(1)

    dmp_path = Path(sys.argv[1])
    template_path = Path(sys.argv[2])
    output_dir = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("output")
    output_dir.mkdir(exist_ok=True)

    print(f"Parsing {dmp_path}...")
    from parse_dmp_worksheet import parse_dmp_worksheet
    dmp = parse_dmp_worksheet(dmp_path)
    print(f"  splitters={len(dmp.splitters)} rsps={len(dmp.rsps)} master_zones={len(dmp.master_zones)}")

    school = dmp.site_info.school_name or "OUTPUT"
    out_name = f"{_slugify(school)}_door_chart_{date.today().isoformat()}.xlsx"
    out_path = output_dir / out_name

    inject(template_path, dmp, out_path)
