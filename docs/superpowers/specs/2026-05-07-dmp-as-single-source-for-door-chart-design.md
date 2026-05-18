# Design: DMP worksheet as the single source for door chart generation

**Date:** 2026-05-07
**Status:** Approved, ready for implementation plan

## Goal

Restructure the door chart pipeline so the freshly-generated DMP worksheet is the sole input to door chart generation. The user's intended workflow:

```
blueprint.pdf ──> generate_dmp_ws ──> DMP_worksheet.xlsx     [user reviews]

DMP_worksheet.xlsx ──> generate_door_chart ──> door_chart.xlsx
```

The DMP becomes the authoritative artifact: a quality gate the user reviews before the door chart is produced. Any data correctness issue surfaces in the DMP review and is fixed upstream (in `parse_zone_schedule.py` / `generate_dmp_ws.py`), and the door chart inherits the corrections automatically.

## Why this change

- **Today:** `run_pipeline.py` reads the PDF twice — once for zones (via `parse_zone_schedule`), and optionally once for splitters (via `parse_dmp_worksheet`, against a pre-existing filled-in DMP). The freshly-generated DMP from `generate_dmp_ws.py` (currently named `generate_dmp.py`; renamed as part of this work) isn't part of the door chart pipeline at all.
- **Problem:** the two deliverables can diverge. A zone fix in one path doesn't propagate to the other.
- **Fix:** make the DMP the single source of truth for the door chart. PDF parsing lives entirely on the DMP-generation side.

## Architecture

```
blueprint.pdf
     │
     ▼
generate_dmp_ws.py     (PDF parsing, OCR if needed, interactive prompts)
     │
     ▼
DMP_worksheet.xlsx     [user review checkpoint]
     │
     ▼
parse_dmp_worksheet.py
     │
     ▼
DMPDesign              (canonical in-memory shape)
     │
     ▼
inject_door_chart.py   (no PDF involvement, no ParsedDesign)
     │
     ▼
door_chart.xlsx
```

`parse_zone_schedule.py` remains in use, but only by `generate_dmp_ws.py`. It is no longer imported by anything in the door chart pipeline.

## DMPDesign schema extensions

`parse_dmp_worksheet.parse_dmp_worksheet` becomes the canonical reader. The returned `DMPDesign` carries everything the door chart needs.

**Existing field reused:** `site_info.school_name` (already populated by the parser from `SITE INFO!B10`) supplies the door chart's Header B3. No new `school_info` dict is added — `inject_door_chart` reaches into `dmp_design.site_info.school_name` directly. The PDF parser does not currently extract address/city, so the door chart's Header B4 / B5 fall through to the template's `[ADDRESS]` / `[CITY, STATE ZIP]` placeholders. (Adding address extraction is out of scope for this change.)

**New field:**
- `zones: list[ZoneRecord]` — each record carries:
  - `zone_number: int` (e.g. `501`)
  - `description: str` (room name, `"SPARE"`, `"PS-N: A/C LOSS"`, or `"PS-N: BATT. TRBL."`)
  - `rsp_number: int | None` (None for spares)
  - `is_spare: bool`
  - `is_ps_ac: bool`
  - `is_ps_batt: bool`

  Read from the DMP's `Master` sheet (column A holds the zone label `Z501`-`Z980`, column B holds the zone description; rows 2 onward).

**Existing field that must populate (currently returns `None`):**
- `site_info.xr550_location` — the parser currently reads `row[4]` of the `DMP XR550` sheet's location row, but the value is in column D (`row[3]`). Fix the column index.

**Existing field, parser fix needed:**
- `splitters` — currently returns 0 for the generated DMP because the parser only recognizes legacy `LX-710-N` / `KP-710-N` IDs. Extend ID matching to also accept the IA-diagram format `710-LX{500..900}-N` and `710-KP-N` that `generate_dmp_ws` writes. Same dataclass shape, broader regex.

**RSP location formatting:**
The door chart writer composes two strings per RSP:
- `RSP/PS LOCATION` = `"<Bldg> - <Room> - Remote Service Panel #N"`
- `66 BLOCK LOCATION` = `"<Bldg> - <Room> - Main SecTC 66 Block(s) - RSP N"`

