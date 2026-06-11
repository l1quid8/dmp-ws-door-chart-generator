"""Design validation for the field-edit workflow.

One rule set drives two consumers: the finalize gate (a FINAL worksheet may
only be generated when there are zero error-severity issues) and the live
tab badges in the editor. Rules are pure functions over a DMPDesign so they
are unit-testable without Tk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterator, Optional

from parse_dmp_worksheet import DMPDesign

# Tabs the editor exposes; Issue.tab routes badges and "Go to" buttons.
TAB_SITE = "SITE"
TAB_ZONES = "ZONES"
TAB_SPLITTERS = "SPLITTERS"
TAB_KEYPADS = "KEYPADS"
TAB_POWER = "POWER"


@dataclass(frozen=True)
class Issue:
    code: str                 # e.g. "zone.blank_desc"
    severity: str             # "error" | "warning"
    tab: str                  # TAB_* constant
    ref: Optional[str]        # "zone:507", "splitter:710-LX500-1", "field:ip_address"
    message: str


# Zone descriptions that are placeholders, not real rooms. "NEW" is what the
# generator historically emitted for unresolved locations.
_PLACEHOLDER_RE = re.compile(r"^\s*NEW\s*$", re.I)

# 'RSP 3' / 'RSP3' where the convention is 'RSP-3'.
_RSP_UNHYPHENATED_RE = re.compile(r"\bRSP\s*(\d+)\b")


def rsp_hyphen_fix(text: str) -> str:
    """Normalize 'RSP 3'/'RSP3' to 'RSP-3' (the auto-fix the UI offers)."""
    return _RSP_UNHYPHENATED_RE.sub(r"RSP-\1", text)


# -------- rules --------

def _rule_site_required(design: DMPDesign, ctx: dict) -> Iterator[Issue]:
    required = [
        ("ip_address", "Panel IP address"),
        ("default_gateway", "Default gateway"),
        ("install_date", "Install date"),
        ("install_tech", "Install tech"),
    ]
    for field_name, label in required:
        value = getattr(design.site_info, field_name, None)
        if not (value or "").strip():
            yield Issue(
                code="site.required_missing", severity="error", tab=TAB_SITE,
                ref=f"field:{field_name}", message=f"{label} is missing",
            )


def _rule_zone_descriptions(design: DMPDesign, ctx: dict) -> Iterator[Issue]:
    for z in design.zones:
        desc = (z.location or "").strip()
        if not desc:
            yield Issue(
                code="zone.blank_desc", severity="error", tab=TAB_ZONES,
                ref=f"zone:{z.number}",
                message=f"Z{z.number} has no description (use SPARE if unused)",
            )
        elif _PLACEHOLDER_RE.match(desc):
            yield Issue(
                code="zone.new_placeholder", severity="error", tab=TAB_ZONES,
                ref=f"zone:{z.number}",
                message=f"Z{z.number} is still the 'NEW' placeholder",
            )
        elif desc.upper() == "SPARE" and desc != "SPARE":
            yield Issue(
                code="zone.spare_case", severity="error", tab=TAB_ZONES,
                ref=f"zone:{z.number}",
                message=f"Z{z.number}: '{desc}' must be uppercase SPARE",
            )


def _rule_rsp_hyphen(design: DMPDesign, ctx: dict) -> Iterator[Issue]:
    def check(text: Optional[str], tab: str, ref: str, where: str) -> Iterator[Issue]:
        for m in _RSP_UNHYPHENATED_RE.finditer(text or ""):
            yield Issue(
                code="naming.rsp_hyphen", severity="error", tab=tab, ref=ref,
                message=f"{where}: 'RSP {m.group(1)}' should be 'RSP-{m.group(1)}'",
            )

    for sp in design.splitters:
        for i, out in enumerate(sp.outputs):
            yield from check(out, TAB_SPLITTERS, f"splitter:{sp.id}",
                             f"{sp.id} output {i + 1}")
    for z in design.zones:
        # Supervisory descriptions legitimately reference 'PS-N', never 'RSP N';
        # still scan them — a hand-typed 'RSP 3' in any description is the bug.
        yield from check(z.location, TAB_ZONES, f"zone:{z.number}", f"Z{z.number}")


def _rule_conflicts(design: DMPDesign, ctx: dict) -> Iterator[Issue]:
    for c in design.conflicts:
        kind_tab = TAB_SPLITTERS if getattr(c, "kind", "") == "RSP" else TAB_KEYPADS
        yield Issue(
            code="conflicts.unresolved", severity="error", tab=kind_tab,
            ref=f"conflict:{getattr(c, 'kind', '?')}:{getattr(c, 'number', '?')}",
            message=f"Unresolved source conflict: {getattr(c, 'label', 'location')}",
        )


def _rule_topology_confirmed(design: DMPDesign, ctx: dict) -> Iterator[Issue]:
    if ctx.get("topology_confirmed"):
        return
    # Auto-derived wiring is a guess and must be reviewed; riser-extracted
    # wiring is trustworthy but a review is still encouraged.
    severity = "error" if design.topology_source == "auto-derived" else "warning"
    yield Issue(
        code="topology.unconfirmed", severity=severity, tab=TAB_SPLITTERS,
        ref=None,
        message="Splitter wiring has not been marked as reviewed"
                + (" (auto-derived — must be checked against the riser)"
                   if severity == "error" else ""),
    )


RULES: list[Callable[[DMPDesign, dict], Iterator[Issue]]] = [
    _rule_site_required,
    _rule_zone_descriptions,
    _rule_rsp_hyphen,
    _rule_conflicts,
    _rule_topology_confirmed,
]


# -------- entry points --------

def validate_design(design: DMPDesign, *, topology_confirmed: bool = False) -> list[Issue]:
    ctx = {"topology_confirmed": topology_confirmed}
    issues: list[Issue] = []
    for rule in RULES:
        issues.extend(rule(design, ctx))
    return issues


def errors(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.severity == "error"]


def badge_counts(issues: list[Issue]) -> dict[str, int]:
    """Error count per tab, for the editor's tab badges."""
    counts: dict[str, int] = {}
    for issue in errors(issues):
        counts[issue.tab] = counts.get(issue.tab, 0) + 1
    return counts


def finalize_ok(issues: list[Issue]) -> bool:
    return not errors(issues)
