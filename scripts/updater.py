"""
Self-update support for the packaged app.

The release pipeline (.github/workflows/release.yml) publishes a GitHub Release on
every ``v*`` tag with stable, per-platform asset names:

    C1-DMP-Toolkit-macOS.zip     (a .app, zipped with --keepParent)
    C1-DMP-Toolkit-Windows.zip   (the one-folder app at top level)

This module checks the public GitHub Releases API, compares the latest release tag
against the bundled VERSION, and — when newer — downloads the matching asset and
hands off to a small detached helper script that swaps the running app in place and
relaunches it. Stdlib only (no requests); every network path is wrapped so being
offline or rate-limited never blocks launch.

Updates the app downloads itself via urllib do NOT get macOS's com.apple.quarantine
xattr (only browsers/Finder set it), so a self-installed update opens without the
Gatekeeper prompt the user hits on the first manual install. The mac helper also
strips quarantine defensively.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from paths import resource_path

REPO = "l1quid8/c1-dmp-toolkit"
_API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
_RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
_UA = "DMP-DoorChart-Updater"

# Asset name suffixes produced by the release workflow, keyed by sys.platform.
_ASSET_SUFFIX = {"darwin": "macOS.zip", "win32": "Windows.zip"}


# --------------------------------------------------------------------------- #
# Version helpers                                                              #
# --------------------------------------------------------------------------- #

def parse_version(text: str) -> tuple[int, ...]:
    """Parse '1.0.3' or 'v1.0.3' into (1, 0, 3); () if unparseable."""
    nums = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in nums) if nums else ()


def current_version_str() -> str:
    """The bundled VERSION string ('' if missing)."""
    try:
        return resource_path("VERSION").read_text().strip()
    except Exception:
        return ""


def current_version() -> tuple[int, ...]:
    return parse_version(current_version_str())


def is_newer(latest: tuple[int, ...], current: tuple[int, ...]) -> bool:
    """True if `latest` is a strictly higher version than `current`."""
    if not latest:
        return False
    n = max(len(latest), len(current))
    pad = lambda t: t + (0,) * (n - len(t))
    return pad(latest) > pad(current)


# --------------------------------------------------------------------------- #
# Release lookup                                                               #
# --------------------------------------------------------------------------- #

def _select_asset(assets: list[dict]) -> str | None:
    suffix = _ASSET_SUFFIX.get(sys.platform)
    if not suffix:
        return None
    for a in assets:
        name = a.get("name") or ""
        if name.endswith(suffix):
            return a.get("browser_download_url")
    return None


def fetch_latest(timeout: float = 5.0) -> dict | None:
    """Return info about the latest release, or None on any failure/offline.

    Result keys: tag, version (tuple), notes (release body), asset_url (platform
    zip download), html_url (release page).
    """
    req = urllib.request.Request(
        _API_LATEST,
        headers={"User-Agent": _UA, "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    tag = data.get("tag_name") or ""
    return {
        "tag": tag,
        "version": parse_version(tag),
        "notes": (data.get("body") or "").strip(),
        "asset_url": _select_asset(data.get("assets") or []),
        "html_url": data.get("html_url") or _RELEASES_PAGE,
    }


# --------------------------------------------------------------------------- #
# Install-location detection (packaged builds only)                           #
# --------------------------------------------------------------------------- #

def install_dir() -> Path | None:
    """The on-disk app to replace, or None in a dev (unfrozen) run.

    macOS: the .app bundle root. Windows: the one-folder app directory.
    """
    if not getattr(sys, "frozen", False):
        return None
    exe = Path(sys.executable)
    if sys.platform == "darwin":
        for parent in exe.parents:
            if parent.suffix == ".app":
                return parent
        return None
    if sys.platform == "win32":
        return exe.parent
    return None


def can_self_update() -> bool:
    return install_dir() is not None and sys.platform in _ASSET_SUFFIX


# --------------------------------------------------------------------------- #
# Download                                                                     #
# --------------------------------------------------------------------------- #

def download(url: str, dest: Path, progress_cb=None, timeout: float = 30.0) -> None:
    """Stream `url` to `dest`, calling progress_cb(fraction 0..1) as it goes."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        read = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                read += len(chunk)
                if progress_cb and total:
                    progress_cb(min(read / total, 0.999))
    if progress_cb:
        progress_cb(1.0)


# --------------------------------------------------------------------------- #
# Install (extract + detached swap-and-relaunch)                              #
# --------------------------------------------------------------------------- #

