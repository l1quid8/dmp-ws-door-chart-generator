"""Project session persistence for the field-edit workflow.

A session is the app's working document: a DMPDesign plus editing state,
saved as a human-readable JSON file (.dmps) so a school can be reopened and
finished across multiple site visits. The xlsx outputs are pure artifacts
generated from a session; they are never re-imported (the session is the
source of truth).

Save model: explicit saves only. A background recovery file (<name>.recovery)
is written while the editor has unsaved changes and removed on a clean save,
so a crash never loses field data but nothing is committed without the user
asking.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from paths import output_dir
from parse_dmp_worksheet import (
    DMPDesign,
    Keypad,
    PowerSupply,
    RSP,
    SiteInfo,
    Splitter,
    Zone,
    ZoneInfo,
    _master_zones_from_point_info,
)

SCHEMA_VERSION = 1
SESSION_EXT = ".dmps"
RECOVERY_SUFFIX = ".recovery"


class SessionLoadError(Exception):
    """Raised when a session file can't be loaded (corrupt, or newer schema)."""


@dataclass
class Session:
    """A DMPDesign plus the editing state that must survive app restarts."""
    design: DMPDesign
    source_kind: str = ""            # "pdf" | "xlsx" | ""
    source_name: str = ""            # original input filename, display only
    topology_confirmed: bool = False
    saved_at: Optional[str] = None   # ISO timestamp of last clean save
    path: Optional[Path] = None      # where this session lives on disk


@dataclass
class SessionSummary:
    """Cheap listing entry for the recent-projects screen."""
    path: Path
    school_name: str
    saved_at: Optional[str]
    source_name: str


# -------- dirs / naming --------

def sessions_dir() -> Path:
    """App-managed session folder, under the user-configurable output root."""
    d = output_dir() / "Sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _slugify(name: str) -> str:
    # Same shape as inject_door_chart._slugify (copied to avoid importing the
    # xlsx-injection module just for a filename).
    slug = re.sub(r"[^A-Za-z0-9]+", "_", (name or "").strip()).strip("_")
    return slug or "UNTITLED"


def default_session_path(design: DMPDesign) -> Path:
    return sessions_dir() / f"{_slugify(design.site_info.school_name or '')}{SESSION_EXT}"


def recovery_path(session_path: Path) -> Path:
    return session_path.parent / (session_path.name + RECOVERY_SUFFIX)


# -------- DMPDesign <-> dict --------

def design_to_dict(design: DMPDesign) -> dict:
    # asdict handles every nested dataclass. LocationConflict objects in
    # design.conflicts are also dataclasses; their (value, source) option
    # tuples become JSON lists and are re-tupled on load.
    return dataclasses.asdict(design)


def _site_info_from_dict(d: dict) -> SiteInfo:
    fields = {f.name for f in dataclasses.fields(SiteInfo)}
    return SiteInfo(**{k: v for k, v in d.items() if k in fields})


def _splitter_from_dict(d: dict) -> Splitter:
    return Splitter(
        id=d.get("id", ""),
        splitter_type=d.get("splitter_type", ""),
        location=d.get("location"),
        inputs=dict(d.get("inputs") or {}),
        outputs=list(d.get("outputs") or []),
    )


def _rsp_from_dict(d: dict) -> RSP:
    return RSP(
        number=d.get("number", 0),
        location=d.get("location"),
        zones=list(d.get("zones") or []),
    )


def _keypad_from_dict(d: dict) -> Keypad:
    return Keypad(
        number=d.get("number", 0),
        source=d.get("source"),
        location=d.get("location"),
        global_keypad=bool(d.get("global_keypad", False)),
    )


def _power_supply_from_dict(d: dict) -> PowerSupply:
    return PowerSupply(
        number=d.get("number", 0),
        location=d.get("location"),
        # JSON forces dict keys to strings; relay numbers are ints.
        relays={int(k): v for k, v in (d.get("relays") or {}).items()},
    )


def _zone_info_from_dict(d: dict) -> ZoneInfo:
    return ZoneInfo(
        number=d.get("number", 0),
        location=d.get("location"),
        device_type=d.get("device_type"),
        partition=d.get("partition"),
    )


def _zone_from_dict(d: dict) -> Zone:
    return Zone(
        number=d.get("number", 0),
        description=d.get("description", ""),
        rsp_number=d.get("rsp_number"),
        is_spare=bool(d.get("is_spare", False)),
        is_ps_ac=bool(d.get("is_ps_ac", False)),
        is_ps_batt=bool(d.get("is_ps_batt", False)),
    )


def _conflict_from_dict(d: dict) -> Any:
    # Lazy import: generate_dmp_ws pulls in the PDF stack, which session.py
    # must not require just to open a saved project.
    from generate_dmp_ws import LocationConflict
    return LocationConflict(
        kind=d.get("kind", ""),
        number=d.get("number", 0),
        label=d.get("label", ""),
        options=[tuple(o) for o in (d.get("options") or [])],
    )


def design_from_dict(d: dict) -> DMPDesign:
    return DMPDesign(
        site_info=_site_info_from_dict(d.get("site_info") or {}),
        splitters=[_splitter_from_dict(x) for x in d.get("splitters") or []],
        rsps=[_rsp_from_dict(x) for x in d.get("rsps") or []],
        keypads=[_keypad_from_dict(x) for x in d.get("keypads") or []],
        power_supplies=[_power_supply_from_dict(x) for x in d.get("power_supplies") or []],
        zones=[_zone_info_from_dict(x) for x in d.get("zones") or []],
        master_zones=[_zone_from_dict(x) for x in d.get("master_zones") or []],
        conflicts=[_conflict_from_dict(x) for x in d.get("conflicts") or []],
        topology_source=d.get("topology_source", ""),
        master_zones_source=d.get("master_zones_source", ""),
        dmp_status=d.get("dmp_status", ""),
    )


