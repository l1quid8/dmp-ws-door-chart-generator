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
from hardware import zone_block_for


# Mapping: zone number → master row (bus-aligned)
def zone_to_master_row(zone_num: int) -> int:
    """501 -> 67, 596 -> 162, 601 -> 167, etc.  Returns 0 if out of range."""
    if not (501 <= zone_num <= 996):
        return 0
    # Linear: zone N (501..996) → row 67 + (N - 501). Door chart Master sheet's
    # zone area starts at row 67 and runs to row 562.
    return 67 + (zone_num - 501)


# The door chart's presentation tabs (Terminal Cans, RSPs, Power Supplies) read the
# Master data sheet at FIXED 16-row block anchors — one block per expander module,
# regardless of whether the module is a 16-port (714-16) or 8-port (714-8). The anchor
# for module N is the row where N's nominal 16-zone block (zone_block_for) starts.
def rsp_block_anchor(rsp_number: int) -> int:
    """Master row where module `rsp_number`'s presentation block starts (e.g. 1->67,
    2->83, 3->99, 7->167 across the bus jump)."""
    return 67 + (min(zone_block_for(rsp_number)) - 501)


# -------- surgical presentation edits --------
#
# The Master data sheet is a clean CONTIGUOUS zone list (zone N → row zone_to_master_row).
# The presentation tabs' fixed block anchors only line up with that when every module is
# 16-port; each 8-port module shifts every later block's source rows by 8. So we retarget
# each block's =Master! references to its RSP's real contiguous rows (and reshape 8-port
# blocks). The presentation sheets carry <tablePart>/<drawing> rels that openpyxl drops on
# re-save, so we edit their worksheet XML in place (preserving every other byte).

def _find_cell(xml: str, ref: str):
    """Match the full <c r="REF" ...>…</c> (or self-closing) element for cell REF.

    The two branches keep a styled self-closing cell (``<c r="B5" s="3"/>``) from being
    over-consumed into the next cell's </c>: the first alternative captures the cell's own
    ``/>``; only a real content cell falls through to ``>…</c>``.
    """
    return re.search(r'<c r="%s"(?:[^>]*?/>|[^>]*?>.*?</c>)' % re.escape(ref), xml, re.S)


def _blank_cell(xml: str, ref: str) -> str:
    """Replace cell REF with an empty cell, preserving its style index."""
    m = _find_cell(xml, ref)
    if not m:
        return xml
    sm = re.search(r'\bs="(\d+)"', m.group(0))
    style = f' s="{sm.group(1)}"' if sm else ""
    return xml[:m.start()] + f'<c r="{ref}"{style}/>' + xml[m.end():]


def _move_cell(xml: str, src_ref: str, dst_ref: str) -> str:
    """Copy src cell's content+style into dst (overwriting dst), keeping src as-is.

    Caller blanks src afterward. Used to lift the AUX POWER labels up the block.
    """
    ms = _find_cell(xml, src_ref)
    md = _find_cell(xml, dst_ref)
    if not ms or not md:
        return xml
    new = re.sub(r'^<c r="%s"' % re.escape(src_ref), f'<c r="{dst_ref}"', ms.group(0))
    return xml[:md.start()] + new + xml[md.end():]


def _set_cell_master_row(xml: str, ref: str, new_row: int) -> str:
    """Set the row of every =Master!X{row} reference in cell `ref` to `new_row` (keeping
    the column letter). Robust to irregular templates whose cells reference arbitrary rows.

    The cached <v> is left stale; inject sets fullCalcOnLoad + drops calcChain so Excel
    recomputes on open (same contract the rest of these formula sheets rely on).
    """
    m = _find_cell(xml, ref)
    if not m or "Master!" not in m.group(0):
        return xml
    cell = re.sub(r'Master!([A-Z]+)\d+',
                  lambda mm: f"Master!{mm.group(1)}{new_row}", m.group(0))
    return xml[:m.start()] + cell + xml[m.end():]