def _work_dir(install_path: Path) -> Path:
    """A scratch dir on the same volume as the install (so move is atomic/fast).

    Falls back to the system temp dir if the install's parent isn't writable.
    """
    sibling = install_path.parent / f".dmp_update_{os.getpid()}"
    try:
        sibling.mkdir(parents=True, exist_ok=True)
        return sibling
    except Exception:
        return Path(tempfile.mkdtemp(prefix="dmp_update_"))


def _find_payload(extracted: Path) -> Path | None:
    """Locate the new app inside the extracted zip contents."""
    if sys.platform == "darwin":
        apps = list(extracted.glob("*.app"))
        return apps[0] if apps else None
    # Windows: the workflow zips the one-folder app at the archive's top level.
    dirs = [p for p in extracted.iterdir() if p.is_dir()]
    return dirs[0] if len(dirs) == 1 else (dirs[0] if dirs else None)


def prepare_payload(zip_path: Path, work: Path) -> Path | None:
    """Extract `zip_path` into `work` and return the new .app / app folder."""
    extracted = work / "extracted"
    extracted.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        # A macOS .app needs its executable bit and internal symlinks intact —
        # Python's zipfile drops both (the main binary comes out non-executable
        # and the bundle's ~158 framework symlinks get flattened into plain
        # files), producing a bundle launchd refuses to spawn. CI builds the
        # archive with `ditto`, so extract it with `ditto` too.
        subprocess.run(["ditto", "-x", "-k", str(zip_path), str(extracted)],
                       check=True)
    else:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(extracted)
    return _find_payload(extracted)


def _write_mac_script(new_app: Path, app: Path, work: Path) -> Path:
    pid = os.getpid()
    bak = f"{app}.bak"
    script = work / "swap.sh"
    script.write_text(f"""#!/bin/bash
# Wait for the running app (pid {pid}) to exit, then swap in the new build.
while kill -0 {pid} 2>/dev/null; do sleep 0.5; done
NEW="{new_app}"
APP="{app}"
BAK="{bak}"
xattr -dr com.apple.quarantine "$NEW" 2>/dev/null
rm -rf "$BAK"
if mv "$APP" "$BAK"; then
  if mv "$NEW" "$APP"; then
    xattr -dr com.apple.quarantine "$APP" 2>/dev/null
    rm -rf "$BAK"
  else
    mv "$BAK" "$APP"   # rollback
  fi
fi
open "$APP"
rm -rf "{work}"
""")
    script.chmod(0o755)
    return script


def _write_windows_script(new_dir: Path, app: Path, work: Path) -> Path:
    pid = os.getpid()
    exe_name = Path(sys.executable).name
    bak = f"{app}.bak"
    script = work / "swap.bat"
    script.write_text(f"""@echo off
:waitloop
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul
if not errorlevel 1 (
  timeout /t 1 /nobreak >nul
  goto waitloop
)
set "APP={app}"
set "NEW={new_dir}"
set "BAK={bak}"
if exist "%BAK%" rmdir /s /q "%BAK%"
move "%APP%" "%BAK%" >nul
if exist "%APP%" goto rollback
move "%NEW%" "%APP%" >nul
if not exist "%APP%\\{exe_name}" goto rollback
rmdir /s /q "%BAK%"
start "" "%APP%\\{exe_name}"
goto done
:rollback
if exist "%APP%" rmdir /s /q "%APP%"
move "%BAK%" "%APP%" >nul
start "" "%APP%\\{exe_name}"
:done
rmdir /s /q "{work}" 2>nul
del "%~f0"
""", encoding="utf-8")
    return script


def apply_update(zip_path: Path) -> bool:
    """Swap the running app for the downloaded build and relaunch.

    Returns True after launching the detached helper (the caller should then quit
    the app so the helper can replace its files). Returns False if self-update
    isn't possible (dev run / unknown platform / malformed archive).
    """
    app = install_dir()
    if app is None or sys.platform not in _ASSET_SUFFIX:
        return False

    work = _work_dir(app)
    payload = prepare_payload(zip_path, work)
    if payload is None:
        return False

    if sys.platform == "darwin":
        script = _write_mac_script(payload, app, work)
        subprocess.Popen(["/bin/bash", str(script)], start_new_session=True)
    else:  # win32
        script = _write_windows_script(payload, app, work)
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(["cmd", "/c", str(script)], creationflags=flags,
                          close_fds=True)
    return True
