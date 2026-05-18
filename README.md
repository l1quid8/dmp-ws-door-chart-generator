# DMP WS & Door Chart Generator

Desktop app (macOS + Windows) that turns a security-system design PDF into two
Excel deliverables: a **DMP Installation Worksheet** and a **Door Chart**.

One codebase runs on both operating systems — platform differences are handled
at runtime via `sys.platform` checks. The app is built into a native `.app`
(macOS) or single-file `.exe` (Windows).

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
   - Windows: double-click `build_windows.bat` → app copied to your Desktop
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

The Windows build is a **single self-contained `.exe`** — it bundles Python, all
libraries, and the OCR tools (Tesseract + Ghostscript). The recipient needs
nothing installed.

To share: download the `.exe` from the GitHub Release and send it over
**Teams** (email servers block `.exe` attachments). They:

1. Save the `.exe` anywhere (Desktop, etc.).
2. Double-click it.
3. On first run of a version, Windows SmartScreen shows "Windows protected your
   PC" — click **More info → Run anyway**. This is expected for an unsigned
   build; it is one click, not an install.

They never touch GitHub or the source code.

## Output location

Generated worksheets and door charts are written to a folder the user picks
in-app (**"Save output to → Change…"**), stored per machine. The default is
`~/Documents/DMP WS & Door Chart Generator/`. Point it at a OneDrive folder to
sync deliverables across machines, or keep it local.
