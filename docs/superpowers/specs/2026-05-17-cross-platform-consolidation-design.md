# Cross-Platform Consolidation — Design

**Date:** 2026-05-17
**Status:** Approved, pending implementation plan

## Problem

The project exists as two diverging copies: a macOS copy at
`~/Documents/Claude/Projects/DMP Worksheet and Door Chart Generator/` and a
Windows copy at `OneDrive-ConvergeOne/DMP & Door Chart WS/Windows/DMP & Door Chart WS/`.
Editing both by hand produces inconsistent changes (drift). The goal: a single
codebase that runs on macOS and Windows, editable from any of the user's three
machines (personal macOS, Windows VM, Windows work computer) without manual
back-and-forth.

## Current divergence (baseline)

Comparison of the two copies (Windows copy = `Windows/DMP & Door Chart WS/`;
the sibling `__MACOSX/` folder is zip junk and is ignored):

**Identical:** `build_icons.py`, `extract_topology.py`, `generate_dmp_ws.py`,
`generate_door_chart.py`, `inject_door_chart.py`, `parse_dmp_worksheet.py`,
`parse_zone_schedule.py`, `rthook_ocr.py`, both `docs/` markdown files,
`requirements.txt`, `entitlements.plist`.

**Divergent (4 files):**

- `app.py` — Windows adds a `sys.platform`-guarded title-bar `iconbitmap`,
  crash logging to `output/debug.log` in `_run_async`, and uses the shorter app
  name.
- `prepare_pdf.py` — Windows adds `_silence_subprocess_consoles_on_windows()`
  (monkeypatches `subprocess.Popen` with `CREATE_NO_WINDOW`), guarded by
  `sys.platform`.
- `paths.py` — differs only in the app-name string.
- `dmp_doorchart.spec` — Windows renames `APP_NAME`, adds an EXE icon, and uses
  a recursive `tessdata` glob (`rglob`, preserving `configs/` subdirs). The Mac
  copy's flat `glob("*")` is a bug — it omits `configs/hocr` and `configs/txt`,
  which ocrmypdf requires.

**Windows-only file:** `scripts/build_exe_icon.py` (builds the `.exe` icon).

Every Windows-specific behaviour is already `sys.platform`-guarded, so a single
merged codebase requires no per-OS file forks.

## Decisions

| Topic | Decision |
|---|---|
| App name | **DMP WS & Door Chart Generator** everywhere (title, built app/exe, repo). |
| Run model | Build a native `.app`/`.exe` per machine. OneDrive holds source-equivalent code in git, not builds. |
| Build output location | Build script copies the finished app to `~/Applications/` (macOS) or the Desktop (Windows). |
| Output files | Configurable per machine via an in-app setting (local or cloud folder). |
| Sync mechanism | Private GitHub repo, cloned **outside** OneDrive on each machine. |
| Distribution | GitHub Actions CI builds both OSes on a version tag and publishes to GitHub Releases. |
| Hardening | Pin `requirements.txt` to exact versions; add a `VERSION` file shown in the UI. |

## The reframe: a GitHub project, not a OneDrive project

The consolidated project's home is a **private GitHub repo**, not OneDrive:

- **Code** → private GitHub repo, cloned outside OneDrive on each machine.
- **Builds** → GitHub Releases, produced by CI.
- **OneDrive** → optional, output files only. If a machine's output setting
  points at a OneDrive folder, finished worksheets sync across machines. This is
  OneDrive's only remaining role and it is optional per machine.

The OneDrive `DMP & Door Chart WS/` folder is retired as a code location.

## Migration

1. The current macOS folder (`~/Documents/Claude/Projects/DMP Worksheet and Door Chart Generator/`)
   is already outside OneDrive. It is promoted to the git repo: merge the
   divergent files, add the new files, add `.gitignore`, `git init`, and push to
   a new private GitHub repo (suggested name `dmp-ws-door-chart-generator`).
2. The OneDrive `Windows/DMP & Door Chart WS/` copy and the `__MACOSX/` folder
   are abandoned (user may delete them afterward).
3. Other machines: `git clone` into `~/Projects/` and run the build script once.

## Components

### 1. Merged cross-platform code

- The 4 divergent files become single copies that keep every Windows-specific
  behaviour behind `sys.platform == "win32"` guards: title-bar icon, console
  suppression, crash logging.
- `build_exe_icon.py` is kept; it only runs during Windows builds.
- `dmp_doorchart.spec` is unified — branches on `sys.platform` for the
  `.exe`+icon path vs. the `.app`+`.icns` path, and uses the recursive
  `tessdata` glob (the fixed behaviour) on both OSes.
