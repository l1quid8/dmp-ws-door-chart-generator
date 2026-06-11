# DMP WS & Door Chart Generator

Desktop app (macOS + Windows) that turns a security-system design PDF into two
Excel deliverables: a **DMP Installation Worksheet** and a **Door Chart**.

One codebase runs on both operating systems — platform differences are handled
at runtime via `sys.platform` checks. The app is built into a native `.app`
(macOS) or a one-folder app — a folder holding the `.exe` (Windows).

## Application shell (v1.2)

Landscape window (1000×680) with desktop-app chrome: a slim toolbar (brand
mark, project name with unsaved-changes dot, Open / Save / Export Draft /
Finalize), the editor filling the window, and a status bar (save state,
validation chips, collapsible terminal). The full ConvergeOne logo appears
only on the home screen.

## Hardware changes (v1.3)

Post-CAD hardware changes happen in the editor: add or remove **714-16/714-8
expanders** (each brings its RSP + power supply + zone block — DMP bus
addressing: module 7 starts Z601), **710 splitters** (LX or KP), and
**keypads**. Removal scrubs dangling references (splitter outputs → Spare,
orphaned keypad sources flagged by the finalize gate). Template capacities
are enforced: 15 expanders, 12 splitters per type, 28 keypads.

## Field-edit workflow (v1.1)

The app is the working document; the Excel files are output artifacts.

1. **Import** — drop a design PDF, a DMP worksheet (`.xlsx`), or a saved
   project (`.dmps`). Parsing lands in a tabbed editor (SITE / ZONES /
   SPLITTERS / KEYPADS / POWER).
2. **Edit & save** — correct what the prints got wrong (zone descriptions,
   splitter wiring, RSP locations). Explicit save (`Ctrl/Cmd+S`) writes a
   `.dmps` project file (plain JSON) under `<output>/Sessions/`; a background
   recovery file guards against crashes. Projects reopen from the home
   screen's Recent Projects list, across days and site visits.
3. **Export Draft** — anytime; the file is stamped `DRAFT`, carries a
   NOT-FOR-INSTALL banner, and is refused on re-import (the project file is
   the source of truth).
4. **Finalize** — a validation gate (required IP/gateway/tech/date, no blank
   or placeholder zone descriptions, `RSP-N`/`SPARE` naming, conflicts
   resolved, wiring reviewed) must pass before the `FINAL`-stamped worksheet
   and door chart are generated.

## Repository layout

| Path | Purpose |
|---|---|
| `scripts/` | All Python source (one cross-platform copy) |
| `dmp_doorchart.spec` | PyInstaller build spec (OS-branched internally) |
| `requirements.txt` | Pinned dependencies — build with **Python 3.13** |
| `VERSION` | App version, shown in the title bar |
| `build_mac.command` / `build_windows.bat` | Per-machine build scripts |
| `.github/workflows/release.yml` | CI: builds both OSes on a version tag |
| `logos/`, `*.xlsx` | Branding assets and Excel templates |
| `docs/` | Design specs |

Build output, virtualenvs, and working data are **not** committed — see
`.gitignore`. Each machine builds locally; nothing platform-specific is shared.

## Developer setup (new machine)

1. Install **Python 3.13** and **Git** (or GitHub Desktop).
2. Install the OCR tools:
   - macOS: `brew install tesseract ghostscript`
   - Windows: Tesseract-OCR (UB Mannheim build) and Ghostscript, default paths.
3. Clone this repo (anywhere **outside** OneDrive, e.g. `~/Projects/`).
4. Build:
   - macOS: double-click `build_mac.command` → app installed to `~/Applications/`
   - Windows: double-click `build_windows.bat` → app folder copied to your Desktop
5. Launch the built app.

The build script creates its own virtualenv at `~/.dmp-doorchart/` (local, never
synced). First build takes a few minutes; rebuilds are faster.

**Updating:** `git pull`, then re-run the build script — or download a build
from GitHub Releases.

## Releasing

Push a version tag to build both platforms via CI and publish a Release:

```
# bump VERSION first (e.g. to 1.0.1), commit, then:
git tag v1.0.1
git push origin v1.0.1
```

GitHub Actions builds the macOS `.app` and Windows `.exe` and attaches both to a
GitHub Release for that tag. The Release is the version archive.

## Sharing with a colleague (Windows, no install)

The Windows build is a **self-contained app folder** — it bundles Python, all
libraries, and the OCR tools (Tesseract + Ghostscript). The recipient needs
nothing installed. It ships as a `.zip` (one folder holding the `.exe` plus an
`_internal/` folder of bundled files).

To share: download `DMP-WS-Door-Chart-Generator-Windows.zip` from the GitHub
Release and send it over **Teams** (email servers block executables). They:

1. Save the `.zip` and **extract it** (right-click → Extract All). Keep the
   `.exe` and the `_internal/` folder together — the app needs both.
2. Open the extracted folder and double-click the `.exe`.
3. If Windows SmartScreen shows "Windows protected your PC", click
   **More info → Run anyway** (one click, not an install).

They never touch GitHub or the source code.

The build is deliberately **one-folder** (not one-file) and **UPX-free** so it
is not blocked by AppLocker `%TEMP%` rules or flagged as a false positive by
antivirus on managed/corporate Windows machines.

## Output location

Generated worksheets and door charts are written to a folder the user picks
in-app (**"Save output to → Change…"**), stored per machine. The default is
`~/Documents/DMP WS & Door Chart Generator/`. Point it at a OneDrive folder to
sync deliverables across machines, or keep it local.
