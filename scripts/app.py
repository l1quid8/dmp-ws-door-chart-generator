import sys
import os
import json
import threading
import subprocess
import contextlib
import tempfile
import webbrowser
from datetime import date
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from tkinterdnd2 import TkinterDnD, DND_FILES

sys.path.insert(0, str(Path(__file__).parent))

from paths import resource_path, output_dir, next_rev_path, latest_rev_path
from generate_dmp_ws import (
    build_dmp_design_from_pdf,
    write_dmp_xlsx,
    ensure_searchable_pdf,
    resolve_original_pdf,
    DEFAULT_TEMPLATE,
)
from parse_dmp_worksheet import parse_dmp_worksheet, worksheet_looks_like_dmp
from inject_door_chart import inject, _slugify
from session import (
    SESSION_EXT,
    Session,
    SessionLoadError,
    clear_recovery,
    ensure_editable_zones,
    list_recent_sessions,
    load_recovery,
    load_session,
    normalize_rsp_tokens,
    normalize_zone_descriptions,
    pending_recovery,
    sync_master_zones,
    unique_session_path,
)
from editor_frame import EditorFrame
from rl_injector.xml_export import generate_account_xml
import updater

DOOR_CHART_TEMPLATE = resource_path("door_chart_template_blank.xlsx")
PREFS_PATH = Path.home() / ".c1_door_chart_app.json"
ACCENT = "#4a7bb8"
ACCENT_HOVER = "#3a6aa8"
SPINNER_FRAMES = list("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

_WORKFLOW_HELP = """The editor is the working document. The Excel files are artifacts \
generated from it — they are never re-imported, so always re-open the .dmps project to \
make changes.

1. IMPORT
   Drop a design PDF, an existing DMP worksheet (.xlsx), or a saved project (.dmps) onto \
the home screen. The app parses it and detects the school name.

2. EDIT  (five tabs)
   • SITE — school, address, contact, tech, install date, IP / gateway, XR-550 location.
   • ZONES — searchable grid of every zone. Filter chips: All / Needs attention (blank or \
"NEW" description) / Spares / Errors. Double-click a cell to edit.
   • SPLITTERS — splitter wiring and CAD conflicts. Tick "Wiring reviewed" once you've \
checked it against the riser diagram (required before FINAL).
   • KEYPADS — each keypad's location and source (MSP or a KP splitter).
   • POWER — RSP / power-supply locations; add or remove expanders here.

   Naming rules the checks enforce: SPARE must be uppercase, and RSP references \
must be hyphenated (RSP-3, not RSP 3).

3. SAVE
   Saving is explicit — click Save (or Ctrl/Cmd+S). The orange dot and "Unsaved changes" \
mean you have edits that aren't on disk yet. A background recovery file guards against \
crashes between saves.

4. GENERATE  (repeat as needed)
   Two buttons at the bottom of the editor: "Generate Worksheet" and "Generate Door \
Chart" (the chart is built from the newest worksheet). Each run writes the next revision \
— school_dmp_rev1.xlsx, rev2, … — keeping earlier revisions, so the normal loop is: \
generate, print, review with the superintendent, edit, regenerate. If checks are failing \
you'll see a summary first, but generation is never blocked. You stay in the editor the \
whole time; a notification offers to open the finished file.

Hardware changes (post-CAD): you can add or remove expanders, splitters, and keypads. \
Removing hardware re-points anything that fed it to "Spare" and unsources affected \
keypads — the app pops a summary and routes you to review the new wiring. Template \
capacities: 15 expanders, 12 LX + 12 KP splitters, 28 keypads."""

ctk.set_appearance_mode("light")
ctk.set_default_color_theme("blue")


class CTkDnD(ctk.CTk, TkinterDnD.DnDWrapper):
    """customtkinter root with tkinterdnd2's bundled tkdnd loaded.

    tkinterdnd2 ships the tkdnd binaries cross-platform, so drag-and-drop works
    in the packaged build without relying on a system-installed Tcl extension.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.TkdndVersion = TkinterDnD._require(self)


def open_file(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    elif sys.platform == "win32":
        os.startfile(str(path))
    else:
        subprocess.Popen(["xdg-open", str(path)])


def load_prefs() -> dict:
    try:
        return json.loads(PREFS_PATH.read_text())
    except Exception:
        return {}


def save_prefs(data: dict) -> None:
    try:
        PREFS_PATH.write_text(json.dumps(data))
    except Exception:
        pass


def _app_version() -> str:
    """Read the app version from the bundled VERSION file (empty if missing)."""
    try:
        return resource_path("VERSION").read_text().strip()
    except Exception:
        return ""


class TextRedirector:
    def __init__(self, widget, root):
        self._widget = widget
        self._root = root

    def write(self, s):
        def _insert():
            self._widget.configure(state="normal")
            self._widget.insert("end", s)
            self._widget.see("end")
            self._widget.configure(state="disabled")
        self._root.after(0, _insert)

    def flush(self):
        pass


class App:
    def __init__(self):
        self.root = CTkDnD()
        _version = _app_version()
        self.root.title("DMP WS & Door Chart Generator" + (f"  v{_version}" if _version else ""))
        self.root.geometry("1000x680")
        self.root.minsize(860, 560)
        self.root.resizable(True, True)

        # Windows title-bar icon (distinct from the .exe's embedded icon by design).
        if sys.platform == "win32":
            with contextlib.suppress(Exception):
                self.root.iconbitmap(str(resource_path("logos/icons/toolbar-icon.ico")))

        self.state = "idle"
        self.pdf_path: Path | None = None
        self.dmp_path: Path | None = None
        self.door_chart_path: Path | None = None
        self.parsed_design = None
        self.session: Session | None = None
        self.editor: EditorFrame | None = None

        # Which artifact is generating right now: "worksheet" | "chart" | None.
        # Generation runs in the background while the editor stays up.
        self._generating: str | None = None
        # editor.edit_epoch at the moment the last worksheet was generated —
        # lets the door-chart action warn when the worksheet has gone stale.
        self._ws_epoch: int | None = None

        self._spinner_jobs: list[str] = []

        # Output folder — configurable per machine via the meta section picker.
        self.output_dir: Path = output_dir()

        self._build_layout()
        self._show_drop_zone()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Route EVERY quit path through _on_close/_shutdown. The red close button
        # fires WM_DELETE_WINDOW, but the macOS Apple-menu Quit and Cmd-Q hit Tk's
        # default handler, which tears down Tcl (Tcl_Exit -> C exit()) while the
        # Python interpreter is finalizing — that runs PyMuPDF's global-context
        # destructor, which flushes buffered MuPDF warnings back into a dead
        # interpreter and segfaults. Intercepting the Apple-menu Quit makes those
        # paths use our hard-exit shutdown instead.
        if sys.platform == "darwin":
            try:
                self.root.createcommand("::tk::mac::Quit", self._on_close)
            except Exception:
                pass

        # Tk swallows exceptions raised inside widget callbacks, which reads
        # as "the button does nothing". Surface them instead.
        self.root.report_callback_exception = self._on_ui_exception

        # Manual-save shortcut (active only while the editor is open).
        shortcut = "<Command-s>" if sys.platform == "darwin" else "<Control-s>"
        self.root.bind_all(shortcut, self._save_shortcut)

        # Menu-bar shortcuts (mirror File + Worksheet). Handlers self-guard.
        mod = "Command" if sys.platform == "darwin" else "Control"
        self.root.bind_all(f"<{mod}-n>", lambda _e=None: self._process_another())
        self.root.bind_all(f"<{mod}-o>", lambda _e=None: self._choose_pdf())
        self.root.bind_all(f"<{mod}-w>", lambda _e=None: self._process_another())
        self.root.bind_all(f"<{mod}-e>", lambda _e=None: self._generate_worksheet())
        self.root.bind_all(f"<{mod}-d>", lambda _e=None: self._generate_door_chart())

        try:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self._on_drop)
        except Exception as e:
            print(f"Drag-and-drop unavailable: {e}", file=sys.stderr)

        # Background update check shortly after the window is up (silent: only
        # surfaces a dialog when a newer release exists and wasn't skipped).
        self._update_dialog: ctk.CTkToplevel | None = None
        self.root.after(1200, lambda: self._check_for_updates(silent=True))

    # ------------------------------------------------------------------ #
    # Layout shell                                                          #
    # ------------------------------------------------------------------ #

    def _build_layout(self):
        """Application chrome: toolbar / main / collapsible terminal / status bar.

        The main area swaps between two surfaces: a centered scrollable "flow"
        column (home, parsing, generation progress, review/done screens) and
        the full-bleed project editor.
        """
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        self._build_menubar()
        self._build_toolbar()

        ctk.CTkFrame(self.root, height=1, corner_radius=0,
                     fg_color=("gray80", "gray28")).grid(row=1, column=0, sticky="ew")

        # Main area
        self.main = ctk.CTkFrame(self.root, fg_color="transparent")
        self.main.grid(row=2, column=0, sticky="nsew")
        self.main.columnconfigure(0, weight=1)
        self.main.rowconfigure(0, weight=1)

        # Centered flow column (home / progress / review screens). A plain
        # frame — content is small and fixed, and CTkScrollableFrame's
        # always-visible scrollbar trough reads as an empty panel. Top-anchored
        # via sticky n; main's weight-1 column centers it horizontally.
        self.flow = ctk.CTkFrame(self.main, fg_color="transparent")
        self.flow.grid(row=0, column=0, sticky="n")
        self.flow.columnconfigure(0, weight=1, minsize=560)

        self.input_section = ctk.CTkFrame(self.flow, fg_color="transparent")
        self.input_section.grid(row=0, column=0, sticky="ew", pady=(16, 0))
        self.input_section.columnconfigure(0, weight=1)

        self.action_section = ctk.CTkFrame(self.flow, fg_color="transparent")
        self.action_section.columnconfigure(0, weight=1)

        # Full-bleed editor host (gridded when a project is open)
        self.editor_section = ctk.CTkFrame(self.main, fg_color="transparent")
        self.editor_section.columnconfigure(0, weight=1)
        self.editor_section.rowconfigure(0, weight=1)

        # Collapsible terminal panel
        self.term_section = ctk.CTkFrame(self.root, fg_color="transparent")
        self.term_section.columnconfigure(0, weight=1)
        self._term_visible = False
        self._build_terminal()

        ctk.CTkFrame(self.root, height=1, corner_radius=0,
                     fg_color=("gray80", "gray28")).grid(row=4, column=0, sticky="ew")
        self._build_statusbar()

    def _build_menubar(self):
        """Native menu bar (File + Worksheet) — the app's primary controls.

        Every command reuses an existing handler that already runs its own
        guards, so switching/closing mid-edit still prompts to save and the
        worksheet actions stay no-ops when no project is open.
        """
        is_mac = sys.platform == "darwin"
        accel = (lambda key: f"Cmd+{key}") if is_mac else (lambda key: f"Ctrl+{key}")

        self._menubar = tk.Menu(self.root)

        # ---- Application menu (macOS) ----
        # On macOS "Check for Updates…" belongs in the bold app-name menu — the
        # native home users expect (matching Sparkle apps). Commands added to a
        # menu named "apple" appear at the top of that application menu.
        if is_mac:
            app_menu = tk.Menu(self._menubar, name="apple")
            self._menubar.add_cascade(menu=app_menu)
            app_menu.add_command(
                label="Check for Updates…",
                command=lambda: self._check_for_updates(silent=False))

        # ---- File ----
        file_menu = tk.Menu(self._menubar, tearoff=0,
                            postcommand=self._refresh_file_menu)
        self._file_menu = file_menu
        self._recent_menu = tk.Menu(file_menu, tearoff=0)

        file_menu.add_command(label="New Project", accelerator=accel("N"),
                              command=self._process_another)
        file_menu.add_command(label="Open…", accelerator=accel("O"),
                              command=self._choose_pdf)
        file_menu.add_cascade(label="Open Recent", menu=self._recent_menu)
        file_menu.add_separator()
        file_menu.add_command(label="Close Project", accelerator=accel("W"),
                              command=self._process_another)
        file_menu.add_command(label="Save", accelerator=accel("S"),
                              command=self._save_shortcut)
        file_menu.add_command(label="Revert to Saved…",
                              command=self._revert_clicked)
        file_menu.add_separator()
        file_menu.add_command(label="Quit", accelerator=accel("Q"),
                              command=self._on_close)
        self._menubar.add_cascade(label="File", menu=file_menu)

        # ---- Worksheet ----
        ws_menu = tk.Menu(self._menubar, tearoff=0,
                          postcommand=self._refresh_worksheet_menu)
        self._worksheet_menu = ws_menu
        ws_menu.add_command(label="Generate Worksheet", accelerator=accel("E"),
                            command=self._generate_worksheet)
        ws_menu.add_command(label="Generate Door Chart", accelerator=accel("D"),
                            command=self._generate_door_chart)
        self._menubar.add_cascade(label="Worksheet", menu=ws_menu)

        # ---- Help (always present — lowers the README-dependence) ----
        help_menu = tk.Menu(self._menubar, tearoff=0)
        help_menu.add_command(label="Field-Edit Workflow…",
                              command=self._show_workflow_help)
        help_menu.add_command(label="Keyboard Shortcuts…",
                              command=self._show_shortcuts_help)
        help_menu.add_command(label="Open README", command=self._open_readme)
        # On macOS "Check for Updates…" already lives in the app menu above.
        if not is_mac:
            help_menu.add_separator()
            help_menu.add_command(
                label="Check for Updates…",
                command=lambda: self._check_for_updates(silent=False))
        self._menubar.add_cascade(label="Help", menu=help_menu)

        self.root.configure(menu=self._menubar)

    def _refresh_file_menu(self):
        """Rebuild dynamic File-menu state each time it opens."""
        editing = self.state == "editing" and self.editor is not None
        state = "normal" if editing else "disabled"
        self._file_menu.entryconfigure("Save", state=state)
        self._file_menu.entryconfigure("Close Project", state=state)
        can_revert = editing and self.session is not None and self.session.path
        self._file_menu.entryconfigure(
            "Revert to Saved…", state="normal" if can_revert else "disabled")

        self._recent_menu.delete(0, "end")
        recents = list_recent_sessions(limit=10)
        if not recents:
            self._recent_menu.add_command(label="(No recent projects)",
                                          state="disabled")
            return
        for summary in recents:
            self._recent_menu.add_command(
                label=summary.school_name or "(untitled)",
                command=lambda p=summary.path: self._open_session_path(p),
            )

    def _refresh_worksheet_menu(self):
        """Enable the worksheet actions only while a project is open and idle."""
        idle = (self.state == "editing" and self.editor is not None
                and self._generating is None)
        self._worksheet_menu.entryconfigure(
            "Generate Worksheet", state="normal" if idle else "disabled")
        can_chart = idle and self._latest_worksheet_path() is not None
        self._worksheet_menu.entryconfigure(
            "Generate Door Chart", state="normal" if can_chart else "disabled")

    def _build_toolbar(self):
        bar = ctk.CTkFrame(self.root, fg_color=("gray95", "gray15"),
                           corner_radius=0, height=46)
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_propagate(False)
        bar.columnconfigure(2, weight=1)

        # Small brand mark (full logo lives on the home view only)
        logo_path = resource_path("logos/ConvergeOne_logo.png")
        try:
            from PIL import Image as _PILImage
            pil_img = _PILImage.open(logo_path)
            w, h = pil_img.size
            mark_h = 22
            self._toolbar_logo = ctk.CTkImage(light_image=pil_img,
                                              size=(int(mark_h * w / h), mark_h))
            ctk.CTkLabel(bar, image=self._toolbar_logo, text="").grid(
                row=0, column=0, padx=(14, 10), pady=12)
        except Exception:
            ctk.CTkLabel(bar, text="C1", font=ctk.CTkFont(size=14, weight="bold"),
                         text_color=ACCENT).grid(row=0, column=0, padx=(14, 10))

        ctk.CTkFrame(bar, width=1, height=22,
                     fg_color=("gray80", "gray28")).grid(row=0, column=1)

        title_cell = ctk.CTkFrame(bar, fg_color="transparent")
        title_cell.grid(row=0, column=2, sticky="w", padx=(12, 0))
        self._title_lbl = ctk.CTkLabel(
            title_cell, text="No project open",
            font=ctk.CTkFont(size=13, weight="bold"), text_color="gray45",
            anchor="w",
        )
        self._title_lbl.pack(side="left")
        self._dirty_lbl = ctk.CTkLabel(
            title_cell, text="", width=16,
            font=ctk.CTkFont(size=13, weight="bold"), text_color="#c05621",
        )
        self._dirty_lbl.pack(side="left")

        # File/worksheet actions now live in the native menu bar. The only
        # toolbar control is a quick "close project" affordance at the far
        # right; the weight-1 title column (col 2) pushes it there.
        self._close_btn = ctk.CTkButton(
            bar, text="✕  Close project", height=30, width=120,
            fg_color="transparent", border_width=1, border_color="gray60",
            text_color=("gray25", "gray85"), hover_color=("gray90", "gray25"),
            font=ctk.CTkFont(size=12),
            command=self._process_another,
        )
        self._close_btn.grid(row=0, column=3, padx=(0, 12), pady=8)
        self._set_toolbar_enabled(False)

    def _build_statusbar(self):
        bar = ctk.CTkFrame(self.root, fg_color=("gray97", "gray13"),
                           corner_radius=0, height=28)
        bar.grid(row=5, column=0, sticky="ew")
        bar.grid_propagate(False)
        bar.columnconfigure(2, weight=1)

        self._status_lbl = ctk.CTkLabel(bar, text="Ready",
                                        font=ctk.CTkFont(size=11),
                                        text_color="gray50", anchor="w")
        self._status_lbl.grid(row=0, column=0, sticky="w", padx=(14, 12))

        self._source_lbl = ctk.CTkLabel(bar, text="", font=ctk.CTkFont(size=11),
                                        text_color="gray60", anchor="w")
        self._source_lbl.grid(row=0, column=1, sticky="w")

        self._issues_lbl = ctk.CTkLabel(bar, text="", font=ctk.CTkFont(size=11),
                                        text_color="#c05621", anchor="e")
        self._issues_lbl.grid(row=0, column=3, sticky="e", padx=(0, 12))

        self._term_btn = ctk.CTkButton(
            bar, text="▷ terminal", width=84, height=20,
            fg_color="transparent", text_color=ACCENT,
            hover_color=("gray90", "gray25"), border_width=0,
            font=ctk.CTkFont(size=11),
            command=self._toggle_terminal,
        )
        self._term_btn.grid(row=0, column=4, sticky="e", padx=(0, 10))

    def _build_terminal(self):
        self.term_text = ctk.CTkTextbox(
            self.term_section,
            height=200,
            font=ctk.CTkFont(family="Courier", size=11),
            fg_color="#1e1e1e",
            text_color="#d4d4d4",
            state="disabled",
            wrap="word",
        )
        self.term_text.grid(row=0, column=0, sticky="ew", padx=10, pady=(6, 6))
        self._redirector = TextRedirector(self.term_text, self.root)

    def _toggle_terminal(self):
        self._term_visible = not self._term_visible
        if self._term_visible:
            self.term_section.grid(row=3, column=0, sticky="ew")
            self._term_btn.configure(text="▽ terminal")
        else:
            self.term_section.grid_remove()
            self._term_btn.configure(text="▷ terminal")

    # ------------------------------------------------------------------ #
    # Chrome state                                                          #
    # ------------------------------------------------------------------ #

    def _set_toolbar_enabled(self, editing: bool):
        # Actions live in the menu bar (enabled live via the menus' postcommands);
        # the toolbar's only control is the close button, shown while a project
        # is open and hidden on the home screen.
        if editing:
            self._close_btn.grid()
        else:
            self._close_btn.grid_remove()

    def _set_project_title(self, text: str | None, dirty: bool = False):
        if text:
            self._title_lbl.configure(text=text, text_color=("gray15", "gray90"))
        else:
            self._title_lbl.configure(text="No project open", text_color="gray45")
        self._dirty_lbl.configure(text="●" if dirty else "")

    def _on_editor_status(self, text: str, dirty: bool):
        """EditorFrame save-state callback → status bar + toolbar title."""
        self._status_lbl.configure(
            text=text, text_color="#c05621" if dirty else "gray50")
        school = ""
        if self.session:
            school = self.session.design.site_info.school_name or "Untitled project"
        self._set_project_title(school, dirty)
        # The first save creates session.path, which enables Revert.
        self._set_toolbar_enabled(self.state == "editing")

    def _revert_clicked(self):
        """Discard in-memory changes and reload the session from disk."""
        if self.state != "editing" or not self.session or not self.session.path:
            return
        school = self.session.design.site_info.school_name or "this project"
        when = (self.session.saved_at or "")[:16].replace("T", " ")
        if not messagebox.askyesno(
            "Revert to last save?",
            f"Discard all changes to {school} since the last save"
            + (f" ({when})" if when else "") + "?\n\nThis cannot be undone.",
        ):
            return
        path = self.session.path
        try:
            clear_recovery(path)
            fresh = load_session(path)
        except SessionLoadError as exc:
            messagebox.showerror("Couldn't revert", str(exc))
            return
        self._enter_editor(fresh)

    def _on_editor_validation(self, text: str, ok: bool):
        self._issues_lbl.configure(text=text,
                                   text_color="#2f855a" if ok else "#c05621")

    def _show_flow(self):
        """Show the centered flow column (and hide the editor surface)."""
        self.editor_section.grid_remove()
        self.flow.grid(row=0, column=0, sticky="n")

    # ------------------------------------------------------------------ #
    # Section 1 — Input                                                     #
    # ------------------------------------------------------------------ #

    def _clear_input_section(self):
        for w in self.input_section.winfo_children():
            w.destroy()
        self._show_flow()

    def _show_drop_zone(self):
        self._clear_input_section()
        self._show_flow()
        self._set_project_title(None)
        self._status_lbl.configure(text="Ready", text_color="gray50")
        self._source_lbl.configure(text="")
        self._issues_lbl.configure(text="")

        # Full brand logo lives here, on the welcome surface only.
        logo_path = resource_path("logos/ConvergeOne_logo.png")
        try:
            from PIL import Image as _PILImage
            pil_img = _PILImage.open(logo_path)
            self._home_logo = ctk.CTkImage(light_image=pil_img, size=(140, 62))
            ctk.CTkLabel(self.input_section, image=self._home_logo, text="").grid(
                row=0, column=0, pady=(4, 14))
        except Exception:
            pass

        dz = ctk.CTkFrame(
            self.input_section,
            border_width=2,
            border_color="gray70",
            corner_radius=10,
            fg_color=("gray97", "gray20"),
            height=140,
        )
        dz.grid(row=1, column=0, sticky="ew")
        dz.columnconfigure(0, weight=1)
        dz.grid_propagate(False)

        ctk.CTkLabel(dz, text="⬆", font=ctk.CTkFont(size=36)).grid(row=0, column=0, pady=(22, 2))
        ctk.CTkLabel(
            dz,
            text="Drop a PDF, DMP worksheet (.xlsx), or project (.dmps) here",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).grid(row=1, column=0)
        ctk.CTkLabel(
            dz,
            text="e.g. SCHOOL_INTRUSION_DESIGN.pdf  ·  SCHOOL_dmp_2026-05-29.xlsx  ·  click to browse",
            font=ctk.CTkFont(size=11),
            text_color="gray50",
        ).grid(row=2, column=0, pady=(2, 14))

        def on_enter(_): dz.configure(border_color=ACCENT)
        def on_leave(_): dz.configure(border_color="gray70")

        for widget in [dz] + list(dz.winfo_children()):
            widget.bind("<Button-1>", lambda _: self._choose_pdf())
            widget.bind("<Enter>", on_enter)
            widget.bind("<Leave>", on_leave)

        self._show_recent_projects()
        self._show_output_dir_row()

    def _show_output_dir_row(self):
        # Output-folder picker lives on the home screen now that the old
        # job-details form (its previous host) is gone.
        row = ctk.CTkFrame(self.input_section, fg_color="transparent")
        row.grid(row=3, column=0, sticky="ew", pady=(12, 0))
        row.columnconfigure(1, weight=1)
        ctk.CTkLabel(row, text="Save output to:", font=ctk.CTkFont(size=11),
                     text_color="gray50").grid(row=0, column=0, sticky="w")
        self._output_dir_label = ctk.CTkLabel(
            row, text=str(self.output_dir), font=ctk.CTkFont(size=11),
            text_color=ACCENT, anchor="w",
        )
        self._output_dir_label.grid(row=0, column=1, sticky="w", padx=(6, 0))
        ctk.CTkButton(
            row, text="Change…", width=80, height=24,
            fg_color="transparent", border_width=1, border_color="gray60",
            text_color=("gray30", "gray80"), hover_color=("gray90", "gray25"),
            command=self._choose_output_dir,
        ).grid(row=0, column=2, padx=(8, 0))

    def _show_recent_projects(self):
        recents = list_recent_sessions(limit=4)
        if not recents:
            return

        frame = ctk.CTkFrame(self.input_section, fg_color="transparent")
        frame.grid(row=2, column=0, sticky="ew", pady=(16, 0))
        frame.columnconfigure(0, weight=1)

        ctk.CTkLabel(
            frame, text="Recent projects",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))

        for i, summary in enumerate(recents):
            row = ctk.CTkFrame(frame, corner_radius=8, fg_color=("gray97", "gray20"))
            row.grid(row=i + 1, column=0, sticky="ew", pady=3)
            row.columnconfigure(0, weight=1)

            saved = (summary.saved_at or "")[:16].replace("T", " ")
            sub = f"Saved {saved}" + (f"  ·  from {summary.source_name}"
                                      if summary.source_name else "")
            ctk.CTkLabel(
                row, text=summary.school_name,
                font=ctk.CTkFont(size=12, weight="bold"), anchor="w",
            ).grid(row=0, column=0, sticky="w", padx=12, pady=(8, 0))
            ctk.CTkLabel(
                row, text=sub, font=ctk.CTkFont(size=10),
                text_color="gray50", anchor="w",
            ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 8))

            ctk.CTkButton(
                row, text="Open", width=64, height=28,
                fg_color=ACCENT, hover_color=ACCENT_HOVER,
                command=lambda p=summary.path: self._open_session_path(p),
            ).grid(row=0, column=1, rowspan=2, padx=(4, 6))
            ctk.CTkButton(
                row, text="✕", width=28, height=28,
                fg_color="transparent", text_color="gray50",
                hover_color=("gray90", "gray25"),
                command=lambda p=summary.path, n=summary.school_name:
                    self._delete_session(p, n),
            ).grid(row=0, column=2, rowspan=2, padx=(0, 8))

    def _delete_session(self, path: Path, name: str):
        if not messagebox.askyesno(
            "Delete project?",
            f"Delete the saved project for {name}?\n\n"
            "Generated worksheets and door charts are not affected.",
        ):
            return
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)
        clear_recovery(path)
        if self.state == "idle":
            self._show_drop_zone()

    def _show_file_card(self, filename: str, *, parsing: bool, school_name: str = "",
                        busy_text: str = " Parsing PDF…"):
        self._clear_input_section()

        card = ctk.CTkFrame(
            self.input_section,
            border_width=2,
            border_color=ACCENT,
            corner_radius=10,
            fg_color=("#f4f7fb", "gray18"),
        )
        card.grid(row=0, column=0, sticky="ew")
        card.columnconfigure(1, weight=1)

        ctk.CTkLabel(card, text="📄", font=ctk.CTkFont(size=28)).grid(
            row=0, column=0, rowspan=2, padx=(16, 8), pady=14
        )

        name_text = filename if len(filename) <= 52 else filename[:49] + "…"
        ctk.CTkLabel(
            card,
            text=name_text,
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        ).grid(row=0, column=1, sticky="w", pady=(14, 2))

        if parsing:
            det_frame = ctk.CTkFrame(card, fg_color="transparent")
            det_frame.grid(row=1, column=1, sticky="w", pady=(0, 6))
            self._det_spinner_lbl = ctk.CTkLabel(
                det_frame, text="⠋", text_color="gray50", font=ctk.CTkFont(size=12), width=18
            )
            self._det_spinner_lbl.pack(side="left")
            ctk.CTkLabel(
                det_frame, text=busy_text, text_color="gray50", font=ctk.CTkFont(size=12)
            ).pack(side="left")
            self._start_label_spinner(self._det_spinner_lbl)
        else:
            ctk.CTkLabel(
                card,
                text=f"Detected: {school_name}",
                text_color=ACCENT,
                font=ctk.CTkFont(size=12, weight="bold"),
                anchor="w",
            ).grid(row=1, column=1, sticky="w", pady=(0, 6))

        ctk.CTkButton(
            card,
            text="Replace",
            width=80,
            height=28,
            fg_color="transparent",
            border_width=1,
            border_color="gray60",
            text_color=("gray30", "gray80"),
            hover_color=("gray90", "gray25"),
            command=self._replace_pdf,
        ).grid(row=0, column=2, rowspan=2, padx=12, pady=14)

    def _show_parse_error(self, exc: Exception, title: str = "Couldn't parse PDF"):
        self._clear_input_section()
        self._make_error_card(self.input_section, title, exc, self._show_drop_zone)

    # ------------------------------------------------------------------ #
    # Section 2 — Metadata form                                            #
    # ------------------------------------------------------------------ #

    def _choose_output_dir(self):
        chosen = filedialog.askdirectory(
            title="Choose output folder", initialdir=str(self.output_dir)
        )
        if not chosen:
            return
        self.output_dir = Path(chosen)
        save_prefs({**load_prefs(), "output_dir": str(self.output_dir)})
        with contextlib.suppress(Exception):
            self._output_dir_label.configure(text=str(self.output_dir))

    # ------------------------------------------------------------------ #
    # Error card                                                            #
    # ------------------------------------------------------------------ #

    def _make_error_card(self, parent, title: str, exc: Exception, retry):
        card = ctk.CTkFrame(
            parent, fg_color=("#fff5f5", "gray20"),
            border_width=1, border_color="#ffcdd2", corner_radius=8,
        )
        card.grid(row=0, column=0, sticky="ew", pady=4)
        card.columnconfigure(1, weight=1)

        ctk.CTkLabel(card, text="⚠", text_color="#e53e3e",
                     font=ctk.CTkFont(size=20)).grid(row=0, column=0, padx=(14, 8), pady=(14, 4), sticky="n")

        info = ctk.CTkFrame(card, fg_color="transparent")
        info.grid(row=0, column=1, sticky="ew", pady=12, padx=(0, 14))
        ctk.CTkLabel(info, text=title, font=ctk.CTkFont(size=13, weight="bold"),
                     text_color="#e53e3e", anchor="w").pack(anchor="w")
        ctk.CTkLabel(info, text=str(exc)[:200], font=ctk.CTkFont(size=11),
                     text_color="gray50", anchor="w", wraplength=380).pack(anchor="w", pady=(2, 8))
        ctk.CTkButton(info, text="Try again", width=90, height=30,
                      fg_color=ACCENT, hover_color=ACCENT_HOVER, command=retry).pack(anchor="w")

    # ------------------------------------------------------------------ #
    # Toast                                                                 #
    # ------------------------------------------------------------------ #

    def _open_with_toast(self, path: Path | None):
        if not path:
            return
        open_file(path)
        self._show_toast(f"Opening {path.name} in Excel…")

    def _show_toast(self, message: str, action: tuple | None = None):
        """Slide-in notification. `action` is an optional ("Label", callback)
        button — used by generation completions ("rev 3 ready — Open"); a
        toast with an action lingers longer so it can actually be clicked."""
        self.root.update_idletasks()
        rx, ry = self.root.winfo_x(), self.root.winfo_y()
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        tw, th = (440, 48) if action else (360, 48)
        tx = rx + (rw - tw) // 2
        ty_end = ry + rh - 70
        ty_start = ty_end + 20

        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)
        toast.configure(bg="#1c1c1e")
        toast.geometry(f"{tw}x{th}+{tx}+{ty_start}")
        toast.attributes("-alpha", 0.92)
        tk.Label(toast, text=message, bg="#1c1c1e", fg="white",
                 font=("Helvetica", 12), padx=16, pady=12).pack(
            side="left", expand=True, fill="x")
        if action:
            label, callback = action
            tk.Button(
                toast, text=label, relief="flat", borderwidth=0,
                bg="#1c1c1e", fg="#8ab8f0", activebackground="#1c1c1e",
                activeforeground="white", highlightthickness=0,
                font=("Helvetica", 12, "bold"), cursor="pointinghand"
                if sys.platform == "darwin" else "hand2",
                command=lambda: (callback(),
                                 toast.destroy() if toast.winfo_exists() else None),
            ).pack(side="right", padx=(0, 14))

        steps = 8
        for i in range(steps + 1):
            y = int(ty_start + (ty_end - ty_start) * i / steps)
            self.root.after(int(i * 180 / steps),
                            lambda _y=y: toast.geometry(f"{tw}x{th}+{tx}+{_y}") if toast.winfo_exists() else None)
        linger = 6000 if action else 1800
        self.root.after(linger, lambda: toast.destroy() if toast.winfo_exists() else None)

    # ------------------------------------------------------------------ #
    # Spinner helpers                                                       #
    # ------------------------------------------------------------------ #

    def _start_label_spinner(self, lbl: ctk.CTkLabel):
        idx = [0]
        def _tick():
            if not lbl.winfo_exists():
                return
            idx[0] += 1
            lbl.configure(text=SPINNER_FRAMES[idx[0] % len(SPINNER_FRAMES)])
            job = self.root.after(80, _tick)
            self._spinner_jobs.append(job)
        _tick()

    def _stop_spinners(self):
        for job in self._spinner_jobs:
            try:
                self.root.after_cancel(job)
            except Exception:
                pass
        self._spinner_jobs.clear()

    # ------------------------------------------------------------------ #
    # Flow: parse                                                           #
    # ------------------------------------------------------------------ #

    def _choose_pdf(self):
        path = filedialog.askopenfilename(
            title="Choose design PDF, DMP worksheet, or saved project",
            filetypes=[
                ("PDF, worksheet, or project", "*.pdf *.xlsx *.dmps"),
                ("PDF files", "*.pdf"),
                ("DMP worksheet", "*.xlsx"),
                ("Saved project", "*.dmps"),
                ("All files", "*.*"),
            ],
        )
        if path:
            self._start_input(Path(path))

    def _on_drop(self, event):
        # tkdnd hands back a Tcl list: one brace-wrapped path per file. Split it
        # properly so paths containing spaces survive, and reject multi-file drops
        # (the app processes one file at a time) with a clear message rather than a
        # mangled path.
        try:
            paths = list(self.root.tk.splitlist(event.data))
        except Exception:
            path_str = event.data
            if path_str.startswith("{") and path_str.endswith("}"):
                path_str = path_str[1:-1]
            paths = [path_str]
        if len(paths) > 1:
            self._show_parse_error(
                ValueError("Please drop one file at a time."),
                title="Too many files",
            )
            return
        if paths:
            self._start_input(Path(paths[0]))

    def _start_input(self, path: Path):
        """Route by file type: .dmps resumes a saved project, .xlsx imports a
        DMP worksheet, anything else takes the PDF parse pipeline."""
        if self.editor and not self.editor.maybe_close():
            return
        # One project at a time: opening a new input closes the current one.
        self._teardown_editor()
        self.session = None
        suffix = path.suffix.lower()
        if suffix == SESSION_EXT:
            self._open_session_path(path)
        elif suffix == ".xlsx":
            self._start_load_worksheet(path)
        else:
            self._start_parse(path)

    def _start_parse(self, pdf_path: Path):
        self.pdf_path = pdf_path
        self.parsed_design = None
        self.state = "parsing"
        self._stop_spinners()
        self._show_file_card(pdf_path.name, parsing=True)

        def work():
            with contextlib.redirect_stdout(self._redirector), \
                 contextlib.redirect_stderr(self._redirector):
                searchable = ensure_searchable_pdf(pdf_path)
                original = resolve_original_pdf(pdf_path)
                return build_dmp_design_from_pdf(searchable, original, non_interactive=True)

        def on_done(design):
            if self.state != "parsing":
                return
            self._stop_spinners()
            self._show_file_card(pdf_path.name, parsing=False,
                                 school_name=design.site_info.school_name or "Unknown")
            self._enter_editor(Session(design=design, source_kind="pdf",
                                       source_name=pdf_path.name))

        def on_error(exc):
            if self.state != "parsing":
                return
            self.state = "idle"
            self._stop_spinners()
            self._show_parse_error(exc)

        self._run_async(work, on_done, on_error)

    def _start_load_worksheet(self, xlsx_path: Path):
        """Load an already-generated DMP worksheet (.xlsx) into the editor,
        skipping PDF parsing.

        self.dmp_path points at the imported file, so 'Generate Door Chart' is
        available immediately and builds from it as-is; generating a worksheet
        revision re-points dmp_path at the new file.
        """
        self.dmp_path = xlsx_path
        self.pdf_path = None
        self.parsed_design = None
        self.state = "loading_xlsx"
        self._stop_spinners()
        self._show_file_card(xlsx_path.name, parsing=True, busy_text=" Reading worksheet…")

        def work():
            with contextlib.redirect_stdout(self._redirector), \
                 contextlib.redirect_stderr(self._redirector):
                return parse_dmp_worksheet(xlsx_path)

        def on_done(design):
            if self.state != "loading_xlsx":
                return
            self._stop_spinners()
            if not worksheet_looks_like_dmp(design):
                self.state = "idle"
                self.dmp_path = None
                self._show_parse_error(
                    ValueError(
                        "This file has no SITE INFO sheet or Master zone list — "
                        "it doesn't look like a DMP worksheet."
                    ),
                    title="Not a DMP worksheet",
                )
                return
            # Importing a worksheet starts a NEW project from that file. Give it a
            # unique session path so it never overwrites an existing saved project for
            # the same school (default_session_path keys only on the school name).
            # Drafts are accepted: a worksheet hand-finalized in Excel by a third party
            # keeps its hidden DMPStatus=DRAFT flag, and the operator still needs it.
            school = design.site_info.school_name or "Unknown"
            self._show_file_card(xlsx_path.name, parsing=False, school_name=school)
            self._enter_editor(Session(design=design, source_kind="xlsx",
                                       source_name=xlsx_path.name,
                                       path=unique_session_path(design)))

        def on_error(exc):
            if self.state != "loading_xlsx":
                return
            self.state = "idle"
            self.dmp_path = None
            self._stop_spinners()
            self._show_parse_error(exc, title="Couldn't read DMP worksheet")

        self._run_async(work, on_done, on_error)

    def _replace_pdf(self):
        if self.editor and not self.editor.maybe_close():
            return
        self.state = "idle"
        self._stop_spinners()
        self.pdf_path = None
        self.dmp_path = None
        self.parsed_design = None
        self._teardown_editor()
        self.action_section.grid_remove()
        self._show_drop_zone()

    # ------------------------------------------------------------------ #
    # Project editor                                                         #
    # ------------------------------------------------------------------ #

    def _enter_editor(self, session: Session):
        """Open the unified editor over a session (new or loaded)."""
        # Make zones editable even when only Master rows were parsed (xlsx with
        # unevaluated Point Info formulas), and canonicalize legacy 'RSP N'
        # splitter tokens so validation only flags hand-typed regressions.
        ensure_editable_zones(session.design)
        normalize_rsp_tokens(session.design)
        normalize_zone_descriptions(session.design)
        self.session = session
        self.parsed_design = session.design  # generation flows read this
        self.state = "editing"
        self._ws_epoch = None  # no worksheet generated for this project yet

        self._teardown_editor()
        self.editor = EditorFrame(
            self.editor_section, self.root, session,
            on_generate_worksheet=self._generate_worksheet,
            on_generate_chart=self._generate_door_chart,
            on_generate_remotelink=self._generate_remotelink,
            on_status_change=self._on_editor_status,
            on_validation_change=self._on_editor_validation,
        )
        self.editor.grid(row=0, column=0, sticky="nsew")
        if session.source_kind == "xlsx" and self.dmp_path is not None:
            # The imported worksheet IS the design as of right now — treat it
            # as in-sync so the door-chart staleness warning only fires for
            # edits made after import.
            self._ws_epoch = self.editor.edit_epoch
        if session.saved_at is None:
            # Fresh parse: seed the per-machine defaults the old form offered.
            self.editor.prefill_site_defaults(load_prefs())
        self._show_editor_surface()
        self._set_toolbar_enabled(True)
        if session.source_name:
            self._source_lbl.configure(text=f"from {session.source_name}")

    def _show_editor_surface(self):
        """Full-bleed editor; flow column hidden."""
        self.flow.grid_remove()
        self.action_section.grid_remove()
        self.editor_section.grid(row=0, column=0, sticky="nsew",
                                 padx=14, pady=(10, 8))

    def _teardown_editor(self):
        if self.editor is not None:
            self.editor.destroy()
            self.editor = None
        self.editor_section.grid_remove()
        self._set_toolbar_enabled(False)
        self._set_project_title(None)
        self._source_lbl.configure(text="")
        self._issues_lbl.configure(text="")

    def _open_session_path(self, path: Path):
        """Open a saved .dmps project, offering crash recovery when present."""
        if self.editor and not self.editor.maybe_close():
            return
        try:
            rec_time = pending_recovery(path)
            if rec_time and messagebox.askyesno(
                "Recover unsaved changes?",
                f"Unsaved changes from {rec_time.strftime('%b %-d, %-I:%M %p')} "
                "were found for this project (the app may have closed "
                "unexpectedly).\n\nRecover them?",
            ):
                session = load_recovery(path)
            else:
                clear_recovery(path)
                session = load_session(path)
        except SessionLoadError as exc:
            self._show_parse_error(exc, title="Couldn't open project")
            return
        self._stop_spinners()
        self.pdf_path = None
        self.dmp_path = None
        self._show_file_card(path.name, parsing=False,
                             school_name=session.design.site_info.school_name or "")
        self._enter_editor(session)

    def _save_shortcut(self, _event=None):
        if self.state == "editing" and self.editor:
            self.editor.save()

    def _on_ui_exception(self, exc_type, exc, tb):
        import traceback as _tb
        detail = "".join(_tb.format_exception(exc_type, exc, tb))
        with contextlib.suppress(Exception):
            self._redirector.write(f"\n=== UI error ===\n{detail}")
        with contextlib.suppress(Exception):
            with open(output_dir() / "debug.log", "a", encoding="utf-8") as fh:
                fh.write(f"\n=== UI error ===\n{detail}")
        messagebox.showerror(
            "Unexpected error",
            f"Something went wrong in the interface:\n\n{exc}\n\n"
            "Your project data is unaffected — save it, then check the "
            "terminal panel for details.",
        )

    # ------------------------------------------------------------------ #
    # Generation — in-editor, repeatable, revision-numbered                 #
    # ------------------------------------------------------------------ #
    #
    # Generation is a background action, not a mode: the editor stays up,
    # both buttons disable while a run is in flight, and each run writes the
    # next {slug}_dmp_revN.xlsx / {slug}_door_chart_revN.xlsx so the tech can
    # print, review with the superintendent, edit, and regenerate at will.

    def _school_slug(self) -> str:
        return _slugify(self.session.design.site_info.school_name or "output")

    def _latest_worksheet_path(self) -> Path | None:
        """The worksheet a door chart would be built from: the last one this
        session touched (a generated rev, or the imported source xlsx), else
        the highest rev on disk from a previous session."""
        if self.dmp_path is not None and Path(self.dmp_path).exists():
            return Path(self.dmp_path)
        if self.session is not None:
            return latest_rev_path(self.output_dir, f"{self._school_slug()}_dmp")
        return None

    def _set_generating(self, which: str | None):
        self._generating = which
        if self.editor is not None:
            self.editor.set_generating(which)

    def _generate_worksheet(self):
        """Generate the next worksheet revision without leaving the editor.

        Validation warns but never blocks — the printed sheet is itself a
        review pass with the superintendent."""
        if self.state != "editing" or not self.session or not self.editor \
                or self._generating is not None:
            return
        if self.editor.dirty and messagebox.askyesno(
            "Save project?", "Save the project before generating?",
        ):
            self.editor.save()

        def proceed():
            design = self.session.design
            sync_master_zones(design)
            # Persist the per-machine site defaults (tech, IP, ...). Phone and
            # install date are deliberately excluded: phone is school-specific
            # (auto-looked-up per site), and a remembered install date is always
            # stale on the next project — it defaults to today at import instead.
            prefs = load_prefs()
            prefs.pop("install_date", None)  # drop any value saved by older builds
            save_prefs({**prefs,
                        "install_tech": design.site_info.install_tech or "",
                        "ip_address": design.site_info.ip_address or "",
                        "default_gateway": design.site_info.default_gateway or ""})

            out_dir = self.output_dir
            pdf_path = self.pdf_path
            slug = self._school_slug()
            self._set_generating("worksheet")

            def work():
                # PDF prep only applies when the source was a PDF; imported
                # worksheets and saved sessions write straight from the design.
                if pdf_path is not None:
                    ensure_searchable_pdf(pdf_path)
                out_dir.mkdir(parents=True, exist_ok=True)
                out_path = next_rev_path(out_dir, f"{slug}_dmp")
                with contextlib.redirect_stdout(self._redirector), \
                     contextlib.redirect_stderr(self._redirector):
                    write_dmp_xlsx(design, DEFAULT_TEMPLATE, out_path)
                return out_path

            def on_done(out_path):
                self._set_generating(None)
                self.dmp_path = out_path
                if self.editor is not None:
                    self._ws_epoch = self.editor.edit_epoch
                rev = out_path.stem.rsplit("_rev", 1)[-1]
                self._show_toast(f"Worksheet rev {rev} ready",
                                 action=("Open", lambda: open_file(out_path)))

            def on_error(exc):
                self._set_generating(None)
                messagebox.showerror("Worksheet generation failed", str(exc))

            self._run_async(work, on_done, on_error)

        self.editor.show_issues_dialog(proceed, proceed_label="Generate anyway")

    def _generate_door_chart(self):
        """Generate the next door-chart revision from the newest worksheet."""
        if self.state != "editing" or not self.session or not self.editor \
                or self._generating is not None:
            return
        src = self._latest_worksheet_path()
        if src is None:
            messagebox.showinfo(
                "No worksheet yet",
                "Generate a worksheet first — the door chart is built from it.")
            return
        # The chart reads the worksheet FILE, so editor changes newer than
        # that file won't be in it. Warn (never block) when that's the case.
        stale = (self._ws_epoch != self.editor.edit_epoch
                 if self._ws_epoch is not None else self.editor.edit_epoch > 0)
        if stale and not messagebox.askyesno(
            "Worksheet may be stale",
            f"The design has changed since {src.name} was generated.\n\n"
            "The door chart is built from that file, so it won't include the "
            "newer edits. Generate the door chart anyway?\n\n"
            "(Tip: regenerate the worksheet first to pick up your edits.)",
        ):
            return

        out_dir = self.output_dir
        self._set_generating("chart")

        def work():
            with contextlib.redirect_stdout(self._redirector), \
                 contextlib.redirect_stderr(self._redirector):
                dmp_design = parse_dmp_worksheet(src)
            school_slug = _slugify(dmp_design.site_info.school_name or "output")
            out_dir.mkdir(parents=True, exist_ok=True)
            chart_output = next_rev_path(out_dir, f"{school_slug}_door_chart")
            with contextlib.redirect_stdout(self._redirector), \
                 contextlib.redirect_stderr(self._redirector):
                inject(DOOR_CHART_TEMPLATE, dmp_design, chart_output)
            return chart_output

        def on_done(chart_path):
            self._set_generating(None)
            self.door_chart_path = chart_path
            rev = chart_path.stem.rsplit("_rev", 1)[-1]
            self._show_toast(f"Door chart rev {rev} ready",
                             action=("Open", lambda: open_file(chart_path)))

        def on_error(exc):
            self._set_generating(None)
            messagebox.showerror("Door chart generation failed", str(exc))

        self._run_async(work, on_done, on_error)

    # ------------------------------------------------------------------ #
    # RemoteLink account                                                    #
    # ------------------------------------------------------------------ #

    def _generate_remotelink(self):
        """Generate an encrypted RemoteLink `.xml` account from the design.

        Portable (any OS) — the operator imports the `.xml` into RemoteLink."""
        if self.state != "editing" or not self.session or not self.editor \
                or self._generating is not None:
            return
        if self.editor.dirty and messagebox.askyesno(
            "Save project?", "Save the project before generating?",
        ):
            self.editor.save()
        self._show_remotelink_dialog()

    def _show_remotelink_dialog(self):
        """Prompt for the account number, receiver number, and export passphrase.

        Account prefills from the school code (LOC CODE). The account is stamped
        from a bundled demo template (no real data), so there's nothing to pick —
        just choose a passphrase, which you'll re-type when importing."""
        design = self.session.design
        default_account = (design.site_info.school_code or "").strip()

        dlg = ctk.CTkToplevel(self.root)
        dlg.title("Generate RemoteLink Account")
        dlg.geometry("520x240")
        dlg.transient(self.root)
        dlg.grab_set()

        ctk.CTkLabel(dlg, text="Generate RemoteLink Account",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(
            anchor="w", padx=20, pady=(18, 2))
        ctk.CTkLabel(
            dlg, text="Builds an encrypted .xml you import into RemoteLink.",
            text_color=("gray40", "gray70")).pack(anchor="w", padx=20, pady=(0, 10))

        form = ctk.CTkFrame(dlg, fg_color="transparent")
        form.pack(fill="x", padx=20)
        form.columnconfigure(1, weight=1)

        ctk.CTkLabel(form, text="Account number").grid(
            row=0, column=0, sticky="w", pady=6)
        acct_var = ctk.StringVar(value=default_account)
        ctk.CTkEntry(form, textvariable=acct_var).grid(
            row=0, column=1, sticky="ew", pady=6)

        ctk.CTkLabel(form, text="Passphrase").grid(row=1, column=0, sticky="w", pady=6)
        pass_var = ctk.StringVar(value="")
        ctk.CTkEntry(form, textvariable=pass_var, show="•").grid(
            row=1, column=1, sticky="ew", pady=6)

        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=16)
        ctk.CTkButton(btns, text="Cancel", width=90, fg_color="transparent",
                      border_width=1, text_color=("gray30", "gray80"),
                      command=dlg.destroy).pack(side="left")

        def submit():
            account_num = acct_var.get().strip()
            passphrase = pass_var.get()
            if not account_num.isdigit():
                messagebox.showerror(
                    "Invalid account number",
                    "The account number must be numeric (the school LOC CODE, "
                    "e.g. 2250) — it becomes the panel user code.", parent=dlg)
                return
            if not passphrase:
                messagebox.showerror(
                    "Passphrase needed",
                    "Enter the export passphrase — you'll type the same one when "
                    "importing into RemoteLink.", parent=dlg)
                return
            dlg.destroy()
            self._run_generate_remotelink(account_num, passphrase)

        ctk.CTkButton(btns, text="Generate", width=120, fg_color=ACCENT,
                      hover_color=ACCENT_HOVER, command=submit).pack(side="right")

    def _run_generate_remotelink(self, account_num, passphrase):
        design = self.session.design
        sync_master_zones(design)
        out_dir = self.output_dir
        template_path = resource_path("remotelink_account_template.xml")
        self._set_generating("remotelink")

        def work():
            out_dir.mkdir(parents=True, exist_ok=True)
            with contextlib.redirect_stdout(self._redirector), \
                 contextlib.redirect_stderr(self._redirector):
                return generate_account_xml(
                    design, account_num,
                    template_path=template_path, passphrase=passphrase,
                    out_dir=out_dir)

        def on_done(path):
            self._set_generating(None)
            # A generated account still needs its per-site comm / IP / panel
            # settings entered in Remote Link. Generating ≠ commissioning.
            self._show_toast(f"RemoteLink .xml for {account_num} ready — "
                             "import it into RemoteLink",
                             action=("Open", lambda: open_file(path)))

        def on_error(exc):
            self._set_generating(None)
            messagebox.showerror("RemoteLink account generation failed", str(exc))

        self._run_async(work, on_done, on_error)

    # ------------------------------------------------------------------ #
    # Reset                                                                 #
    # ------------------------------------------------------------------ #

    def _process_another(self):
        if self._generating is not None:
            messagebox.showinfo("Generation in progress",
                                "Wait for the current generation to finish "
                                "before closing the project.")
            return
        if self.editor and not self.editor.maybe_close():
            return
        self.pdf_path = None
        self.dmp_path = None
        self.door_chart_path = None
        self.parsed_design = None
        self.session = None
        self.state = "idle"
        self._ws_epoch = None
        self._stop_spinners()
        self._teardown_editor()
        self.action_section.grid_remove()
        self._show_drop_zone()

    # ------------------------------------------------------------------ #
    # Async runner                                                          #
    # ------------------------------------------------------------------ #

    def _run_async(self, work_fn, on_done, on_error):
        def runner():
            try:
                result = work_fn()
                self.root.after(0, on_done, result)
            except Exception as exc:
                import traceback as _tb
                try:
                    with open(output_dir() / "debug.log", "a", encoding="utf-8") as fh:
                        fh.write("\n=== exception in _run_async ===\n")
                        _tb.print_exc(file=fh)
                except Exception:
                    pass
                self.root.after(0, on_error, exc)
        threading.Thread(target=runner, daemon=True).start()

    # ------------------------------------------------------------------ #
    # Close guard                                                           #
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Auto-update                                                          #
    # ------------------------------------------------------------------ #

    def _check_for_updates(self, silent: bool = True):
        """Query GitHub for the latest release on a background thread.

        silent=True (launch check) throttles to once/day and stays quiet unless a
        newer, non-skipped release exists. silent=False (Help menu) always reports.
        """
        if silent and load_prefs().get("last_update_check") == date.today().isoformat():
            return

        def work():
            info = updater.fetch_latest()
            self.root.after(0, lambda: self._on_update_info(info, silent))

        threading.Thread(target=work, daemon=True).start()

    def _on_update_info(self, info, silent: bool):
        if info is None:
            if not silent:
                messagebox.showinfo(
                    "Check for Updates",
                    "Couldn't reach the update server. Please try again later.")
            return

        prefs = load_prefs()
        prefs["last_update_check"] = date.today().isoformat()
        save_prefs(prefs)

        if not updater.is_newer(info["version"], updater.current_version()):
            if not silent:
                messagebox.showinfo(
                    "Check for Updates",
                    f"You're up to date (v{updater.current_version_str()}).")
            return

        if silent and prefs.get("skip_version") == info["tag"]:
            return

        self._show_update_dialog(info)

    def _show_update_dialog(self, info):
        if self._update_dialog is not None and self._update_dialog.winfo_exists():
            self._update_dialog.lift()
            return

        dlg = ctk.CTkToplevel(self.root)
        self._update_dialog = dlg
        dlg.title("Update Available")
        dlg.geometry("460x420")
        dlg.transient(self.root)
        dlg.protocol("WM_DELETE_WINDOW", lambda: self._close_update_dialog())

        cur = updater.current_version_str()
        ctk.CTkLabel(
            dlg, text=f"Version {info['tag'].lstrip('v')} is available",
            font=ctk.CTkFont(size=16, weight="bold")).pack(padx=20, pady=(20, 2))
        ctk.CTkLabel(dlg, text=f"You have v{cur}.",
                     text_color=("gray40", "gray70")).pack(padx=20, pady=(0, 10))

        notes = ctk.CTkTextbox(dlg, height=200, wrap="word")
        notes.pack(fill="both", expand=True, padx=20)
        notes.insert("1.0", info["notes"] or "See the release page for details.")
        notes.configure(state="disabled")

        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=16)

        def later():
            prefs = load_prefs()
            prefs["skip_version"] = info["tag"]
            save_prefs(prefs)
            self._close_update_dialog()

        ctk.CTkButton(btns, text="Later", width=90, fg_color="transparent",
                      border_width=1, text_color=("gray30", "gray80"),
                      command=later).pack(side="left")

        if updater.can_self_update() and info["asset_url"]:
            ctk.CTkButton(btns, text="Update Now", width=130,
                          fg_color=ACCENT, hover_color=ACCENT_HOVER,
                          command=lambda: self._start_update(info, dlg, btns)
                          ).pack(side="right")
        else:
            # Dev run or missing asset — fall back to the download page.
            ctk.CTkButton(
                btns, text="Open Download Page", width=170,
                fg_color=ACCENT, hover_color=ACCENT_HOVER,
                command=lambda: (webbrowser.open(info["html_url"]),
                                 self._close_update_dialog())
            ).pack(side="right")

    def _close_update_dialog(self):
        if self._update_dialog is not None and self._update_dialog.winfo_exists():
            self._update_dialog.destroy()
        self._update_dialog = None

    # ------------------------------------------------------------------ #
    # In-app help                                                          #
    # ------------------------------------------------------------------ #

    def _show_help_text(self, title: str, body: str):
        """Read-only help window — same shell as the update dialog."""
        dlg = ctk.CTkToplevel(self.root)
        dlg.title(title)
        dlg.geometry("560x520")
        dlg.transient(self.root)
        ctk.CTkLabel(dlg, text=title,
                     font=ctk.CTkFont(size=16, weight="bold")).pack(
            anchor="w", padx=20, pady=(18, 8))
        box = ctk.CTkTextbox(dlg, wrap="word")
        box.pack(fill="both", expand=True, padx=20, pady=(0, 8))
        box.insert("1.0", body)
        box.configure(state="disabled")
        ctk.CTkButton(dlg, text="Close", width=90, fg_color=ACCENT,
                      hover_color=ACCENT_HOVER,
                      command=dlg.destroy).pack(pady=(0, 16))

    def _show_workflow_help(self):
        self._show_help_text("Field-Edit Workflow", _WORKFLOW_HELP)

    def _show_shortcuts_help(self):
        mod = "Cmd" if sys.platform == "darwin" else "Ctrl"
        rows = [
            (f"{mod}+N", "New / close project"),
            (f"{mod}+O", "Open a PDF or worksheet"),
            (f"{mod}+S", "Save the project (.dmps)"),
            (f"{mod}+E", "Generate the DMP worksheet (next revision)"),
            (f"{mod}+D", "Generate the door chart (next revision)"),
            ("Double-click / Return / F2", "Edit the selected zone cell"),
            ("Escape", "Cancel a zone edit"),
        ]
        body = "\n".join(f"{k:<28}{v}" for k, v in rows)
        self._show_help_text("Keyboard Shortcuts", body)

    def _open_readme(self):
        """Open the bundled README if present, else the online copy."""
        local = resource_path("README.md")
        if local.exists():
            webbrowser.open(local.as_uri())
        else:
            webbrowser.open(f"https://github.com/{updater.REPO}/blob/main/README.md")

    def _start_update(self, info, dlg, btns):
        """Download the new build with a progress bar, then swap + relaunch."""
        for child in btns.winfo_children():
            child.destroy()
        status = ctk.CTkLabel(btns, text="Downloading…")
        status.pack(side="left")
        bar = ctk.CTkProgressBar(btns, width=200)
        bar.set(0)
        bar.pack(side="right")

        def progress(frac):
            self.root.after(0, lambda: bar.set(frac) if bar.winfo_exists() else None)

        def work():
            try:
                zip_path = Path(tempfile.mkdtemp(prefix="dmp_dl_")) / "update.zip"
                updater.download(info["asset_url"], zip_path, progress)
                ok = updater.apply_update(zip_path)
            except Exception as exc:
                self.root.after(0, lambda: self._update_failed(str(exc)))
                return
            if ok:
                self.root.after(0, self._quit_for_update)
            else:
                self.root.after(0, lambda: self._update_failed(
                    "Self-update isn't available for this build."))

        threading.Thread(target=work, daemon=True).start()

    def _update_failed(self, msg: str):
        self._close_update_dialog()
        messagebox.showerror("Update failed", f"{msg}\n\nYou can download the latest "
                             "build from the GitHub releases page.")

    def _shutdown(self):
        """Exit the process immediately, skipping interpreter/library teardown.

        A normal Tk teardown runs C++ static destructors (notably PyMuPDF's
        global MuPDF context) while Python is finalizing; that destructor
        flushes buffered MuPDF warnings through a Python callback into a
        half-dead interpreter and segfaults on quit. os._exit sidesteps the
        whole chain. Safe here because the app persists all state during use
        (prefs on generate; .dmps via the editor save-prompt) — nothing
        important runs in atexit/finalizers.
        """
        try:
            self.root.withdraw()  # instant visual close before the hard exit
        except Exception:
            pass
        os._exit(0)

    def _quit_for_update(self):
        """Quit so the detached helper can replace files and relaunch."""
        if self.state == "editing" and self.editor and not self.editor.maybe_close():
            return  # user cancelled the save prompt — leave the app open
        self._shutdown()

    def _on_close(self):
        if self._generating is not None:
            if messagebox.askyesno("Quit?", "Generation in progress — quit anyway?"):
                self._shutdown()
            return
        if self.state == "editing" and self.editor and not self.editor.maybe_close():
            return
        self._shutdown()

    def run(self):
        self.root.mainloop()


def main():
    app = App()
    app.run()


if __name__ == "__main__":
    main()
