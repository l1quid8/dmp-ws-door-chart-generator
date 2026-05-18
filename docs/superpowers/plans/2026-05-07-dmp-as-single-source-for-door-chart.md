# DMP as Single Source for Door Chart — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Re-wire the door chart pipeline so the freshly-generated DMP worksheet is its sole input. Eliminate PDF re-parsing on the door-chart side.

**Architecture:** The DMP worksheet becomes the single source of truth. `parse_dmp_worksheet` extends to read zones from the Master sheet, fixes existing bugs (splitter ID matching, xr550_location cell), and delivers a complete `DMPDesign` object. `inject_door_chart` accepts only `DMPDesign` (drops `ParsedDesign`). A new thin CLI `generate_door_chart.py` replaces `run_pipeline.py`. The DMP generator script is renamed `generate_dmp_ws.py` to clarify its role.

**Tech Stack:** Python 3, openpyxl, PyMuPDF (only on the DMP-generation side, untouched by this plan). No tests framework in this repo — verification is by Python REPL one-liners against the two known-good PDFs in `input/` (Academy + O'Melveny).

**Spec:** `docs/superpowers/specs/2026-05-07-dmp-as-single-source-for-door-chart-design.md`

**Project context:**
- Not a git repo — no commits between tasks; verify after each task instead.
- Use the venv: `./venv/bin/python ...`.
- Two reference PDFs:
  - `input/O'MELVENY ES INTRUSION DESIGN 5-04-26_searchable.pdf`
  - `input/Academy_Enrichment_Science_2026-05-06_searchable.pdf`
- Both have generated DMPs already in `output/`:
  - `output/O_MELVENY_ELEMENTARY_SCHOOL_dmp_2026-05-06.xlsx`
  - `output/THE_ACADEMY_OF_ENRICHED_SCIENCES_dmp_2026-05-06.xlsx`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `scripts/parse_dmp_worksheet.py` | Modify | Canonical DMP reader. Add `Zone` dataclass + zone parsing. Fix splitter ID matching. Fix xr550 cell reference. |
| `scripts/inject_door_chart.py` | Modify | Door chart writer. Drop `ParsedDesign` import; consume only `DMPDesign`. Refactor RSP-location formatters to operate on `RSP` objects. |
| `scripts/generate_door_chart.py` | Create | Thin CLI: `python scripts/generate_door_chart.py <dmp_xlsx>` → produces door chart. |
| `scripts/generate_dmp.py` → `scripts/generate_dmp_ws.py` | Rename | Update in-file docstring's CLI usage. No internal logic changes. |
| `scripts/run_pipeline.py` | Delete | Superseded by the two-step `generate_dmp_ws` → `generate_door_chart` flow. |

---

## Task 1: Capture golden door chart for regression check

Before changing anything, run the existing pipeline once for O'Melveny and save the output as a regression baseline. Subsequent tasks compare against this.

**Files:**
- Create: `output/_golden/O_MELVENY_ELEMENTARY_SCHOOL_door_chart_GOLDEN.xlsx`

- [ ] **Step 1: Generate the golden door chart using the existing pipeline**

Run:
```bash
cd "/Users/tylercaldwell/Documents/Claude/Projects/C1 Intrusion Door Chart Script Build"
./venv/bin/python scripts/run_pipeline.py "input/O'MELVENY ES INTRUSION DESIGN 5-04-26_searchable.pdf" --skip-ocr
```

This produces `output/<name>_door_chart_<date>.xlsx`. (The script auto-detects the existing hand-filled `input/O'melveny DMP Worksheet.xlsx` to feed splitter topology.)

- [ ] **Step 2: Move the produced file into a golden directory**

```bash
mkdir -p output/_golden
mv output/O_MELVENY_ELEMENTARY_SCHOOL_door_chart_*.xlsx output/_golden/O_MELVENY_ELEMENTARY_SCHOOL_door_chart_GOLDEN.xlsx
```

Verify the file exists:
```bash
ls -la output/_golden/
```
Expected: file named `O_MELVENY_ELEMENTARY_SCHOOL_door_chart_GOLDEN.xlsx`.

- [ ] **Step 3: Snapshot the load-bearing cells of the golden file**

```bash
./venv/bin/python -c "
import openpyxl
wb = openpyxl.load_workbook('output/_golden/O_MELVENY_ELEMENTARY_SCHOOL_door_chart_GOLDEN.xlsx', data_only=True)
m = wb['Master']
print('B3:', m['B3'].value)            # MSP location
print('B8:', m['B8'].value)            # Keypad bus first KP location
print('B11:', m['B11'].value)          # LX500 first splitter location
# Splitter rows
for r in [29, 30, 31, 35]:             # check first few slot rows
    row = [m.cell(row=r, column=c).value for c in range(1, 8)]
    print(f'row {r}:', row)
# Zone rows
for r in [67, 68, 70, 81]:
    row = [m.cell(row=r, column=c).value for c in range(2, 5)]
    print(f'zone row {r}:', row)
print('Header B3:', wb['Header']['B3'].value)
" > output/_golden/golden_snapshot.txt
cat output/_golden/golden_snapshot.txt
```

Expected: non-empty values for B3 ("ADMIN BLDG..." or similar), B8/B11 populated. Splitter rows have IDs like `710-LX500-1` (or `LX-710-1` for the legacy hand-filled DMP), locations, etc. Zone rows have descriptions, RSP locations, 66 block locations.

This snapshot is what we re-verify against in Task 11.

---

## Task 2: Fix splitter ID matching in DMP parser

Current parser doesn't recognize `710-LX500-N` / `710-KP-N` (the IA-diagram format the generator writes), so it returns 0 splitters when reading a generated DMP. Extend the startswith checks.

**Files:**
- Modify: `scripts/parse_dmp_worksheet.py:191` (`_parse_kp_splitters`)
- Modify: `scripts/parse_dmp_worksheet.py:233` (`_parse_lx_splitters`)

- [ ] **Step 1: Verify the failure first**

```bash
./venv/bin/python -c "
import sys; sys.path.insert(0, 'scripts')
from parse_dmp_worksheet import parse_dmp_worksheet
d = parse_dmp_worksheet('output/THE_ACADEMY_OF_ENRICHED_SCIENCES_dmp_2026-05-06.xlsx')
print('splitters:', len(d.splitters))
"
```
Expected: `splitters: 0` (the bug).

- [ ] **Step 2: Extend KP splitter ID matching**

In `scripts/parse_dmp_worksheet.py`, find this line in `_parse_kp_splitters`:
```python
        # Device ID line (e.g., "KP-710-1", "KP 710-1")
        if id_text.startswith("KP-") or id_text.startswith("KP "):
```

Replace with:
```python
        # Device ID line — accept legacy ("KP-710-1", "KP 710-1") and IA-diagram ("710-KP-1") formats
        if id_text.startswith("KP-") or id_text.startswith("KP ") or id_text.startswith("710-KP"):
```

- [ ] **Step 3: Extend LX splitter ID matching**

In `_parse_lx_splitters`, find:
```python
        # Device ID line (e.g., "LX 710-1", "LX-710-1")
        if id_text.startswith("LX ") or id_text.startswith("LX-"):
```

Replace with:
```python
        # Device ID line — accept legacy ("LX-710-1", "LX 710-1") and IA-diagram ("710-LX500-1") formats
        if id_text.startswith("LX ") or id_text.startswith("LX-") or id_text.startswith("710-LX"):
```

- [ ] **Step 4: Verify splitters now parse on both generated DMPs**

```bash
./venv/bin/python -c "
import sys; sys.path.insert(0, 'scripts')
from parse_dmp_worksheet import parse_dmp_worksheet
for p in ['output/THE_ACADEMY_OF_ENRICHED_SCIENCES_dmp_2026-05-06.xlsx',
          'output/O_MELVENY_ELEMENTARY_SCHOOL_dmp_2026-05-06.xlsx']:
    d = parse_dmp_worksheet(p)
    print(f'{p}:')
    print(f'  splitters: {len(d.splitters)}')
    for s in d.splitters:
        print(f'    {s.id!r} type={s.splitter_type} loc={s.location!r} outs={s.outputs}')
"
```

Expected for Academy: 2 LX splitters (`710-LX500-1`, `710-LX500-2`) + 2 KP splitters (`710-KP-1`, `710-KP-2`), each with locations like `'ADMIN BUILDING'` / `'CLASSROOM  BUILDING E'` and 3 outputs each.

Expected for O'Melveny: 2 LX splitters + 1 KP splitter.

---

## Task 3: Fix xr550_location column index in DMP parser

The parser currently reads `row[4]` (column E) of the `DMP XR550` sheet's location row, but the value lives in column D (`row[3]`). Fix the index.

**Files:**
- Modify: `scripts/parse_dmp_worksheet.py:163-173` (`_parse_xr550_location`)

- [ ] **Step 1: Verify the failure first**

```bash
./venv/bin/python -c "
import sys; sys.path.insert(0, 'scripts')
from parse_dmp_worksheet import parse_dmp_worksheet
d = parse_dmp_worksheet('output/THE_ACADEMY_OF_ENRICHED_SCIENCES_dmp_2026-05-06.xlsx')
print('xr550_location:', d.site_info.xr550_location)
"
```
Expected: `xr550_location: None` (the bug — value is in column D not E).

- [ ] **Step 2: Fix the column index**

In `scripts/parse_dmp_worksheet.py`, find the function `_parse_xr550_location`:
```python
def _parse_xr550_location(ws) -> Optional[str]:
    """Extract XR550 location from DMP XR550 sheet."""
    for row in ws.iter_rows(min_row=1, max_row=10, values_only=True):
        if not row:
            continue
        # Look for "DMP XR550  #" row which has location in column E
        if row[0] and "DMP XR550" in str(row[0]):
            location = row[4] if len(row) > 4 else None
            if location:
                return str(location).strip()
    return None
```

Replace with:
```python
def _parse_xr550_location(ws) -> Optional[str]:
    """Extract XR550 location from DMP XR550 sheet (cell D4)."""
    for row in ws.iter_rows(min_row=1, max_row=10, values_only=True):
        if not row:
            continue
        # The "DMP XR550  #" row carries the panel location in column D (index 3).
        if row[0] and "DMP XR550" in str(row[0]):
            location = row[3] if len(row) > 3 else None
            if location:
                return str(location).strip()
    return None
```

- [ ] **Step 3: Verify the location is now extracted**

```bash
./venv/bin/python -c "
import sys; sys.path.insert(0, 'scripts')
from parse_dmp_worksheet import parse_dmp_worksheet
for p in ['output/THE_ACADEMY_OF_ENRICHED_SCIENCES_dmp_2026-05-06.xlsx',
          'output/O_MELVENY_ELEMENTARY_SCHOOL_dmp_2026-05-06.xlsx']:
    d = parse_dmp_worksheet(p)
    print(f'{p}: xr550_location={d.site_info.xr550_location!r}')
"
```

Expected: both DMPs return non-None location strings (Academy: `'ADMIN BUILDING'`).

---

## Task 4: Add Zone dataclass + parse zones from Master sheet

Add a new `Zone` dataclass and a `_parse_master_zones` function that reads the DMP's `Master` sheet (cols A/B, rows 2 onward) and derives the `is_spare` / `is_ps_ac` / `is_ps_batt` / `rsp_number` flags from the zone description text.

**Files:**
- Modify: `scripts/parse_dmp_worksheet.py` — add `Zone` dataclass, add `_parse_master_zones`, wire it in `parse_dmp_worksheet`, extend `DMPDesign`.

- [ ] **Step 1: Add the Zone dataclass**

Locate the existing `ZoneInfo` dataclass at line 73-79 (used for point-info sheets — different purpose, keep it). Right after it (around line 80), add a new dataclass:

```python
@dataclass
class Zone:
    """A zone row from the DMP's Master sheet (the door chart's source for zone descriptions).

    Distinct from ZoneInfo (which is sourced from the per-RSP point-info sheets).
    """
    number: int                              # e.g. 501
    description: str                         # e.g. 'CLASSROOM 1', 'SPARE', 'PS-2: A/C LOSS'
    rsp_number: Optional[int] = None         # 1..N for normal/PS rows; None for SPARE
    is_spare: bool = False
    is_ps_ac: bool = False
    is_ps_batt: bool = False
```

- [ ] **Step 2: Extend DMPDesign with a `master_zones` field**

In the `DMPDesign` dataclass (currently at line 82-89), add a new field:

```python
@dataclass
class DMPDesign:
    site_info: SiteInfo = field(default_factory=SiteInfo)
    splitters: list[Splitter] = field(default_factory=list)
    rsps: list[RSP] = field(default_factory=list)
    keypads: list[Keypad] = field(default_factory=list)
    power_supplies: list[PowerSupply] = field(default_factory=list)
    zones: list[ZoneInfo] = field(default_factory=list)
    master_zones: list[Zone] = field(default_factory=list)   # NEW: from Master sheet
```

(Keep the existing `zones` field — it's populated from point-info sheets and may be used elsewhere. The new `master_zones` field is what `inject_door_chart` will consume.)

- [ ] **Step 3: Add the parser function**

Add this function at the bottom of the parsing helpers section (e.g., after `_parse_point_info` around line 386):

```python
import re as _re_for_zones  # local to avoid touching imports at top of file

_PS_AC_RE = _re_for_zones.compile(r"^PS-(\d+):\s*A/?C", _re_for_zones.IGNORECASE)
_PS_BATT_RE = _re_for_zones.compile(r"^PS-(\d+):\s*BATT", _re_for_zones.IGNORECASE)


def _parse_master_zones(ws, rsps: list[RSP]) -> list[Zone]:
    """Read zone rows from the DMP's Master sheet.

    Layout: A1='ZONE #', B1='ZONE DESCRIPTION', then zone rows from row 2:
      A col: 'Z501'..'Z980'
      B col: room name / 'SPARE' / 'PS-N: A/C LOSS' / 'PS-N: BATT. TRBL'

    rsp_number for non-supervisory zones is derived from the RSP zone ranges
    (which were already parsed from 'DMP 714 Exp Mod'). For PS rows it comes
    from the 'PS-N:' prefix directly.
    """
    out: list[Zone] = []
    # Build {zone_num: rsp_num} lookup from RSP zone ranges
    zone_to_rsp: dict[int, int] = {}
    for r in rsps:
        for z in r.zones:
            zone_to_rsp[z] = r.number

    for row in ws.iter_rows(min_row=2, max_row=ws.max_row, values_only=True):
        if not row or not row[0]:
            continue
        z_label = str(row[0]).strip()
        if not (z_label.startswith("Z") and z_label[1:].isdigit()):
            continue
        zone_num = int(z_label[1:])

        desc = str(row[1]).strip() if len(row) > 1 and row[1] is not None else ""
        if not desc:
            continue

        is_spare = desc.upper() == "SPARE"
        m_ac = _PS_AC_RE.match(desc)
        m_batt = _PS_BATT_RE.match(desc)
        is_ps_ac = bool(m_ac)
        is_ps_batt = bool(m_batt)

        if is_ps_ac:
            rsp_number = int(m_ac.group(1))
        elif is_ps_batt:
            rsp_number = int(m_batt.group(1))
        elif is_spare:
            rsp_number = None
        else:
            rsp_number = zone_to_rsp.get(zone_num)

        out.append(Zone(
            number=zone_num,
            description=desc,
            rsp_number=rsp_number,
            is_spare=is_spare,
            is_ps_ac=is_ps_ac,
            is_ps_batt=is_ps_batt,
        ))
    return out
```

- [ ] **Step 4: Wire the parser into `parse_dmp_worksheet`**

In `parse_dmp_worksheet` (around line 122-126, after RSPs are parsed), add the Master-sheet parsing call. Find the section that ends with the 714 Exp Mod loop:

```python
    # 714-16 / 714-08 expansion modules ARE the RSPs — module# = RSP#, point range = zones
    for sheet_name in wb.sheetnames:
        if sheet_name.strip() == "DMP 714 Exp Mod":
            design.rsps = _parse_rsps(wb[sheet_name])
            break
```

Right after that block, add:

```python
    if "Master" in wb.sheetnames:
        design.master_zones = _parse_master_zones(wb["Master"], design.rsps)
```

- [ ] **Step 5: Verify Zone parsing on both generated DMPs**

```bash
./venv/bin/python -c "
import sys; sys.path.insert(0, 'scripts')
from parse_dmp_worksheet import parse_dmp_worksheet
for p in ['output/THE_ACADEMY_OF_ENRICHED_SCIENCES_dmp_2026-05-06.xlsx',
          'output/O_MELVENY_ELEMENTARY_SCHOOL_dmp_2026-05-06.xlsx']:
    d = parse_dmp_worksheet(p)
    n_total = len(d.master_zones)
    n_spare = sum(1 for z in d.master_zones if z.is_spare)
    n_ac = sum(1 for z in d.master_zones if z.is_ps_ac)
    n_batt = sum(1 for z in d.master_zones if z.is_ps_batt)
    n_room = n_total - n_spare - n_ac - n_batt
    print(f'{p}:')
    print(f'  total={n_total} room={n_room} spare={n_spare} ps_ac={n_ac} ps_batt={n_batt}')
    # Show first 5 of each kind
    for z in [z for z in d.master_zones if z.is_ps_ac][:2]:
        print(f'  ps_ac sample: Z{z.number} desc={z.description!r} rsp={z.rsp_number}')
    for z in [z for z in d.master_zones if not (z.is_spare or z.is_ps_ac or z.is_ps_batt)][:3]:
        print(f'  room sample: Z{z.number} desc={z.description!r} rsp={z.rsp_number}')
"
```

Expected: both DMPs return non-zero counts. PS rows have `rsp_number` matching the `PS-N:` prefix. Room rows have `rsp_number` matching the RSP that owns the zone range. Spares have `rsp_number=None`.

---

## Task 5: Update spec note in `parse_dmp_worksheet.py` CLI section

Add `master_zones` to the `__main__` block so `python parse_dmp_worksheet.py <xlsx>` shows the new field's contents (helpful for hand-debugging the parser later).

**Files:**
- Modify: `scripts/parse_dmp_worksheet.py:391-428` (the `if __name__ == "__main__"` block)

- [ ] **Step 1: Append a master_zones print loop**

After the existing zones print (around line 428), add:

```python
    print(f"\n=== Master Zones ({len(design.master_zones)}) ===")
    for z in design.master_zones[:15]:
        flags = []
        if z.is_spare: flags.append("SPARE")
        if z.is_ps_ac: flags.append("PS_AC")
        if z.is_ps_batt: flags.append("PS_BATT")
        flag_str = f" [{','.join(flags)}]" if flags else ""
        print(f"  Z{z.number}: {z.description!r} rsp={z.rsp_number}{flag_str}")
    if len(design.master_zones) > 15:
        print(f"  ... ({len(design.master_zones) - 15} more)")
```

- [ ] **Step 2: Verify CLI output**

```bash
./venv/bin/python scripts/parse_dmp_worksheet.py output/THE_ACADEMY_OF_ENRICHED_SCIENCES_dmp_2026-05-06.xlsx | tail -25
```

Expected: a `=== Master Zones (...) ===` section with at least 15 entries, mixing room names, SPARE, PS_AC, PS_BATT.

---

## Task 6: Refactor `inject_door_chart.py` — drop `ParsedDesign`, consume only `DMPDesign`

Major refactor. The `inject` function takes a single `DMPDesign` arg. RSP-location helpers operate on `RSP` objects. The zone-area population reads from `dmp_design.master_zones`. Header sheet reads `dmp_design.site_info.school_name`.

The slot-to-splitter mapping (`_build_slot_to_splitter_map`) is also simplified — since both the door chart template's slot IDs and the new DMP's splitter IDs use the IA-diagram format (`710-LX500-N`, `710-KP-N`), they match by direct ID equality. No more bus-resolution walk.

**Files:**
- Modify: `scripts/inject_door_chart.py` (substantial refactor)

- [ ] **Step 1: Replace the imports and remove `ParsedDesign` types**

At the top of `scripts/inject_door_chart.py`, find:
```python
from parse_zone_schedule import ParsedDesign, ZoneRecord, CombusLine
from parse_dmp_worksheet import DMPDesign, Splitter
```

Replace with:
```python
from parse_dmp_worksheet import DMPDesign, Splitter, RSP, Zone
```

- [ ] **Step 2: Replace the RSP-location helpers**

Find the existing helpers (around line 48-74):
```python
def find_combus_line(combus: list[CombusLine], kind: str, n: int) -> Optional[CombusLine]:
    for cl in combus:
        if cl.kind == kind and cl.n == n:
            return cl
    return None


def format_rsp_location(cl: CombusLine) -> str:
    """Build a location string like 'ADMIN BLDG - A/V ROOM - Remote Service Panel #1'."""
    return f"{cl.building} - {cl.room} - Remote Service Panel #{cl.n}"


def format_66_block_location(cl: CombusLine) -> str:
    """Build a 66 BLOCK location string."""
    return f"{cl.building} - {cl.room} - Main SecTC 66 Block(s) - RSP {cl.n}"


def format_zone_description(z: ZoneRecord) -> str:
    """The visible zone description that goes in Master col B."""
    if z.is_spare:
        return "SPARE"
    if z.is_ps_ac:
        return f"PS-{z.rsp}: A/C LOSS"
    if z.is_ps_batt:
        return f"PS-{z.rsp}: BATT. TRBL."
    # Regular zone — use the room/area as the description
    return z.room or ""
```

Replace the entire block with:
```python
def _find_rsp(rsps: list[RSP], number: int) -> Optional[RSP]:
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
```

(`format_zone_description` is gone — `dmp_design.master_zones[i].description` is already the pre-formatted string from the DMP. We use it verbatim.)

- [ ] **Step 3: Simplify `_build_slot_to_splitter_map`**

Find the existing function (around line 139-177). Replace the entire function body with:

```python
def _build_slot_to_splitter_map(dmp_design: DMPDesign) -> dict[str, Splitter]:
    """Map template slot IDs to DMP splitters by direct ID equality.

    Both the door chart template's column-A slot IDs and the DMP's splitter
    IDs follow the IA-diagram convention ('710-LX500-N', '710-KP-N'), so
    the mapping is now trivial — no bus-resolution walk needed.
    """
    return {s.id: s for s in dmp_design.splitters}
```

The helpers `_normalize_lx_id` and `_resolve_lx_buses` are no longer called anywhere. Delete them (around lines 85-136). Also delete the regex constants `_BUS_RE`, `_FROM_LX_RE`, `_KP_NUM_RE`, `_LX_NUM_RE` (around line 79-82) — they're no longer referenced. Keep the `import re` at the top since it's still used by `_slugify`.

- [ ] **Step 4: Update `_populate_xr550_config` to use direct IDs**

Find `_populate_xr550_config` (around line 180-226). The bus-row logic relies on `_resolve_lx_buses` (which we're deleting). Replace the bus-resolution block with direct ID parsing. The full new function:

```python
_LX500_BUS_RE = re.compile(r"^710-LX(\d{3})-(\d+)$")


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
```

- [ ] **Step 5: Rewrite the `inject` function**

Find the `inject` function (around line 279-361). Replace its entire body with:

```python
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

    # Copy template -> output, then open the copy
    shutil.copy2(template_path, output_path)
    wb = openpyxl.load_workbook(output_path)

    # Force recalc on next Excel open so all formulas refresh
    wb.calculation = CalcProperties(fullCalcOnLoad=True)

    # 1. Header sheet — school name only (address fields not currently extracted)
    header = wb["Header"]
    if dmp_design.site_info.school_name:
        header["B3"] = dmp_design.site_info.school_name

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

    wb.save(output_path)
    print(f"Injected: {n_rooms} rooms, {n_spares} spares, {n_ps} PS supervisory rows")
    print(f"  XR-550 CONFIG: {n_xr550_cells} cells populated")
    print(f"  SPLITTER TOPOLOGY: {n_splitters} splitters ({n_splitter_cells} cells populated)")
    print(f"Output: {output_path}")
```

- [ ] **Step 6: Update the CLI `__main__` block**

Find the `if __name__ == "__main__":` block at the bottom (around line 373-393). Replace with:

```python
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
```

- [ ] **Step 7: Verify `inject_door_chart.py` is syntactically valid + runs**

```bash
./venv/bin/python -c "
import sys; sys.path.insert(0, 'scripts')
import inject_door_chart
print('OK — module loads')
print('inject signature:', inject_door_chart.inject.__doc__.splitlines()[0] if inject_door_chart.inject.__doc__ else 'no doc')
"
```
Expected: `OK — module loads`.

Then test the CLI directly:
```bash
./venv/bin/python scripts/inject_door_chart.py \
    output/THE_ACADEMY_OF_ENRICHED_SCIENCES_dmp_2026-05-06.xlsx \
    door_chart_template_blank.xlsx \
    output
```

Expected: prints injection summary (e.g. `Injected: ~50 rooms, ~25 spares, 6 PS supervisory rows`, `SPLITTER TOPOLOGY: 4 splitters`). A new `output/THE_ACADEMY_OF_ENRICHED_SCIENCES_door_chart_2026-05-07.xlsx` is created.

- [ ] **Step 8: Spot-check the generated Academy door chart**

```bash
./venv/bin/python -c "
import openpyxl
wb = openpyxl.load_workbook('output/THE_ACADEMY_OF_ENRICHED_SCIENCES_door_chart_2026-05-07.xlsx', data_only=False)
m = wb['Master']
print('B3:', m['B3'].value)
print('B8:', m['B8'].value)
print('B11:', m['B11'].value)
# splitter rows
for r in [29, 30, 31, 32, 33]:
    row = [m.cell(row=r, column=c).value for c in range(1, 8)]
    print(f'splitter row {r}:', row)
# zone rows
for r in [67, 68, 81, 82, 96]:
    row = [m.cell(row=r, column=c).value for c in range(2, 5)]
    print(f'zone row {r}:', row)
print('Header B3:', wb['Header']['B3'].value)
"
```

Expected:
- `B3` = `'ADMIN BUILDING'`
- `B8` = `'ADMIN BUILDING'` (KP-1 location)
- `B11` = `'ADMIN BUILDING'` (LX500-1 location)
- Splitter rows: slot ID in col A, location in col B, COMBUS input/outputs in cols C/D/E/F/G
- Zone rows have descriptions, RSP locations like `'ADMIN BLDG A 1ST FLR OFFICE ROOM 16 - Remote Service Panel #1'`, 66 block locations
- `Header B3` = `'THE ACADEMY OF ENRICHED SCIENCES'`

---

## Task 7: Create `scripts/generate_door_chart.py`

Thin CLI wrapper. Takes the DMP xlsx path; loads, injects, saves.

**Files:**
- Create: `scripts/generate_door_chart.py`

- [ ] **Step 1: Write the script**

```python
"""
Generates a C1 door chart from a DMP worksheet (xlsx).

The DMP must have already been produced by generate_dmp_ws.py from the design PDF.
The DMP is the single source of truth for the door chart — zones, RSPs, keypads,
splitters, and site info all flow through it.

Usage:
    python scripts/generate_door_chart.py <dmp_xlsx>
        [--template <door_chart_template_blank.xlsx>]
        [--output-dir <dir>]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# Make sibling modules importable when run from project root
sys.path.insert(0, str(Path(__file__).parent))

from inject_door_chart import inject, _slugify  # noqa: E402
from parse_dmp_worksheet import parse_dmp_worksheet  # noqa: E402


PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_TEMPLATE = PROJECT_ROOT / "door_chart_template_blank.xlsx"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"


def main():
    ap = argparse.ArgumentParser(
        description="Generate C1 door chart from a generated DMP worksheet."
    )
    ap.add_argument("dmp", help="Path to the DMP worksheet xlsx (produced by generate_dmp_ws.py).")
    ap.add_argument("--template", default=str(DEFAULT_TEMPLATE),
                    help="Path to blank door chart template.")
    ap.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR),
                    help="Output directory.")
    args = ap.parse_args()

    dmp_path = Path(args.dmp).resolve()
    if not dmp_path.exists():
        sys.exit(f"DMP worksheet not found: {dmp_path}")

    template_path = Path(args.template).resolve()
    if not template_path.exists():
        sys.exit(f"Template not found: {template_path}")

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(exist_ok=True)

    print(f"[1/2] Parsing DMP worksheet: {dmp_path.name}")
    dmp = parse_dmp_worksheet(dmp_path)
    print(f"      School: {dmp.site_info.school_name or '?'}")
    print(f"      Splitters: {len(dmp.splitters)}, RSPs: {len(dmp.rsps)}, "
          f"Keypads: {len(dmp.keypads)}, Master zones: {len(dmp.master_zones)}")

    print(f"[2/2] Injecting into door chart...")
    school = dmp.site_info.school_name or "OUTPUT"
    out_name = f"{_slugify(school)}_door_chart_{date.today().isoformat()}.xlsx"
    out_path = output_dir / out_name
    inject(template_path, dmp, out_path)

    print(f"\n✓ Done. Chart written to: {out_path}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Run it on the Academy DMP**

```bash
./venv/bin/python scripts/generate_door_chart.py output/THE_ACADEMY_OF_ENRICHED_SCIENCES_dmp_2026-05-06.xlsx
```

Expected: prints the [1/2] parse summary, the [2/2] injection summary, then `✓ Done. Chart written to: output/THE_ACADEMY_OF_ENRICHED_SCIENCES_door_chart_2026-05-07.xlsx`.

- [ ] **Step 3: Run it on the O'Melveny DMP**

```bash
./venv/bin/python scripts/generate_door_chart.py output/O_MELVENY_ELEMENTARY_SCHOOL_dmp_2026-05-06.xlsx
```

Expected: same flow, produces `output/O_MELVENY_ELEMENTARY_SCHOOL_door_chart_2026-05-07.xlsx`.

---

## Task 8: Rename `generate_dmp.py` → `generate_dmp_ws.py`

**Files:**
- Rename: `scripts/generate_dmp.py` → `scripts/generate_dmp_ws.py`
- Modify: in-file docstring's CLI usage line

- [ ] **Step 1: Rename the file**

```bash
mv scripts/generate_dmp.py scripts/generate_dmp_ws.py
```

- [ ] **Step 2: Update the docstring's CLI usage**

The current docstring (line 8) reads:
```
    python scripts/generate_dmp.py <design.pdf> [--searchable <path>] [--output <path>] [--non-interactive]
```

In `scripts/generate_dmp_ws.py`, update that line to:
```
    python scripts/generate_dmp_ws.py <design.pdf> [--searchable <path>] [--output <path>] [--non-interactive]
```

- [ ] **Step 3: Verify nothing else imports the old module name**

```bash
grep -rn "generate_dmp" scripts/ 2>/dev/null | grep -v __pycache__
```

Expected: no matches that reference `import generate_dmp` or `from generate_dmp` (the file is invoked as a CLI, not imported elsewhere). The only remaining reference should be inside `generate_dmp_ws.py` itself (its own docstring/CLI).

- [ ] **Step 4: Smoke-test the renamed script with a known PDF**

```bash
# Use --non-interactive flag if it exists; otherwise pipe canned answers.
printf '\n818-555-0100\nTyler Caldwell\n05/15/2026\n10.10.50.100\n10.10.50.1\n' | \
    ./venv/bin/python scripts/generate_dmp_ws.py "input/Academy_Enrichment_Science_2026-05-06_searchable.pdf"
```

Expected: produces `output/THE_ACADEMY_OF_ENRICHED_SCIENCES_dmp_2026-05-07.xlsx` (today's date). Check existence:
```bash
ls -la output/THE_ACADEMY_OF_ENRICHED_SCIENCES_dmp_*.xlsx
```

---

## Task 9: Delete `scripts/run_pipeline.py`

**Files:**
- Delete: `scripts/run_pipeline.py`

- [ ] **Step 1: Confirm no other file imports it**

```bash
grep -rn "run_pipeline" scripts/ docs/ 2>/dev/null | grep -v __pycache__ | grep -v "specs/" | grep -v "plans/"
```

Expected: no matches outside the spec/plan docs (which reference it historically).

- [ ] **Step 2: Delete the file**

```bash
rm scripts/run_pipeline.py
```

- [ ] **Step 3: Verify generate_door_chart still runs after deletion**

```bash
./venv/bin/python scripts/generate_door_chart.py output/THE_ACADEMY_OF_ENRICHED_SCIENCES_dmp_2026-05-06.xlsx
```

Expected: runs cleanly, produces door chart.

---

## Task 10: End-to-end fresh-pipeline run for both schools

Run the full new flow (PDF → DMP → door chart) for both reference PDFs.

- [ ] **Step 1: Regenerate DMPs from both PDFs**

```bash
printf '\n818-555-0100\nTyler Caldwell\n05/15/2026\n10.10.50.100\n10.10.50.1\n' | \
    ./venv/bin/python scripts/generate_dmp_ws.py "input/O'MELVENY ES INTRUSION DESIGN 5-04-26_searchable.pdf"

printf '\n818-555-0100\nTyler Caldwell\n05/15/2026\n10.10.50.100\n10.10.50.1\n' | \
    ./venv/bin/python scripts/generate_dmp_ws.py "input/Academy_Enrichment_Science_2026-05-06_searchable.pdf"
```

Expected: both produce `output/<school>_dmp_<today>.xlsx`.

- [ ] **Step 2: Generate door charts from both DMPs**

```bash
./venv/bin/python scripts/generate_door_chart.py output/O_MELVENY_ELEMENTARY_SCHOOL_dmp_$(date +%Y-%m-%d).xlsx
./venv/bin/python scripts/generate_door_chart.py output/THE_ACADEMY_OF_ENRICHED_SCIENCES_dmp_$(date +%Y-%m-%d).xlsx
```

Expected: both produce `output/<school>_door_chart_<today>.xlsx` cleanly.

- [ ] **Step 3: Spot-check both door charts**

```bash
./venv/bin/python -c "
import openpyxl
for f in ['output/O_MELVENY_ELEMENTARY_SCHOOL_door_chart_$(date +%Y-%m-%d).xlsx',
          'output/THE_ACADEMY_OF_ENRICHED_SCIENCES_door_chart_$(date +%Y-%m-%d).xlsx']:
    print(f'=== {f} ===')
    wb = openpyxl.load_workbook(f, data_only=False)
    m = wb['Master']
    print(f'  Header B3: {wb[\"Header\"][\"B3\"].value!r}')
    print(f'  Master B3: {m[\"B3\"].value!r}')
    print(f'  Master B8: {m[\"B8\"].value!r}')
    print(f'  Master B11: {m[\"B11\"].value!r}')
    # Find first non-empty splitter row
    for r in range(29, 50):
        a = m.cell(row=r, column=1).value
        b = m.cell(row=r, column=2).value
        if b:
            print(f'  Splitter slot row {r}: A={a!r} B={b!r}')
    # Find first zone with full data
    for r in range(67, 130):
        b = m.cell(row=r, column=2).value
        c = m.cell(row=r, column=3).value
        if b and c:
            print(f'  First populated zone row {r}: B={b!r} C={c!r} D={m.cell(row=r,column=4).value!r}')
            break
"
```

Expected output for both:
- Header B3 = full school name
- Master B3 / B8 / B11 = building location
- Splitter slot rows have IDs `710-LX500-1` etc. with locations and outputs
- First populated zone row has description + RSP location like `<bldg+room> - Remote Service Panel #1` + 66 block string

---

## Task 11: Compare against golden snapshot

Verify O'Melveny output is consistent with the golden snapshot from Task 1 on the load-bearing cells.

- [ ] **Step 1: Re-run the same snapshot script against the new O'Melveny output**

```bash
./venv/bin/python -c "
import openpyxl
wb = openpyxl.load_workbook('output/O_MELVENY_ELEMENTARY_SCHOOL_door_chart_$(date +%Y-%m-%d).xlsx', data_only=True)
m = wb['Master']
print('B3:', m['B3'].value)
print('B8:', m['B8'].value)
print('B11:', m['B11'].value)
for r in [29, 30, 31, 35]:
    row = [m.cell(row=r, column=c).value for c in range(1, 8)]
    print(f'row {r}:', row)
for r in [67, 68, 70, 81]:
    row = [m.cell(row=r, column=c).value for c in range(2, 5)]
    print(f'zone row {r}:', row)
print('Header B3:', wb['Header']['B3'].value)
" > output/_golden/new_pipeline_snapshot.txt

diff output/_golden/golden_snapshot.txt output/_golden/new_pipeline_snapshot.txt
```

Expected: differences are limited to splitter ID format (golden uses legacy `LX-710-N` if the existing hand-filled DMP was used; new uses `710-LX500-N`) — that's expected and intentional. School name, B3/B8/B11, RSP locations, zone descriptions, and 66 block strings should match (or be format-equivalent).

If there are unexpected diffs (missing zones, blank cells, etc.) — investigate before declaring done.

- [ ] **Step 2: Open both door charts in Excel for visual confirmation**

(This is a manual step — open in Excel.app.) Confirm:
- No "Excel found a problem... do you want to repair?" dialog
- Conditional formatting on PS supervisory zones (color highlight) is preserved
- Cell borders, fills, fonts on the Master sheet match the template
- All splitter rows show full IA-diagram IDs in column A (`710-LX500-1`, `710-KP-1`)
- PS supervisory rows show labels like `PS-1: A/C LOSS` in column B
- No `TBD` cells anywhere on the Master sheet

If the Excel "repair" dialog appears, that's a regression — investigate the openpyxl save flow before declaring done.

---

## Self-Review

**Spec coverage:**
- [x] DMP becomes single source for door chart (Tasks 6, 7)
- [x] DMPDesign extended with master_zones (Task 4)
- [x] xr550_location bug fixed (Task 3)
- [x] Splitter ID matching extended for new format (Task 2)
- [x] inject_door_chart drops ParsedDesign (Task 6)
- [x] generate_door_chart.py created (Task 7)
- [x] generate_dmp.py renamed to generate_dmp_ws.py (Task 8)
- [x] run_pipeline.py deleted (Task 9)
- [x] Both PDFs verified end-to-end (Task 10)
- [x] Regression snapshot diff against pre-change pipeline (Tasks 1, 11)

**Placeholder scan:** No "TBD"/"TODO"/"implement later" left in plan. All file paths and code blocks are concrete.

**Type consistency:**
- `Zone` dataclass added in Task 4; consumed in Task 6 step 5 (`for z in dmp_design.master_zones`).
- `RSP` dataclass already exists; helper functions `format_rsp_location(rsp: RSP)` etc. take it (Task 6 step 2).
- `master_zones` field name used consistently across Tasks 4, 5, 6, 7.