- All user-facing and build-output names become **DMP WS & Door Chart Generator**.
- `paths.py` uses the unified name.

### 2. Configurable output directory

- A new `output_dir` key in the existing per-machine prefs file
  (`~/.c1_door_chart_app.json`).
- `paths.output_dir()` returns the saved pref if set; otherwise the default
  `~/Documents/DMP WS & Door Chart Generator/`. It still creates the directory
  if missing.
- The app's info panel gains a row: **"Save output to: `<path>`  [Change…]"**.
  The button opens a native folder picker (`tkinter.filedialog.askdirectory`);
  the chosen path is saved to prefs immediately.
- The post-generation confirmation message shows the actual chosen path instead
  of the hard-coded `output/`.

### 3. Per-machine build scripts

`build_mac.command` and `build_windows.bat` at the repo root:

1. Create a build virtualenv at `~/.dmp-doorchart/venv` (local, never on
   OneDrive, never in git).
2. `pip install -r requirements.txt` into it.
3. Run PyInstaller with `dmp_doorchart.spec`. Build scratch (`build/`, `dist/`)
   stays under `~/.dmp-doorchart/`.
4. Copy the finished `.app`/`.exe` to `~/Applications/` (macOS) or the Desktop
   (Windows).

These scripts are the only supported way to build locally and also serve as the
fallback if CI is unavailable.

### 4. Hardening

- `requirements.txt` pinned to exact versions (`==`) so every build on every
  machine and on CI resolves identical packages. Build with Python 3.13 on all
  machines (documented in the README).
- A `VERSION` file at the repo root containing a semantic version (initial
  `1.0.0`). The app reads it and shows it in the title bar. Bumped per release.

### 5. GitHub Actions CI — `.github/workflows/release.yml`

- Triggers on pushing a version tag (e.g. `v1.0.1`).
- A macOS runner builds the `.app`; a Windows runner builds the `.exe`. Both
  jobs use the same `dmp_doorchart.spec` and pinned `requirements.txt`.
- Each artifact is zipped and attached to a GitHub Release for that tag.
- The user and colleague download builds from Releases; per-machine building
  becomes optional.

### 6. Repository layout

```
dmp-ws-door-chart-generator/        (git repo, cloned outside OneDrive)
├── .github/workflows/release.yml
├── .gitignore                      (venv/ build/ dist/ input/ output/ *.log
│                                    .DS_Store __pycache__/ *.pyc)
├── scripts/                        (all .py source — one cross-platform copy)
├── logos/                          (images + Mac .icns and Win .ico icons)
├── docs/
├── DMP Installation Worksheet_template_blank.xlsx
├── door_chart_template_blank.xlsx
├── dmp_doorchart.spec              (one spec, OS-branched internally)
├── requirements.txt                (pinned)
├── entitlements.plist
├── VERSION
├── build_mac.command
├── build_windows.bat
└── README.md                       (new-machine setup)
```

Nothing platform-specific or large is ever committed: venvs, build scratch,
build output, sample input PDFs, generated output, and OS junk are all
`.gitignore`d.

## New-machine setup (for the README)

1. Install Python 3.13 and Git (or GitHub Desktop).
2. `git clone` the repo into `~/Projects/`.
3. Double-click `build_mac.command` or `build_windows.bat`.
4. Launch the app from `~/Applications/` (macOS) or the Desktop (Windows).

Updating: `git pull`, then re-run the build script — or download the latest
build from GitHub Releases.

## Error handling

- Build scripts fail loudly if Python 3.13 is missing or `pip install` /
  PyInstaller fails; they print the failing step.
- The output-folder picker: if the user cancels, the existing setting is kept.
  If the chosen folder is later unwritable at generation time, the app surfaces
  the error through its existing error-card UI.
- CI: a build failure on either OS fails the workflow and produces no Release,
  so a broken build is never published.

## Testing

- After consolidation, smoke-test on both OSes: build via the script, launch,
  load a sample PDF, generate a DMP worksheet and a door chart, confirm both
  files land in the configured output folder.
- Verify the output-folder picker persists across app restarts and that a
  OneDrive-pointed folder receives generated files.
- Verify CI produces a Release with both a macOS and a Windows artifact on a
  test tag.

## Out of scope

- Code signing / notarization (Gatekeeper and SmartScreen warnings are clicked
  through for internal use).
- Auto-update frameworks (Sparkle / WinSparkle).
- Migrating the project to git history from the old folders — the repo starts
  fresh at `1.0.0`.
