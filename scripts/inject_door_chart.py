"""
Populates the C1 door chart template from a DMPDesign (parsed from the DMP worksheet).

The DMP worksheet is the single source of truth — zones, RSPs, splitters, and site
info all flow through it. The template (door_chart_template_blank.xlsx) is read-only;
this module copies it, writes into Master + Header sheets, and saves to output/.

The output is CONSOLIDATED: the template's presentation tabs pre-draw 15 placeholder
block groups each, and every group past the last filled one is truncated away (with
its Excel Tables and logo drawings), so the file ships ready to hand off — no manual
deletion of empty door chart scaffolding.

Master sheet write surface (everything else inherits via formulas):
  Header!B3                — school name (from dmp_design.site_info.school_name)

  XR-550 CONFIG sub-table (rows 2-25, cols A-B):
    B3 = MSP location string (xr550_location, with first-splitter fallback)
    B8 = first KP splitter location
    B11..B15 = LX500..LX900 bus terminals (location of first splitter on each bus)

  710 BUS SPLITTER/REPEATER TOPOLOGY sub-table (rows 29-63, cols A-G):
    Col A = splitter ID, written top-down in display order (LX by bus then number,
      then KP). The template ships placeholder slot IDs here; they are overwritten.
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


def _consolidate_lx(xml: str, filled_rows: list[int]) -> str:
    """Compact the LX-KP-710s tab. The template pre-wires 30 chart slots to Master
    topology rows 29..63 in reading order, but a job's real splitters are scattered
    across the per-bus sections (e.g. the first KP splitter sits ~20 slots down).
    Point the earliest slots at the filled Master rows, in order, so truncation can
    always cut at a group boundary.

    Slot geometry: title {B|E}T = =Master!C{row}; data {C|F}{T+2..T+5} = =Master!{D..G}{row}.
    T+1 is the slot's Excel Table header row — blanking it corrupts the named table.
    """
    slots = sorted(((int(m.group(2)), m.group(1)) for m in re.finditer(
        r'<c r="([BE])(\d+)"[^>]*?><f>Master!C\d+</f>', xml)),
        key=lambda s: (s[0], 0 if s[1] == "B" else 1))
    kept_slots = 2 * max(1, (len(filled_rows) + 1) // 2)  # whole groups of two
    for k, (T, col) in enumerate(slots):
        c2 = "C" if col == "B" else "F"
        if k < len(filled_rows):
            xml = _set_cell_master_row(xml, f"{col}{T}", filled_rows[k])
            for off in range(2, 6):
                xml = _set_cell_master_row(xml, f"{c2}{T + off}", filled_rows[k])
        elif k < kept_slots:
            # Leftover slot inside the last kept group (odd splitter count, or none at
            # all): blank its title, labels and data — skipping the table header row.
            for r in range(T, T + 6):
                if r == T + 1:
                    continue
                xml = _blank_cell(xml, f"{col}{r}")
                xml = _blank_cell(xml, f"{c2}{r}")
        # Slots past the kept groups fall below the cutoff; _truncate_rows removes them.
    return xml


# -------- consolidation: truncate empty placeholder blocks --------
#
# Each presentation sheet carries 15 pre-drawn block groups (two charts per group,
# left cols B.. / right cols E../F..). Retargeting fills only the first groups, so the
# finished document should end right after the last filled group — the hand-finished
# deliverables always deleted the empty scaffolding below. We truncate each sheet at a
# group boundary and drop the orphaned Excel Tables and logo drawing anchors so Excel
# doesn't see dangling parts ("repair file").

_SHEET_LAYOUT = {
    # part -> (block-group pitch, a group's last content row relative to its start row)
    "xl/worksheets/sheet3.xml": (25, 24),  # Terminal Cans
    "xl/worksheets/sheet4.xml": (26, 23),  # RSPs (irregular: group 10 starts a row early)
    "xl/worksheets/sheet5.xml": (12, 10),  # Power Supplies
    "xl/worksheets/sheet6.xml": (12, 10),  # LX-KP-710s
}
_N_GROUPS = 15


def _sheet_cutoff(xml: str, part: str, n_charts: int) -> int:
    """Last worksheet row to KEEP on a presentation sheet holding `n_charts` charts.

    Group start rows are read from the sheet's own =Header!B3 header cells rather
    than computed (the RSPs tab's group 10 breaks the uniform pitch). The Power
    Supplies / LX tabs stop carrying Header!B3 on later groups, but their chart
    slots stay on the regular pitch — extrapolate.
    """
    pitch, last_off = _SHEET_LAYOUT[part]
    starts = sorted({int(m.group(1)) for m in re.finditer(
        r'<c r="[A-Z]+(\d+)"[^>]*?><f>Header!B3</f>', xml)})
    if not starts or starts[0] != 2:
        raise AssertionError(
            f"{part}: no block-group header at row 2 — template layout changed, "
            "update _SHEET_LAYOUT")
    while len(starts) < _N_GROUPS:
        starts.append(starts[-1] + pitch)
    g = min(max(1, (n_charts + 1) // 2), _N_GROUPS)  # two charts per group, keep >= 1
    cutoff = starts[g - 1] + last_off
    assert g == _N_GROUPS or cutoff < starts[g], \
        f"{part}: cutoff {cutoff} overlaps group {g + 1} (row {starts[g]})"
    return cutoff


def _truncate_rows(xml: str, cutoff: int) -> str:
    """Delete every sheet row past `cutoff`, the merges that covered them, and shrink
    the declared dimension. Cutoffs are group-aligned, so nothing straddles."""
    starts = [(m.start(), int(m.group(1))) for m in re.finditer(r'<row r="(\d+)"', xml)]
    assert all(a[1] < b[1] for a, b in zip(starts, starts[1:])), "sheet rows not ascending"
    first_cut = next((pos for pos, r in starts if r > cutoff), None)
    if first_cut is not None:
        xml = xml[:first_cut] + xml[xml.index("</sheetData>"):]

    mc = re.search(r'<mergeCells count="\d+">(.*?)</mergeCells>', xml, re.S)
    if mc:
        kept = []
        for mm in re.finditer(r'<mergeCell ref="[A-Z]+(\d+):[A-Z]+(\d+)"/>', mc.group(1)):
            lo, hi = sorted((int(mm.group(1)), int(mm.group(2))))
            if lo > cutoff:
                continue
            assert hi <= cutoff, f"merge {mm.group(0)} straddles truncation cutoff {cutoff}"
            kept.append(mm.group(0))
        xml = (xml[:mc.start()]
               + f'<mergeCells count="{len(kept)}">' + "".join(kept) + "</mergeCells>"
               + xml[mc.end():])

    xml = re.sub(r'(<dimension ref="[A-Z]+\d+:[A-Z]+)(\d+)"/>',
                 lambda m: f'{m.group(1)}{min(cutoff, int(m.group(2)))}"/>', xml, count=1)
    return xml


def _drop_table_parts(xml: str, drop_rids: set[str]) -> str:
    """Remove <tablePart> entries for dropped tables and fix the count attribute."""
    for rid in drop_rids:
        xml = re.sub(r'<tablePart r:id="%s"/>' % re.escape(rid), "", xml)
    m = re.search(r'<tableParts count="\d+">', xml)
    if m:
        n = len(re.findall(r"<tablePart ", xml))
        xml = xml[:m.start()] + f'<tableParts count="{n}">' + xml[m.end():]
    return xml


def _strip_drawing_anchors(xml: str, cutoff: int) -> str:
    """Remove logo anchors that start past the truncation cutoff (anchor rows are
    0-based, so a 0-based `from` row >= cutoff lies on an Excel row > cutoff)."""
    def drop(m: re.Match) -> str:
        fr = re.search(r"<xdr:from>.*?<xdr:row>(\d+)</xdr:row>", m.group(0), re.S)
        return "" if fr and int(fr.group(1)) >= cutoff else m.group(0)
    return re.sub(r"<xdr:(twoCellAnchor|oneCellAnchor)\b.*?</xdr:\1>", drop, xml, flags=re.S)


def _drop_rels(rels_xml: str, target_names: set[str]) -> str:
    """Remove Relationship entries whose Target basename is in `target_names`."""
    def drop(m: re.Match) -> str:
        t = re.search(r'Target="[^"]*?([^/"]+)"', m.group(0))
        return "" if t and t.group(1) in target_names else m.group(0)
    return re.sub(r"<Relationship\b[^>]*?/>", drop, rels_xml)


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


# Master's topology sub-table, and the number of charts the LX-KP-710s tab can
# draw (15 pre-drawn block groups, two charts each).
_TOPOLOGY_ROWS = range(29, 64)
_MAX_TOPOLOGY_CHARTS = _N_GROUPS * 2

_LX_ID_RE = re.compile(r"^710-LX(\d{3})-(\d+)$")
_KP_ID_RE = re.compile(r"^710-KP-(\d+)$")


def _splitter_sort_key(s: Splitter) -> tuple:
    """Display order: LX splitters by bus then number, then KP by number.

    Mirrors the reading order of the template's placeholder slot IDs, so jobs that
    fit the old per-bus layout produce a byte-identical topology table.
    """
    sid = (s.id or "").strip()
    if s.splitter_type == "KP":
        m = _KP_ID_RE.match(sid)
        return (1, 0, int(m.group(1)) if m else 10**6, sid)
    if s.splitter_type == "LX":
        m = _LX_ID_RE.match(sid)
        return (0, int(m.group(1)), int(m.group(2)), sid) if m else (0, 10**6, 10**6, sid)
    return (2, 0, 0, sid)


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


def _populate_splitter_topology(master, dmp_design: DMPDesign) -> tuple[list[int], int]:
    """Populate Master rows 29-63 (SPLITTER TOPOLOGY sub-table).

    Splitter-driven: every splitter in the design is written to a consecutive row
    starting at 29, in display order, with col A set to its real ID.

    It used to be slot-driven — walk the template's placeholder IDs in col A and
    match a splitter by exact ID equality. That silently dropped any splitter whose
    ID named no slot. The template spreads its LX slots over five buses
    (710-LX500-1..5, 710-LX600-1..5, ... 710-LX900-1..5), but hardware.next_splitter_id
    numbers every LX splitter on the 500 bus up to MAX_SPLITTERS_PER_TYPE, so a job
    with six or more LX splitters lost the sixth onward with no error — HAYNES_CHARTER_ES
    shipped without 710-LX500-6 and 710-LX500-7. Col A is an internal key: the
    LX-KP-710s tab reads only cols C-G, so overwriting it is safe.

    Returns (filled Master rows in ascending order, cells written) — the row list
    drives the LX-KP-710s tab's chart compaction.
    """
    filled_rows: list[int] = []
    n_cells = 0
    splitters = sorted(dmp_design.splitters, key=_splitter_sort_key)

    capacity = min(len(_TOPOLOGY_ROWS), _MAX_TOPOLOGY_CHARTS)
    if len(splitters) > capacity:
        raise ValueError(
            f"{len(splitters)} splitters, but the door chart template holds at most "
            f"{capacity} (Master rows {_TOPOLOGY_ROWS.start}-{_TOPOLOGY_ROWS.stop - 1}, "
            f"{_MAX_TOPOLOGY_CHARTS} chart slots). Refusing to drop any silently.")

    for row, s in zip(_TOPOLOGY_ROWS, splitters):
        filled_rows.append(row)

        # Col A — the real splitter ID, replacing the template's placeholder slot
        master[f"A{row}"] = s.id

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

    # Clear the template's leftover placeholder slot IDs below the real data, so
    # Master shows this job's topology and nothing else.
    for row in _TOPOLOGY_ROWS[len(splitters):]:
        master[f"A{row}"] = None

    return filled_rows, n_cells


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
    lx_filled_rows, n_splitter_cells = _populate_splitter_topology(master, dmp_design)
    n_splitters = len(lx_filled_rows)

    # openpyxl's wb.save strips parts it doesn't natively understand — including
    # xl/drawings/* and xl/media/* (the LAUSD + ConvergeOne logos). Save openpyxl's
    # output to a temp file, then start from a binary template copy and overlay
    # only the parts we need:
    #   - sheet1.xml (Header) and sheet2.xml (Master) — the two sheets we modify
    #   - workbook.xml — carries fullCalcOnLoad=True so Excel recomputes on open
    #     (sheets 3-6 are formula sheets like '=Master!B5'; without recompute
    #     they show stale cached values from the template's last save)
    #
    # Sheets 3-6 (Terminal Cans, RSPs, Power Supplies, LX-KP-710s) have
    # <tablePart> + <drawing> references whose rels point at template files —
    # beyond the surgical edits below we leave their XML untouched to avoid
    # desync (which causes "repair file").
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
    #   sheet3 = Terminal Cans, sheet4 = RSPs, sheet5 = Power Supplies, sheet6 = LX-KP-710s
    PRESENTATION_REWRITERS = {
        "xl/worksheets/sheet3.xml": lambda x: _retarget_zone_tab(x, "tc", dmp_design),
        "xl/worksheets/sheet4.xml": lambda x: _retarget_zone_tab(x, "rsps", dmp_design),
        "xl/worksheets/sheet5.xml": lambda x: _retarget_power_supplies(x, dmp_design),
        "xl/worksheets/sheet6.xml": lambda x: _consolidate_lx(x, lx_filled_rows),
    }

    # Consolidation drop plan: read the template's sheet rels to find each sheet's
    # Excel Tables and drawing part, then mark every table that lies entirely past its
    # sheet's cutoff. A dropped table must vanish from four places atomically — the zip
    # part, the sheet's rels, its <tablePart> element, and [Content_Types].xml — or
    # Excel flags the file for repair.
    chart_counts = {
        # TC/RSPs blocks fill in visual order (block i = RSP i+1); PS charts sit at
        # their module NUMBER's slot; LX slots were compacted to the filled count.
        "xl/worksheets/sheet3.xml": len(dmp_design.rsps),
        "xl/worksheets/sheet4.xml": len(dmp_design.rsps),
        "xl/worksheets/sheet5.xml": max((r.number for r in dmp_design.rsps), default=0),
        "xl/worksheets/sheet6.xml": n_splitters,
    }
    cutoffs: dict[str, int] = {}                  # sheet part -> last row to keep
    drop_parts: set[str] = set()                  # xl/tables/tableNN.xml parts to omit
    sheet_drop_rids: dict[str, set[str]] = {}     # sheet part -> tablePart rIds to remove
    rels_drop_names: dict[str, set[str]] = {}     # rels part -> table basenames to remove
    drawing_cutoffs: dict[str, int] = {}          # drawing part -> its sheet's cutoff
    with zipfile.ZipFile(template_path) as ztpl:
        for sheet_part, n_charts in chart_counts.items():
            cutoff = _sheet_cutoff(ztpl.read(sheet_part).decode("utf-8"),
                                   sheet_part, n_charts)
            cutoffs[sheet_part] = cutoff
            rels_part = sheet_part.replace("worksheets/", "worksheets/_rels/") + ".rels"
            for rm in _re.finditer(r"<Relationship\b[^>]*?/>",
                                   ztpl.read(rels_part).decode("utf-8")):
                rid = _re.search(r'Id="(rId\d+)"', rm.group(0)).group(1)
                target = _re.search(r'Target="([^"]+)"', rm.group(0)).group(1)
                name = target.rsplit("/", 1)[-1]
                if "/tables/" in target:
                    ref = _re.search(r'<table [^>]*?\bref="[A-Z]+(\d+):[A-Z]+(\d+)"',
                                     ztpl.read("xl/tables/" + name).decode("utf-8"))
                    lo, hi = sorted((int(ref.group(1)), int(ref.group(2))))
                    if lo > cutoff:
                        drop_parts.add("xl/tables/" + name)
                        sheet_drop_rids.setdefault(sheet_part, set()).add(rid)
                        rels_drop_names.setdefault(rels_part, set()).add(name)
                    else:
                        assert hi <= cutoff, \
                            f"table {name} ({lo}:{hi}) straddles cutoff {cutoff} on {sheet_part}"
                elif "/drawings/" in target:
                    drawing_cutoffs["xl/drawings/" + name] = cutoff

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
                if item in DROP_FILES or item in drop_parts:
                    continue
                if item == "[Content_Types].xml":
                    # Drop the calcChain Override declaration (otherwise Excel
                    # complains about a missing part). Use [^>]*? to span attribute
                    # values containing slashes (e.g. ContentType MIME values).
                    # Same for every table part dropped by consolidation.
                    ct = zin.read(item).decode("utf-8")
                    ct = _re.sub(
                        r'<Override\s+PartName="/xl/calcChain\.xml"[^>]*?/>',
                        "",
                        ct,
                    )
                    for part in drop_parts:
                        ct = _re.sub(
                            r'<Override\s+PartName="/%s"[^>]*?/>' % _re.escape(part),
                            "",
                            ct,
                        )
                    zout.writestr(item, ct.encode("utf-8"))
                    continue
                if item in rels_drop_names:
                    rels = zin.read(item).decode("utf-8")
                    rels = _drop_rels(rels, rels_drop_names[item])
                    zout.writestr(item, rels.encode("utf-8"))
                    continue
                if item in drawing_cutoffs:
                    xml = zin.read(item).decode("utf-8")
                    xml = _strip_drawing_anchors(xml, drawing_cutoffs[item])
                    zout.writestr(item, xml.encode("utf-8"))
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
                    xml = PRESENTATION_REWRITERS[item](xml)
                    xml = _drop_table_parts(xml, sheet_drop_rids.get(item, set()))
                    xml = _truncate_rows(xml, cutoffs[item])
                    zout.writestr(item, xml.encode("utf-8"))
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
