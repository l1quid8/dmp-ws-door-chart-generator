import sys
import os
import sysconfig
import subprocess
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

block_cipher = None
APP_NAME = "DMP WS & Door Chart Generator"
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

# Single source of truth for the version (shown in the title bar and Info.plist)
VERSION = Path("VERSION").read_text().strip() if Path("VERSION").exists() else "0.0.0"

# Icon paths (absolute, so PyInstaller's relative-path resolution doesn't matter)
ICON_ICNS = str(Path("logos/icons/app-icon.icns").resolve())
# Windows EXE icon (Explorer / taskbar / Alt-Tab). Same file as the in-window
# title-bar icon set at runtime in app.py, so the app icon is consistent.
ICON_ICO = str(Path("logos/icons/app-icon.ico").resolve())

# ── customtkinter assets ─────────────────────────────────────────────────────
# Locate via sysconfig to avoid importing the package at spec-parse time
# (importing customtkinter requires _tkinter which Homebrew Python may not have)
_site = sysconfig.get_paths()["purelib"]
CTK_DIR = str(Path(_site) / "customtkinter")

# ── OCR toolchain (platform-specific) ────────────────────────────────────────
ocr_binaries = []
ocr_datas = []

if IS_MAC:
    def _brew_prefix(pkg):
        return subprocess.check_output(["brew", "--prefix", pkg], text=True).strip()

    tess_prefix = Path(_brew_prefix("tesseract"))
    gs_prefix   = Path(_brew_prefix("ghostscript"))

    # Binaries — PyInstaller follows .dylib dependencies automatically
    ocr_binaries += [
        (str(tess_prefix / "bin" / "tesseract"), "bin"),
        (str(gs_prefix   / "bin" / "gs"),        "bin"),
    ]
    # Tesseract data files — keep subdir structure (configs/ is required for hocr/txt outputs)
    tessdata = tess_prefix / "share" / "tessdata"
    for f in tessdata.rglob("*"):
        if f.is_file():
            rel_parent = f.relative_to(tessdata).parent
            dest = f"share/tessdata/{rel_parent}" if str(rel_parent) != "." else "share/tessdata"
            ocr_datas.append((str(f), dest))
    # Ghostscript runtime resources
    gs_share = gs_prefix / "share" / "ghostscript"
    for subdir in ("lib", "Resource", "fonts", "iccprofiles"):
        p = gs_share / subdir
        if p.exists():
            ocr_datas.append((str(p), f"share/ghostscript/{subdir}"))

elif IS_WIN:
    # Standard install locations from the UB Mannheim and Artifex installers
    import glob as _glob

    tess_dir = Path(r"C:\Program Files\Tesseract-OCR")
    if tess_dir.exists():
        ocr_binaries.append((str(tess_dir / "tesseract.exe"), "bin"))
        # Walk recursively so subdirs (configs/, script/, tessconfigs/) come along.
        # configs/hocr and configs/txt are required for ocrmypdf's output formats.
        tessdata = tess_dir / "tessdata"
        for f in tessdata.rglob("*"):
            if f.is_file():
                rel_parent = f.relative_to(tessdata).parent
                dest = f"share/tessdata/{rel_parent}" if str(rel_parent) != "." else "share/tessdata"
                ocr_datas.append((str(f), dest))

    gs_dirs = _glob.glob(r"C:\Program Files\gs\gs*")
    if gs_dirs:
        gs_dir = Path(sorted(gs_dirs)[-1])  # latest version
        ocr_binaries.append((str(gs_dir / "bin" / "gswin64c.exe"), "bin"))
        gs_lib = gs_dir / "lib"
        if gs_lib.exists():
            ocr_datas.append((str(gs_lib), "share/ghostscript/lib"))
        gs_res = gs_dir / "Resource"
        if gs_res.exists():
            ocr_datas.append((str(gs_res), "share/ghostscript/Resource"))

# ─────────────────────────────────────────────────────────────────────────────

# Force-include ALL ocrmypdf submodules so its plugin discovery
# (pkgutil.iter_modules over ocrmypdf.builtin_plugins) finds tesseract_ocr.
# include_py_files=True extracts .py files to disk so pkgutil can enumerate them.
ocrmypdf_modules = collect_submodules("ocrmypdf")
ocrmypdf_data = collect_data_files("ocrmypdf", include_py_files=True)
pdfminer_modules = collect_submodules("pdfminer")  # ocrmypdf dependency

a = Analysis(
    ["scripts/app.py"],
    pathex=[str(Path(".").resolve())],
    binaries=ocr_binaries,
    datas=[
        ("DMP Installation Worksheet_template_blank.xlsx", "."),
        ("door_chart_template_blank.xlsx", "."),
        ("VERSION", "."),
        ("logos", "logos"),
        (CTK_DIR, "customtkinter"),
    ] + ocr_datas + ocrmypdf_data,
    hiddenimports=[
        "customtkinter",
        "PIL",
        "PIL._imagingtk",
        "PIL.ImageTk",
        "openpyxl",
        "openpyxl.cell._writer",
        "fitz",
        "pymupdf",
    ] + ocrmypdf_modules + pdfminer_modules,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

if IS_MAC:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_NAME,
        icon=ICON_ICNS,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        argv_emulation=False,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        name=APP_NAME,
    )
    app = BUNDLE(
        coll,
        name=APP_NAME + ".app",
        icon=ICON_ICNS,
        bundle_identifier="com.convergeone.dmp-worksheet-doorchart",
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "CFBundleName": APP_NAME,
            "CFBundleShortVersionString": VERSION,
            "CFBundleIconFile": "app-icon.icns",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "10.13",
        },
    )
else:
    # Windows: one-folder build — a folder containing the .exe plus _internal/.
    # One-folder (not one-file) so the app does NOT unpack itself into %TEMP%
    # at launch; corporate AppLocker policies commonly block execution from
    # %TEMP%. UPX is disabled because UPX-packed binaries are a frequent
    # antivirus false-positive trigger. Both changes make the build far less
    # likely to be blocked on managed/corporate Windows machines.
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_NAME,
        icon=ICON_ICO,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=False,
        name=APP_NAME,
    )
