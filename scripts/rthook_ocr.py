"""
PyInstaller runtime hook — executes before any user code or imports.
"""
# Unconditional write the very first line — no try/except, no imports above this
with open("/tmp/rthook_ocr_ENTER.log", "a") as _f:
    _f.write("rthook entered\n")

import os
import sys
import time
import pkgutil
from pathlib import Path


# Debug log so we can confirm this hook actually ran in a frozen build
def _log(msg: str) -> None:
    try:
        with open("/tmp/rthook_debug.log", "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception as e:
        try:
            with open("/tmp/rthook_error.log", "a") as f:
                f.write(f"_log failed: {type(e).__name__}: {e}\n")
        except Exception:
            pass


_log("rthook_ocr.py START")
_log(f"  sys.executable = {sys.executable}")
_log(f"  sys._MEIPASS   = {getattr(sys, '_MEIPASS', None)}")


# ─────────────────────────────────────────────────────────────────────────────
# 1. Locate bundled OCR binaries — they live in Frameworks/ (PyInstaller's
#    BUNDLE step moves Mach-O binaries there) and data in Resources/.
# ─────────────────────────────────────────────────────────────────────────────
def _find_dir_with(rel_path: str) -> Path | None:
    """Return the directory (Resources or Frameworks) that contains rel_path."""
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass))
    exe = Path(sys.executable)
    if sys.platform == "darwin" and exe.parent.name == "MacOS":
        candidates.append(exe.parent.parent / "Resources")
        candidates.append(exe.parent.parent / "Frameworks")
    for c in candidates:
        if (c / rel_path).exists():
            return c
    return None


_bin_root = _find_dir_with("bin/tesseract")
_data_root = _find_dir_with("share/tessdata")
_log(f"  _bin_root  = {_bin_root}")
_log(f"  _data_root = {_data_root}")

if _bin_root:
    bin_dir = _bin_root / "bin"
    path = os.environ.get("PATH", "")
    if str(bin_dir) not in path:
        os.environ["PATH"] = str(bin_dir) + os.pathsep + path
    _log(f"  PATH prepended with {bin_dir}")

if _data_root:
    tessdata = _data_root / "share" / "tessdata"
    if tessdata.exists():
        os.environ.setdefault("TESSDATA_PREFIX", str(tessdata))
        _log(f"  TESSDATA_PREFIX = {tessdata}")
    gs_lib = _data_root / "share" / "ghostscript" / "lib"
    if gs_lib.exists():
        os.environ.setdefault("GS_LIB", str(gs_lib))


# ─────────────────────────────────────────────────────────────────────────────
# 2. Patch pkgutil.iter_modules so ocrmypdf finds its built-in plugins.
#    PyInstaller's BUNDLE removes .py files from Resources/, so iter_modules
#    returns empty for ocrmypdf.builtin_plugins. We synthesize the entries.
# ─────────────────────────────────────────────────────────────────────────────
_OCRMYPDF_BUILTIN_PLUGINS = [
    "concurrency",
    "default_filters",
    "ghostscript",
    "null_ocr",
    "optimize",
    "pypdfium",
    "tesseract_ocr",
]

_original_iter_modules = pkgutil.iter_modules


def _patched_iter_modules(path=None, prefix=""):
    real = list(_original_iter_modules(path, prefix))
    if real:
        return iter(real)
    if path is None:
        return iter(real)
    path_str = str(path[0]) if isinstance(path, list) and path else str(path)
    if "ocrmypdf" in path_str and "builtin_plugins" in path_str:
        synthetic = [
            pkgutil.ModuleInfo(None, prefix + name, False)
            for name in _OCRMYPDF_BUILTIN_PLUGINS
        ]
        return iter(synthetic)
    return iter(real)


pkgutil.iter_modules = _patched_iter_modules
_log("  pkgutil.iter_modules monkey-patched")
_log("rthook_ocr.py END")