# -------- zone sync (the dual-representation contract) --------
#
# design.zones (ZoneInfo, editable in the ZONES tab) is the single source of
# truth for zone data. design.master_zones (Zone) is derived from it and is
# what the door chart consumes. Editors mutate zones, then call
# sync_master_zones before any save or export.

def ensure_editable_zones(design: DMPDesign) -> None:
    """Synthesize editable ZoneInfo rows when only master_zones exist.

    Happens when a worksheet was loaded whose Point Info formulas were never
    evaluated by Excel (openpyxl data_only=True reads None), so the parser got
    master_zones but no zones.
    """
    if design.zones or not design.master_zones:
        return
    for mz in design.master_zones:
        if mz.is_spare:
            loc, dtype = "SPARE", "Spare"
        elif mz.is_ps_ac or mz.is_ps_batt:
            loc, dtype = mz.description, "Supervisory"
        else:
            loc, dtype = mz.description, "Motion"
        design.zones.append(ZoneInfo(number=mz.number, location=loc,
                                     device_type=dtype, partition=1))


def sync_master_zones(design: DMPDesign) -> None:
    """Rebuild master_zones from the (possibly edited) zones list.

    _master_zones_from_point_info emits the exact 'SPARE' / 'PS-N: A/C LOSS' /
    'PS-N: BATT. TRBL' phrases the door chart's conditional formatting keys on.
    """
    if design.zones:
        design.master_zones = _master_zones_from_point_info(design.zones, design.rsps)
        design.master_zones_source = "point_info"


# -------- save / load --------

def _session_to_dict(session: Session) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "app_version": _app_version(),
        "saved_at": session.saved_at,
        "source": {"kind": session.source_kind, "name": session.source_name},
        "topology_confirmed": session.topology_confirmed,
        "design": design_to_dict(session.design),
    }


def _session_from_dict(d: dict, path: Path) -> Session:
    version = d.get("schema_version", 0)
    if version > SCHEMA_VERSION:
        raise SessionLoadError(
            f"{path.name} was saved by a newer version of this app "
            f"(schema {version}; this app supports up to {SCHEMA_VERSION}). "
            "Update the app to open it."
        )
    source = d.get("source") or {}
    return Session(
        design=design_from_dict(d.get("design") or {}),
        source_kind=source.get("kind", ""),
        source_name=source.get("name", ""),
        topology_confirmed=bool(d.get("topology_confirmed", False)),
        saved_at=d.get("saved_at"),
        path=path,
    )


def _app_version() -> str:
    try:
        from paths import resource_path
        return resource_path("VERSION").read_text().strip()
    except Exception:
        return ""


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def save_session(session: Session, path: Path | None = None) -> Path:
    """Explicit save: commit the session and clear any recovery file."""
    target = path or session.path or default_session_path(session.design)
    sync_master_zones(session.design)
    session.saved_at = datetime.now().isoformat(timespec="seconds")
    session.path = target
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, json.dumps(_session_to_dict(session), indent=1))
    clear_recovery(target)
    return target


def load_session(path: Path) -> Session:
    path = Path(path)
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionLoadError(f"Couldn't read {path.name}: {exc}") from exc
    return _session_from_dict(d, path)


def list_recent_sessions(limit: int = 10) -> list[SessionSummary]:
    """Most-recently-modified sessions for the home screen."""
    out: list[SessionSummary] = []
    try:
        paths = sorted(sessions_dir().glob(f"*{SESSION_EXT}"),
                       key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return out
    for p in paths[:limit]:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            school = ((d.get("design") or {}).get("site_info") or {}).get("school_name") or p.stem
            out.append(SessionSummary(
                path=p,
                school_name=school,
                saved_at=d.get("saved_at"),
                source_name=(d.get("source") or {}).get("name", ""),
            ))
        except Exception:
            continue  # unreadable file: skip rather than break the home screen
    return out


# -------- crash recovery --------

def write_recovery(session: Session) -> Path:
    """Background snapshot of unsaved work. Never shown unless offered on open."""
    target = session.path or default_session_path(session.design)
    rec = recovery_path(target)
    sync_master_zones(session.design)
    d = _session_to_dict(session)
    d["saved_at"] = datetime.now().isoformat(timespec="seconds")
    rec.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(rec, json.dumps(d, indent=1))
    return rec


def clear_recovery(session_path: Path) -> None:
    try:
        recovery_path(session_path).unlink(missing_ok=True)
    except OSError:
        pass


def pending_recovery(session_path: Path) -> Optional[datetime]:
    """If unsaved work from a crash exists and is newer than the session,
    return its timestamp so the app can offer to recover it."""
    rec = recovery_path(session_path)
    if not rec.exists():
        return None
    try:
        rec_m = rec.stat().st_mtime
        if session_path.exists() and rec_m <= session_path.stat().st_mtime:
            return None
        return datetime.fromtimestamp(rec_m)
    except OSError:
        return None


def load_recovery(session_path: Path) -> Session:
    session = _session_from_dict(
        json.loads(recovery_path(session_path).read_text(encoding="utf-8")),
        session_path,
    )
    return session
