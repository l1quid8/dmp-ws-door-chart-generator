"""Add/remove hardware on a DMPDesign — post-CAD field changes.

Pure mutations with capacity guards; no UI. The editor calls these, then
re-syncs master zones and refreshes its tabs.

Conventions encoded here:
- Expander module N owns the fixed 16-zone address block
  Z{501+16(N-1)}..Z{500+16N} regardless of model — the template's Point Info
  sheet N is hard-wired to that Master stride, and DMP addressing assigns the
  range by module address. A 714-8 only materializes its 8 real points; the
  rest of the block stays unallocated (blank Master rows).
- The expander's last two physical points supervise its paired power supply
  (exact phrases 'PS-N: A/C LOSS' / 'PS-N: BATT. TRBL' — door-chart
  conditional formatting keys on them).
- Zone addresses are physical: removal leaves a numbering gap, never
  renumbers. Adding reuses the lowest free module number (a fresh expander
  takes the free address).
"""

from __future__ import annotations

import re

from parse_dmp_worksheet import DMPDesign, Keypad, PowerSupply, RSP, Splitter, ZoneInfo

# Template capacities (DMP Installation Worksheet_template_blank.xlsx)
MAX_EXPANDERS = 15        # Point Info sheets shipped in the template
MAX_SPLITTERS_PER_TYPE = 12   # 4 rows per splitter in rows 2-50
MAX_KEYPADS = 28          # Keypad sheet rows 3-30

EXPANDER_MODELS = {"714-16": 16, "714-8": 8}

ZONE_BLOCK = 16
ZONE_BASE = 501


class HardwareError(Exception):
    """A hardware change the template (or physics) can't accommodate."""


# -------- expanders (RSP + paired PS + zone block) --------

def zone_block_for(number: int) -> range:
    """The 16-zone address block owned by expander module `number`."""
    start = ZONE_BASE + ZONE_BLOCK * (number - 1)
    return range(start, start + ZONE_BLOCK)


def next_expander_number(design: DMPDesign) -> int:
    used = {r.number for r in design.rsps}
    n = 1
    while n in used:
        n += 1
    return n


def add_expander(design: DMPDesign, model: str, location: str | None = None) -> RSP:
    if model not in EXPANDER_MODELS:
        raise HardwareError(f"Unknown expander model: {model}")
    if len(design.rsps) >= MAX_EXPANDERS:
        raise HardwareError(
            f"The worksheet template supports at most {MAX_EXPANDERS} expanders "
            f"(Point Info sheets 1-{MAX_EXPANDERS})."
        )
    number = next_expander_number(design)
    points = EXPANDER_MODELS[model]
    block = list(zone_block_for(number))[:points]

    rsp = RSP(number=number, location=location, zones=block, model=model)
    design.rsps.append(rsp)
    design.rsps.sort(key=lambda r: r.number)

    design.power_supplies.append(PowerSupply(number=number, location=location))
    design.power_supplies.sort(key=lambda p: p.number)

    # Usable points arrive as SPARE for the tech to rename; the last two
    # physical points supervise the paired power supply.
    for i, zone_num in enumerate(block):
        if i == points - 2:
            zi = ZoneInfo(number=zone_num, location=f"PS-{number}: A/C LOSS",
                          device_type="Supervisory", partition=1)
        elif i == points - 1:
            zi = ZoneInfo(number=zone_num, location=f"PS-{number}: BATT. TRBL",
                          device_type="Supervisory", partition=1)
        else:
            zi = ZoneInfo(number=zone_num, location="SPARE",
                          device_type="Spare", partition=1)
        design.zones.append(zi)
    design.zones.sort(key=lambda z: z.number)
    return rsp


def remove_expander(design: DMPDesign, number: int) -> None:
    rsp = next((r for r in design.rsps if r.number == number), None)
    if rsp is None:
        raise HardwareError(f"No expander #{number} in this design.")
    block = set(zone_block_for(number))
    design.rsps.remove(rsp)
    design.power_supplies = [p for p in design.power_supplies if p.number != number]
    design.zones = [z for z in design.zones if z.number not in block]
    _scrub_splitter_outputs(design, f"RSP-{number}")
    _scrub_splitter_outputs(design, f"RSP {number}")


# -------- splitters --------

_SPLITTER_NUM_RE = re.compile(r"(\d+)\s*$")


def _splitter_id(splitter_type: str, n: int) -> str:
    return f"710-LX500-{n}" if splitter_type == "LX" else f"710-KP-{n}"


def add_splitter(design: DMPDesign, splitter_type: str,
                 location: str | None = None) -> Splitter:
    if splitter_type not in ("LX", "KP"):
        raise HardwareError(f"Unknown splitter type: {splitter_type}")
    same_type = [s for s in design.splitters if s.splitter_type == splitter_type]
    if len(same_type) >= MAX_SPLITTERS_PER_TYPE:
        raise HardwareError(
            f"The splitter sheet fits at most {MAX_SPLITTERS_PER_TYPE} "
            f"{splitter_type} splitters."
        )
    used = set()
    for s in same_type:
        m = _SPLITTER_NUM_RE.search(s.id or "")
        if m:
            used.add(int(m.group(1)))
    n = 1
    while n in used:
        n += 1
    splitter = Splitter(id=_splitter_id(splitter_type, n),
                        splitter_type=splitter_type, location=location,
                        outputs=["Spare", "Spare", "Spare"])
    design.splitters.append(splitter)
    return splitter


def remove_splitter(design: DMPDesign, splitter_id: str) -> None:
    splitter = next((s for s in design.splitters if s.id == splitter_id), None)
    if splitter is None:
        raise HardwareError(f"No splitter {splitter_id} in this design.")
    design.splitters.remove(splitter)
    _scrub_splitter_outputs(design, f"To {splitter_id}")
    # Keypads fed from this splitter need a new source — blank it so the
    # keypad.source_missing rule walks the tech back here before FINAL.
    for kp in design.keypads:
        if (kp.source or "").strip() == splitter_id:
            kp.source = None


# -------- keypads --------

def add_keypad(design: DMPDesign, location: str | None = None,
               source: str | None = None, global_keypad: bool = False) -> Keypad:
    if len(design.keypads) >= MAX_KEYPADS:
        raise HardwareError(f"The keypad sheet fits at most {MAX_KEYPADS} keypads.")
    used = {k.number for k in design.keypads}
    n = 1
    while n in used:
        n += 1
    keypad = Keypad(number=n, source=source, location=location,
                    global_keypad=global_keypad)
    design.keypads.append(keypad)
    design.keypads.sort(key=lambda k: k.number)
    return keypad


def remove_keypad(design: DMPDesign, number: int) -> None:
    keypad = next((k for k in design.keypads if k.number == number), None)
    if keypad is None:
        raise HardwareError(f"No keypad #{number} in this design.")
    design.keypads.remove(keypad)
    _scrub_splitter_outputs(design, f"KEYPAD #{number}")


# -------- shared --------

def _scrub_splitter_outputs(design: DMPDesign, token: str) -> None:
    """Replace outputs that pointed at removed hardware with 'Spare'."""
    for s in design.splitters:
        s.outputs = ["Spare" if (o or "").strip() == token else o
                     for o in (s.outputs or [])]
