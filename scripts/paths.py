import json
import sys
from pathlib import Path

APP_NAME = "DMP WS & Door Chart Generator"

# App preferences file (also written by app.py). The "output_dir" key, if set,
# overrides the default output location.
_PREFS_PATH = Path.home() / ".c1_door_chart_app.json"


def resource_path(rel: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent.parent))
    return base / rel


def _default_output_dir() -> Path:
    return Path.home() / "Documents" / APP_NAME


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