def _retarget_zone_tab(xml: str, kind: str, dmp_design: DMPDesign) -> str:
    """Retarget every block in Terminal Cans ('tc') or RSPs ('rsps') to its RSP's real
    contiguous Master rows; reshape 8-port blocks; blank unused blocks.

    Blocks are matched by VISUAL ORDER (the i-th block top-to-bottom, left-before-right, is
    RSP i+1) and each block's own header formula gives its current anchor — the RSPs tab's
    template uses an irregular anchor sequence, so anchor-based matching is not reliable.

    Geometry: header at R, then data rows starting R+data_off (16 slots), and (Terminal
    Cans only) an AUX POWER row at R+18.
    """
    master_col = "D" if kind == "tc" else "C"
    data_off = 2 if kind == "tc" else 3
    has_aux = kind == "tc"

    # All block headers, in reading order (header cells live in col B (left) / F (right)).
    blocks = [(int(m.group(2)), m.group(1), int(m.group(3)))
              for m in re.finditer(
                  r'<c r="([BF])(\d+)"[^>]*?><f>Master!%s(\d+)</f>' % master_col, xml)]
    blocks.sort(key=lambda b: (b[0], 0 if b[1] == "B" else 1))
    rsps = sorted(dmp_design.rsps, key=lambda r: r.number)

    for i, (R, col_letter, anchor) in enumerate(blocks):
        cols = ("B", "C", "D") if col_letter == "B" else ("F", "G", "H")

        if i >= len(rsps):
            # Unused module slot — blank the summary title (R), data rows and the static
            # AUX/PS footer, leaving an empty table. R+1 is the block's Excel Table HEADER
            # row ("Pin 1/Pin 2/Description" …); blanking it empties a header cell the
            # table part still names, which Excel flags as corrupt ("recover?"). Skip it.
            for r in range(R, R + 19):
                if r == R + 1:
                    continue
                for col in cols:
                    xml = _blank_cell(xml, f"{col}{r}")
            continue

        zs = sorted(rsps[i].zones)
        n = len(zs)

        # Header row R → the RSP's first zone row; each data position k → the row of that
        # RSP's k-th zone. Set rows directly (template data refs are unreliable). The header
        # is a merged cell (formula in col c1) but we sweep all three columns defensively.
        for col in cols:
            xml = _set_cell_master_row(xml, f"{col}{R}", zone_to_master_row(zs[0]))
        for k in range(n):
            target = zone_to_master_row(zs[k])
            for col in cols:
                xml = _set_cell_master_row(xml, f"{col}{R + data_off + k}", target)

        if n >= 16:
            continue
        # 8-port: only n real rows. Terminal Cans lifts AUX POWER to just below them.
        if has_aux:
            aux_dst = R + data_off + n
            for col in cols:
                xml = _move_cell(xml, f"{col}{R + 18}", f"{col}{aux_dst}")
            blank_from = aux_dst + 1
        else:
            blank_from = R + data_off + n
        for r in range(blank_from, R + 19):
            for col in cols:
                xml = _blank_cell(xml, f"{col}{r}")
    return xml


def _retarget_power_supplies(xml: str, dmp_design: DMPDesign) -> str:
    """The Power Supplies tab reads each module's supervisory pair from the 16-port slot
    positions =Master!{C,A}{anchor+14} / A{anchor+15}. Retarget those to each RSP's real
    supervisory rows (the last two of its sorted zones); blank unused modules' cells.
    """
    rsp_by_num = {r.number: r for r in dmp_design.rsps}
    remap: dict[str, str] = {}   # original ref -> retargeted ref (real RSPs)
    blank_refs: set[str] = set()  # refs whose containing cell should be blanked (unused)
    for M in range(1, 61):
        anchor = rsp_block_anchor(M)
        olds = [f"Master!C{anchor + 14}", f"Master!A{anchor + 14}", f"Master!A{anchor + 15}"]
        rsp = rsp_by_num.get(M)
        if rsp is None:
            blank_refs.update(olds)
            continue
        zs = sorted(rsp.zones)
        ac, batt = zone_to_master_row(zs[-2]), zone_to_master_row(zs[-1])
        remap[olds[0]] = f"Master!C{ac}"
        remap[olds[1]] = f"Master!A{ac}"
        remap[olds[2]] = f"Master!A{batt}"

    # Blank unused modules' cells: one O(n) sweep over all cells, collect those that
    # reference an unused slot, then blank each. (No content-search loop — that backtracks.)
    if blank_refs:
        to_blank = [cm.group(1) for cm in
                    re.finditer(r'<c r="([A-Z]+\d+)"(?:[^>]*?/>|[^>]*?>.*?</c>)', xml, re.S)
                    if any(b in cm.group(0) for b in blank_refs)]
        for ref in to_blank:
            xml = _blank_cell(xml, ref)

    # Retarget real refs in a single pass (callback reads the ORIGINAL ref, so a retargeted
    # value can never be re-mapped into another block's row).
    if remap:
        xml = re.sub(r'Master![AC]\d+(?=\D)',
                     lambda m: remap.get(m.group(0), m.group(0)), xml)
    return xml


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


def format_section_title(s: Splitter) -> str:
    """SPLITTER TOPOLOGY SECTION TITLE (Master col C): the block header shown per splitter
    on the LX-KP-710s sheet — '<location> - 710 Splitter <bus> - <id>'.

    LX bus number (500/600/700/800/900) is parsed from the slot id '710-LX{nnn}-N'; KP
    splitters have no bus number and read '710 Splitter Keypad Bus'.
    """
    m = re.search(r"LX(\d{3})", s.id)
    if m:
        bus = f"LX Bus {m.group(1)}"
    elif s.splitter_type == "KP":
        bus = "Keypad Bus"
    else:
        bus = f"{s.splitter_type} Bus"
    parts = [p for p in [(s.location or "").strip(), f"710 Splitter {bus}", s.id] if p]
    return " - ".join(parts)


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

        # Col C — SECTION TITLE: "<location> - 710 Splitter <bus> - <id>"
        title = format_section_title(s)
        if title:
            master[f"C{row}"] = title
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

    # 2. Master sheet — zone area (rows 67-562). A clean CONTIGUOUS zone list: zone N at
    # zone_to_master_row(N). The presentation tabs are retargeted to these rows later.
    master = wb["Master"]
    n_rooms = 0
    n_spares = 0
    n_ps = 0

    # Clear the whole zone area first so no template placeholder/garbage tail survives.
    for r in range(67, 563):
        for col in ("A", "B", "C", "D"):
            master[f"{col}{r}"] = None

    # Only real zones (those owned by an actual RSP module) get written — the worksheet's
    # Master can carry PS-N placeholder rows for modules that don't exist.
    rsp_zone_set = {z for rsp in dmp_design.rsps for z in rsp.zones}

    for z in dmp_design.master_zones:
        if rsp_zone_set and z.number not in rsp_zone_set:
            continue
        row = zone_to_master_row(z.number)
        if row == 0:
            continue
        master[f"A{row}"] = f"Z{z.number}"      # clean label column
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

    # RSP block headers — the Terminal Cans / RSPs tabs read Master!D / Master!C of each
    # block's first row for the per-block titles. Write them at the RSP's first contiguous
    # zone row (the retarget step points the header formulas here).
    for rsp in dmp_design.rsps:
        if not rsp.location or not rsp.zones:
            continue
        r = zone_to_master_row(min(rsp.zones))
        master[f"C{r}"] = format_rsp_location(rsp)
        master[f"D{r}"] = format_66_block_location(rsp)

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

    # Retarget the presentation tabs to the contiguous Master: each RSP block points to
    # its real rows, 8-port blocks are reshaped (8 rows → AUX → blanks), and unused module
    # blocks are blanked. In-place XML edits keep each sheet's <tablePart>/<drawing> intact.
    #   sheet4 = Terminal Cans, sheet5 = RSPs, sheet6 = Power Supplies
    PRESENTATION_REWRITERS = {
        "xl/worksheets/sheet4.xml": lambda x: _retarget_zone_tab(x, "tc", dmp_design),
        "xl/worksheets/sheet5.xml": lambda x: _retarget_zone_tab(x, "rsps", dmp_design),
        "xl/worksheets/sheet6.xml": lambda x: _retarget_power_supplies(x, dmp_design),
    }

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
                if item in PRESENTATION_REWRITERS:
                    xml = zin.read(item).decode("utf-8")
                    zout.writestr(item, PRESENTATION_REWRITERS[item](xml).encode("utf-8"))
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
