# DMP WS & Door Chart Generator

Desktop app (macOS + Windows) that turns a security-system design PDF into two
Excel deliverables: a **DMP Installation Worksheet** and a **Door Chart**.

One codebase runs on both operating systems — platform differences are handled
at runtime via `sys.platform` checks. The app is built into a native `.app`
(macOS) or a one-folder app — a folder holding the `.exe` (Windows).

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
   **More info → Run anyway** (one click, not an install). Code-signed
   releases (see below) do not show this.

They never touch GitHub or the source code.

The build is deliberately **one-folder** (not one-file) and **UPX-free** so it
is not blocked by AppLocker `%TEMP%` rules or flagged as a false positive by
antivirus on managed/corporate Windows machines.

## Code signing (Windows — optional, recommended for wide sharing)

Released Windows builds are code-signed automatically **if** Azure Trusted
Signing secrets are configured on the repo. A signed `.exe` has a verified
publisher, which clears SmartScreen's "unknown publisher" warning and stops
antivirus/EDR from flagging it on managed machines.

One-time setup:

1. Create an **Azure Trusted Signing** account (~$10/month), add a certificate
   profile, and complete the one-time identity validation.
2. Create an Azure AD app registration with access to the signing account.
3. Add these repository secrets (Settings → Secrets and variables → Actions):
   - `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`
   - `AZURE_TS_ENDPOINT` — e.g. `https://eus.codesigning.azure.net`
   - `AZURE_TS_ACCOUNT` — Trusted Signing account name
   - `AZURE_TS_PROFILE` — certificate profile name

`release.yml` signs the `.exe` only when these secrets exist; without them the
build still succeeds and produces an unsigned `.zip`.

## Output location

Generated worksheets and door charts are written to a folder the user picks
in-app (**"Save output to → Change…"**), stored per machine. The default is
`~/Documents/DMP WS & Door Chart Generator/`. Point it at a OneDrive folder to
sync deliverables across machines, or keep it local.