The DMP's RSP sheets already contain the building/room prefix. Each parsed `RSP` exposes that prefix as a structured field. The inject layer composes the two final strings from the prefix + RSP number.

## inject_door_chart changes

- Drop `from parse_zone_schedule import ParsedDesign, ZoneRecord, CombusLine`.
- `inject(template_path, dmp_design, output_path)` — single design argument. No optional `dmp_design` parameter; it's the only design now.
- `find_combus_line` / `format_rsp_location` / `format_66_block_location` are reworked to operate on `DMPDesign.rsps` instead of `combus_lines`.
- `format_zone_description(z)` operates on `DMPDesign`'s `ZoneRecord` (same fields as before, just sourced from the DMP rather than the PDF).
- Header sheet population reads from `dmp_design.school_info`.
- Master sheet zone area population reads from `dmp_design.zones`.
- Existing XR-550 CONFIG and SPLITTER TOPOLOGY population logic is preserved — those already operate on `DMPDesign`.

## CLI changes

- **New:** `scripts/generate_door_chart.py`. Takes one positional arg: the path to the DMP xlsx. Optional `--template` and `--output-dir` flags. Loads DMP, calls `inject`, prints summary.
- **Renamed:** `scripts/generate_dmp.py` → `scripts/generate_dmp_ws.py` (more accurately reflects that it produces the DMP *worksheet*, distinct from the door chart). Update the in-file docstring's usage example to match.
- **Deleted:** `scripts/run_pipeline.py`. The two-step flow (`generate_dmp_ws` then `generate_door_chart`) replaces it.

User-facing workflow:
```bash
python scripts/generate_dmp_ws.py "input/Academy.pdf"
# user reviews output/THE_ACADEMY..._dmp_2026-05-07.xlsx
python scripts/generate_door_chart.py "output/THE_ACADEMY..._dmp_2026-05-07.xlsx"
# door chart written to output/THE_ACADEMY..._door_chart_2026-05-07.xlsx
```

## Verification

**Checkpoint 1 — parser round-trip.** After parser changes, both freshly-generated DMPs load with all expected fields populated:
```python
d = parse_dmp_worksheet("output/THE_ACADEMY..._dmp_2026-05-07.xlsx")
assert d.school_info["school_name"] == "THE ACADEMY OF ENRICHED SCIENCES"
assert d.site_info.xr550_location is not None
assert len(d.splitters) > 0      # currently 0 — fixing this is the parser change
assert len(d.zones) > 0
assert any(z.is_ps_ac for z in d.zones)
```

**Checkpoint 2 — end-to-end no regression.** Run for both schools:
```bash
python scripts/generate_dmp_ws.py "input/O'MELVENY...pdf"
python scripts/generate_door_chart.py "output/O_MELVENY..._dmp_2026-05-07.xlsx"
```
Cell-by-cell compare against the previously-committed door charts on these load-bearing ranges:
- Header sheet B3:B5
- Master sheet B3, B8, B11:B15 (XR-550 config)
- Master sheet A29:G63 (splitter topology)
- Master sheet B67:D562 (zones)

**Checkpoint 3 — visual spot-check in Excel.** Open both door charts and confirm:
- No "repair file" dialog
- Conditional formatting, cell colors, borders match the committed files
- Splitter topology rows show full IA-diagram IDs (`710-LX500-1`, `710-KP-1`)
- PS supervisory labels appear (e.g. `PS-1: A/C LOSS`)
- No `TBD` cells, no unexpectedly-blank cells where data should be

If anything fails checkpoint 3, the fix is upstream in `generate_dmp_ws.py` per the single-source decision; re-run both commands.

## Risks

- **DMP parser must reliably read what DMP generator writes.** Once they're paired, schema drift between them silently corrupts the door chart. Mitigation: checkpoint 1's assertions catch missing fields immediately; checkpoint 2's cell-level diff catches format changes.
- **Breaking change for any external caller of `inject_door_chart.inject()`.** The signature changes from `(template, design, output, dmp_design=None)` to `(template, dmp_design, output)`. Mitigation: there are no external callers; `run_pipeline.py` is the only caller and is being deleted.
- **`run_pipeline.py` deletion.** Any docs / scripts that reference it break. Mitigation: grep the project for references and update them.
