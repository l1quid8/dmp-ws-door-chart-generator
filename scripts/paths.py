import json
import re
import sys
from pathlib import Path

APP_NAME = "C1 DMP Toolkit"

# The pre-rename name. Its ~/Documents folder holds real deliverables on machines
# that used the default output location, so keep honouring it rather than silently
# starting a fresh empty folder next to it.
_LEGACY_APP_NAME = "DMP WS & Door Chart Generator"

# Generated artifacts are revision-numbered ({base}_rev3.xlsx): each generate
# keeps prior revisions so a superintendent's mark-ups on rev N stay
# comparable against rev N+1.
_REV_RE_TMPL = r"^{base}_rev(\d+)\.xlsx$"


def _rev_numbers(out_dir: Path, base: str) -> list[int]:
    rev_re = re.compile(_REV_RE_TMPL.format(base=re.escape(base)))
    numbers = []
    try:
        for p in out_dir.iterdir():
            m = rev_re.match(p.name)
            if m:
                numbers.append(int(m.group(1)))
    except OSError:
        pass
    return numbers


def next_rev_path(out_dir: Path, base: str) -> Path:
    """The next free revision filename for an artifact ({base}_rev{N}.xlsx)."""
    numbers = _rev_numbers(out_dir, base)
    return out_dir / f"{base}_rev{max(numbers, default=0) + 1}.xlsx"


def latest_rev_path(out_dir: Path, base: str) -> Path | None:
    """The highest existing revision of an artifact, or None if none exist."""
    numbers = _rev_numbers(out_dir, base)
    if not numbers:
        return None
    return out_dir / f"{base}_rev{max(numbers)}.xlsx"

# App preferences file (also written by app.py). The "output_dir" key, if set,
# overrides the default output location.
_PREFS_PATH = Path.home() / ".c1_door_chart_app.json"


def resource_path(rel: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent.parent))
    return base / rel


def _default_output_dir() -> Path:
    """Default output folder, preferring an existing pre-rename folder.

    The app was renamed from "DMP WS & Door Chart Generator" to "C1 DMP Toolkit".
    A machine that used the default location has prior revisions under the old
    name; switching outright would leave them behind with no indication why. Only
    fall back to the legacy folder if it already exists and the new one doesn't.
    """
    new = Path.home() / "Documents" / APP_NAME
    if not new.exists():
        legacy = Path.home() / "Documents" / _LEGACY_APP_NAME
        if legacy.is_dir():
            return legacy
    return new


def output_dir() -> Path:
    """Return the user-configured output directory, creating it if missing.

    Reads the "output_dir" pref (set via the app's "Save output to" picker).
    Falls back to ~/Documents/<APP_NAME> if unset or unwritable.
    """
    chosen: Path | None = None
    try:
        raw = json.loads(_PREFS_PATH.read_text()).get("output_dir")
        if raw:
            chosen = Path(raw)
    except Exception:
        chosen = None

    d = chosen or _default_output_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        # Stale or unwritable configured path — fall back to the default.
        fallback = _default_output_dir()
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback
