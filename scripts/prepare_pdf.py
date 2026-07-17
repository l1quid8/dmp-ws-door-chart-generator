"""
Wraps ocrmypdf to convert a C1 design PDF into a searchable PDF.

When bundled (PyInstaller), tesseract + ghostscript binaries are included inside the
app and located automatically. In dev, the system install (brew install ocrmypdf) is used.

Usage:
    python prepare_pdf.py "input/Design.pdf"
"""
from __future__ import annotations

import os
import pkgutil
import shutil
import subprocess
import sys
from pathlib import Path


def _bundle_dir() -> Path | None:
    """Return the directory containing bundled bin/ (Resources or Frameworks)."""
    if not getattr(sys, "frozen", False):
        return None
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass))
    exe = Path(sys.executable)
    if sys.platform == "darwin" and exe.parent.name == "MacOS":
        candidates.append(exe.parent.parent / "Resources")
        candidates.append(exe.parent.parent / "Frameworks")
    for c in candidates:
        if (c / "bin").exists():
            return c
    return candidates[0] if candidates else None


def _setup_ocr_env() -> None:
    """Prepend bundled OCR binaries to PATH and set env vars (frozen builds only)."""
    bundle = _bundle_dir()
    if not bundle:
        return
    bin_dir = bundle / "bin"
    current_path = os.environ.get("PATH", "")
    if str(bin_dir) not in current_path:
        os.environ["PATH"] = str(bin_dir) + os.pathsep + current_path
    tessdata = bundle / "share" / "tessdata"
    if tessdata.exists():
        os.environ.setdefault("TESSDATA_PREFIX", str(tessdata))
    gs_lib = bundle / "share" / "ghostscript" / "lib"
    if gs_lib.exists():
        os.environ.setdefault("GS_LIB", str(gs_lib))


_setup_ocr_env()


# ─────────────────────────────────────────────────────────────────────────────
# Windows windowed (console=False) PyInstaller builds spawn a fresh console
# window for every child process unless creationflags includes CREATE_NO_WINDOW.
# ocrmypdf invokes tesseract.exe, gswin64c.exe, pngquant, etc., so without this
# the user sees a flurry of blank consoles flash during OCR.
# ─────────────────────────────────────────────────────────────────────────────
def _silence_subprocess_consoles_on_windows() -> None:
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    if getattr(_silence_subprocess_consoles_on_windows, "_done", False):
        return
    _silence_subprocess_consoles_on_windows._done = True

    CREATE_NO_WINDOW = 0x08000000
    _orig_init = subprocess.Popen.__init__

    def _patched_init(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
        _orig_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _patched_init


_silence_subprocess_consoles_on_windows()


# ─────────────────────────────────────────────────────────────────────────────
# In frozen builds, replace OcrmypdfPluginManager._setup_plugins so it
# registers built-in plugins by explicit dotted name instead of using
# pkgutil.iter_modules (which doesn't find PyInstaller-bundled modules
# reliably). Idempotent; safe in dev.
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


def _patch_ocrmypdf_setup_plugins() -> None:
    if getattr(_patch_ocrmypdf_setup_plugins, "_done", False):
        return
    _patch_ocrmypdf_setup_plugins._done = True

    import importlib
    from ocrmypdf import _plugin_manager as _pm_mod
    from ocrmypdf import pluginspec as _pluginspec

    def _patched(self):
        self._pm.add_hookspecs(_pluginspec)
        if self._builtins:
            for plugin_name in _OCRMYPDF_BUILTIN_PLUGINS:
                full = f"ocrmypdf.builtin_plugins.{plugin_name}"
                module = importlib.import_module(full)
                self._pm.register(module)
        self._pm.load_setuptools_entrypoints("ocrmypdf")
        for name in self._plugins:
            if isinstance(name, Path) or (isinstance(name, str) and name.endswith(".py")):
                module_name = Path(name).stem
                spec = importlib.util.spec_from_file_location(module_name, name)
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
            else:
                module = importlib.import_module(name)
            self._pm.register(module)

    _pm_mod.OcrmypdfPluginManager._setup_plugins = _patched


def find_ocrmypdf() -> str:
    """Locate the ocrmypdf binary (bundle-aware)."""
    bundle = _bundle_dir()
    if bundle:
        candidate = bundle / "bin" / "ocrmypdf"
        if candidate.exists():
            return str(candidate)
    found = shutil.which("ocrmypdf")
    if found:
        return found
    candidate = Path.home() / ".local" / "bin" / "ocrmypdf"
    if candidate.exists():
        return str(candidate)
    raise RuntimeError(
        "ocrmypdf not found. Install with `brew install ocrmypdf` (Mac) or "
        "`apt install ocrmypdf` (Linux), or `pip install ocrmypdf` plus tesseract+ghostscript."
    )


def prepare(input_pdf: Path, output_pdf: Path | None = None) -> Path:
    """
    OCR a C1 design PDF to produce a searchable version.

    Tries the ocrmypdf Python API first (available when bundled or when the
    package is installed). Falls back to the CLI binary for dev environments
    that have only the system brew install.
    """
    input_pdf = Path(input_pdf).resolve()
    if not input_pdf.exists():
        raise FileNotFoundError(input_pdf)
    if output_pdf is None:
        output_pdf = input_pdf.with_name(input_pdf.stem + "_searchable.pdf")
    else:
        output_pdf = Path(output_pdf).resolve()

    print(f"  Converting {input_pdf.name} to searchable (ocrmypdf)...")

    try:
        import ocrmypdf
        _patch_ocrmypdf_setup_plugins()
        ocrmypdf.ocr(
            input_pdf,
            output_pdf,
            force_ocr=True,
            max_image_mpixels=0,  # disable Pillow decompression-bomb guard for large design sheets
            output_type="pdf",
            progress_bar=False,
        )
    except ImportError:
        # Dev fallback: use system CLI
        cmd = [
            find_ocrmypdf(),
            "--force-ocr",
            "--max-image-mpixels", "0",
            "--output-type", "pdf",
            "--quiet",
            str(input_pdf),
            str(output_pdf),
        ]
        print(f"  Running: {' '.join(cmd)}")
        subprocess.run(cmd, check=True)

    print(f"✓ Searchable PDF written: {output_pdf}")
    return output_pdf


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python prepare_pdf.py <input_pdf> [output_pdf]")
        sys.exit(1)
    in_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    prepare(in_path, out_path)
