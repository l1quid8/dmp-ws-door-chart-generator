import sys
import os
import json
import threading
import subprocess
import contextlib
import tempfile
import webbrowser
from datetime import date, datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk
from tkinterdnd2 import TkinterDnD, DND_FILES

sys.path.insert(0, str(Path(__file__).parent))

from paths import APP_NAME, resource_path, output_dir, next_rev_path, latest_rev_path
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
from editor_tabs import auto_hide_scrollbar
from rl_injector.xml_export import generate_account_xml
import theme
from ui_widgets import (
    Card,
    Chip,
    IconTile,
    ModeToggle,
    SectionLabel,
    bind_click,
    ghost_button,
    primary_button,
    remove_button,
    secondary_button,
)
import updater

DOOR_CHART_TEMPLATE = resource_path("door_chart_template_blank.xlsx")
PREFS_PATH = Path.home() / ".c1_door_chart_app.json"
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


def reveal_in_folder(path: Path) -> None:
    """Show `path` selected in the OS file browser.

    Linux has no cross-desktop "reveal", so it settles for opening the parent.
    """
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", str(path)])
    elif sys.platform == "win32":
        subprocess.Popen(["explorer", f"/select,{path}"])
    else:
        subprocess.Popen(["xdg-open", str(Path(path).parent)])


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
        # Resolve the appearance mode before the first widget exists —
        # CustomTkinter samples the active mode when a widget is constructed,
        # so a later switch would leave the root window in the wrong palette.
        theme.set_mode(load_prefs().get("appearance_mode", "light"))
        self.root = CTkDnD()
        _version = _app_version()
        self.root.title(APP_NAME + (f"  v{_version}" if _version else ""))
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
            # Enter/leave only light up the drop zone. They must hand tkdnd the
            # incoming action back verbatim, or the drop itself is refused.
            self.root.dnd_bind("<<DropEnter>>", self._on_drag_over)
            self.root.dnd_bind("<<DropLeave>>", self._on_drag_out)
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

        # The header's 1px bottom border.
        ctk.CTkFrame(self.root, height=1, corner_radius=0,
                     fg_color=theme.BORDER).grid(row=1, column=0, sticky="ew")

        # Main area
        self.main = ctk.CTkFrame(self.root, fg_color="transparent")
        self.main.grid(row=2, column=0, sticky="nsew")
        self.main.columnconfigure(0, weight=1)
        self.main.rowconfigure(0, weight=1)

        # Centered flow column (home / progress / review screens), inside a
        # scroll host. The home stack — drop zone, recents, output-folder row —
        # is now taller than the minimum window, and the output-folder picker
        # is the only way to change where files land, so it must never be
        # stranded below the fold. auto_hide_scrollbar keeps the trough out of
        # sight until it's actually needed.
        self._flow_host = ctk.CTkScrollableFrame(self.main, fg_color="transparent")
        self._flow_host.grid(row=0, column=0, sticky="nsew")
        self._flow_host.columnconfigure(0, weight=1)
        auto_hide_scrollbar(self._flow_host)

        self.flow = ctk.CTkFrame(self._flow_host, fg_color="transparent")
        self.flow.grid(row=0, column=0)
        self.flow.columnconfigure(0, weight=1, minsize=600)

        self.input_section = ctk.CTkFrame(self.flow, fg_color="transparent")
        self.input_section.grid(row=0, column=0, sticky="ew", pady=(36, 0))
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
                     fg_color=theme.BORDER).grid(row=4, column=0, sticky="ew")
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
        bar = ctk.CTkFrame(self.root, fg_color=theme.CHROME,
                           corner_radius=0, height=theme.HEIGHT["header"])
        bar.grid(row=0, column=0, sticky="ew")
        bar.grid_propagate(False)
        bar.columnconfigure(3, weight=1)  # spacer: pushes the right-hand controls out

        # Icon-only mark; the wordmark is too wide for a 52px bar.
        logo_path = resource_path("logos/c1_icon_logo_transparent.png")
        try:
            from PIL import Image as _PILImage
            pil_img = _PILImage.open(logo_path)
            self._toolbar_logo = ctk.CTkImage(light_image=pil_img, size=(22, 22))
            ctk.CTkLabel(bar, image=self._toolbar_logo, text="").grid(
                row=0, column=0, padx=(theme.PAD["lg"], 10))
        except Exception:
            ctk.CTkLabel(bar, text="C1",
                         font=theme.ui_font(theme.SIZE["title"], "bold"),
                         text_color=theme.ACCENT).grid(
                row=0, column=0, padx=(theme.PAD["lg"], 10))

        title_cell = ctk.CTkFrame(bar, fg_color="transparent")
        title_cell.grid(row=0, column=1, sticky="w")
        self._title_lbl = ctk.CTkLabel(
            title_cell, text="No project open",
            font=theme.ui_font(theme.SIZE["title"], "bold"),
            text_color=theme.TEXT_TERTIARY, anchor="w",
        )
        self._title_lbl.pack(anchor="w")
        # The source filename identifies the project, so it belongs under the
        # project name rather than off in the status bar.
        self._source_lbl = ctk.CTkLabel(
            title_cell, text="", font=theme.ui_font(theme.SIZE["meta"]),
            text_color=theme.TEXT_TERTIARY, anchor="w",
        )

        # Save state reads as a pill beside the title; hidden with no project.
        self._save_pill = Chip(bar, "", variant="success",
                               size=theme.SIZE["label"])

        self._mode_toggle = ModeToggle(bar, on_change=self._on_mode_change)
        self._mode_toggle.grid(row=0, column=4, padx=(0, theme.PAD["sm"]))

        # File/worksheet actions now live in the native menu bar. The only
        # toolbar control is a quick "close project" affordance at the far
        # right; the weight-1 spacer column (col 3) pushes it there.
        self._close_btn = ghost_button(
            bar, "✕  Close project", self._process_another,
            height=theme.HEIGHT["button_md"], width=120,
            border_width=1, border_color=theme.BORDER_STRONG,
        )
        self._close_btn.grid(row=0, column=5, padx=(0, theme.PAD["md"]))
        self._set_toolbar_enabled(False)

    def _build_statusbar(self):
        bar = ctk.CTkFrame(self.root, fg_color=theme.CHROME,
                           corner_radius=0, height=theme.HEIGHT["statusbar"])
        bar.grid(row=5, column=0, sticky="ew")
        bar.grid_propagate(False)
        bar.columnconfigure(1, weight=1)

        self._status_lbl = ctk.CTkLabel(bar, text="Ready",
                                        font=theme.ui_font(theme.SIZE["meta"]),
                                        text_color=theme.TEXT_TERTIARY, anchor="w")
        self._status_lbl.grid(row=0, column=0, sticky="w",
                              padx=(theme.PAD["lg"], theme.PAD["md"]))

        # Deliberately NOT a second copy of the save state or the validation
        # chips — the header pill and the editor footer own those. This bar
        # answers the question neither of them does: where do generated files
        # land?
        self._outdir_lbl = ctk.CTkLabel(bar, text="",
                                        font=theme.ui_font(theme.SIZE["meta"]),
                                        text_color=theme.TEXT_TERTIARY, anchor="e")
        self._outdir_lbl.grid(row=0, column=2, sticky="e", padx=(0, theme.PAD["md"]))

        self._term_btn = ghost_button(
            bar, "▷ terminal", self._toggle_terminal,
            width=84, height=20, text_color=theme.ACCENT,
            font=theme.ui_font(theme.SIZE["meta"]),
        )
        self._term_btn.grid(row=0, column=3, sticky="e", padx=(0, theme.PAD["sm"]))

    def _build_terminal(self):
        self.term_text = ctk.CTkTextbox(
            self.term_section,
            height=200,
            font=ctk.CTkFont(family=theme.MONO_FAMILY, size=theme.SIZE["meta"]),
            fg_color=theme.SURFACE_SUBTLE,
            text_color=theme.TEXT_SECOND,
            border_width=1,
            border_color=theme.BORDER,
            corner_radius=theme.RADIUS["card"],
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

    def _on_mode_change(self, mode: str):
        """Persist the header toggle's choice. app.py owns the prefs file."""
        save_prefs({**load_prefs(), "appearance_mode": mode})

    def _set_project_title(self, text: str | None, dirty: bool = False,
                           status: str = ""):
        """Paint the header's project block. `status` is the editor's save
        string ("Saved 2:41 PM ✓"), which the saved pill shows verbatim."""
        if text:
            self._title_lbl.configure(text=text.upper(), text_color=theme.TEXT)
            if self._source_lbl.winfo_manager() != "pack":
                self._source_lbl.pack(anchor="w")
            self._save_pill.set_variant("warning" if dirty else "success")
            self._save_pill.set_text(
                "● Unsaved changes" if dirty else (status or "Saved ✓"))
            self._save_pill.grid(row=0, column=2, padx=(theme.PAD["md"], 0))
        else:
            self._title_lbl.configure(text="No project open",
                                      text_color=theme.TEXT_TERTIARY)
            self._source_lbl.pack_forget()
            self._save_pill.grid_remove()

    def _on_editor_status(self, text: str, dirty: bool):
        """EditorFrame save-state callback → the header save pill.

        The status bar deliberately stays out of this: the pill and the editor
        footer's Save button already say it, and a third voice saying the same
        thing reads as noise, not reassurance."""
        school = ""
        if self.session:
            school = self.session.design.site_info.school_name or "Untitled project"
        self._set_project_title(school, dirty, status=text)
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
        """Validation is surfaced by the tab-bar badges and the footer's issue
        chips, both owned by EditorFrame. The shell has nothing to add."""

    def _show_flow(self):
        """Show the centered flow column (and hide the editor surface)."""
        self.editor_section.grid_remove()
        self._flow_host.grid(row=0, column=0, sticky="nsew")

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
        self._status_lbl.configure(text="Ready", text_color=theme.TEXT_TERTIARY)
        self._source_lbl.configure(text="")
        self._outdir_lbl.configure(text="")

        # Tk frames can't draw a dashed border, so the spec's dashed outline
        # degrades to a solid 2px one in the same colour.
        dz = ctk.CTkFrame(
            self.input_section,
            border_width=2,
            border_color=theme.BORDER_STRONG,
            corner_radius=12,
            fg_color=theme.SURFACE,
        )
        dz.grid(row=1, column=0, sticky="ew")
        dz.columnconfigure(0, weight=1)
        # Let the zone shrink-wrap its contents and impose the target size as a
        # row floor instead. Pinning the frame's own height clipped the
        # file-type chips off the bottom edge the moment the stack grew.
        self.input_section.rowconfigure(1, minsize=168)
        self._drop_zone = dz

        IconTile(dz, "⬆", size=44).grid(row=0, column=0, pady=(22, 10))
        ctk.CTkLabel(
            dz, text="Drop a design file to start",
            font=theme.ui_font(theme.SIZE["drop_title"], "bold"),
            text_color=theme.TEXT,
        ).grid(row=1, column=0)

        sub = ctk.CTkFrame(dz, fg_color="transparent")
        sub.grid(row=2, column=0, pady=(3, 0))
        ctk.CTkLabel(sub, text="or ", font=theme.ui_font(theme.SIZE["chip"]),
                     text_color=theme.TEXT_SECOND).pack(side="left")
        ctk.CTkLabel(sub, text="browse…",
                     font=theme.ui_font(theme.SIZE["chip"], "bold"),
                     text_color=theme.ACCENT).pack(side="left")

        types = ctk.CTkFrame(dz, fg_color="transparent")
        types.grid(row=3, column=0, pady=(12, 20))
        for kind in ("PDF", "XLSX", "DMPS"):
            Chip(types, kind, size=theme.SIZE["label"]).pack(side="left", padx=3)

        def walk(widget):
            yield widget
            for child in widget.winfo_children():
                yield from walk(child)

        for widget in walk(dz):
            widget.bind("<Button-1>", lambda _e: self._choose_pdf())
            widget.bind("<Enter>", lambda _e: self._set_drop_active(True))
            widget.bind("<Leave>", lambda _e: self._set_drop_active(False))

        self._show_recent_projects()
        self._show_output_dir_row()

    def _set_drop_active(self, active: bool):
        """Accent the drop zone — shared by pointer hover and drag-over."""
        dz = getattr(self, "_drop_zone", None)
        if dz is None or not dz.winfo_exists():
            return
        dz.configure(border_color=theme.ACCENT if active else theme.BORDER_STRONG)

    def _on_drag_over(self, event):
        self._set_drop_active(True)
        # tkdnd reads the handler's return value as the accepted action;
        # anything else refuses the drop.
        return getattr(event, "action", "copy")

    def _on_drag_out(self, event):
        self._set_drop_active(False)
        return getattr(event, "action", "copy")

    def _show_output_dir_row(self):
        # Output-folder picker lives on the home screen now that the old
        # job-details form (its previous host) is gone.
        row = ctk.CTkFrame(self.input_section, fg_color="transparent")
        row.grid(row=3, column=0, sticky="ew", pady=(theme.PAD["md"], 0))
        row.columnconfigure(1, weight=1)
        ctk.CTkLabel(row, text="Save output to:",
                     font=theme.ui_font(theme.SIZE["meta"]),
                     text_color=theme.TEXT_TERTIARY).grid(row=0, column=0, sticky="w")
        self._output_dir_label = ctk.CTkLabel(
            row, text=str(self.output_dir), font=theme.ui_font(theme.SIZE["meta"]),
            text_color=theme.ACCENT, anchor="w",
        )
        self._output_dir_label.grid(row=0, column=1, sticky="w", padx=(6, 0))
        change = ctk.CTkLabel(
            row, text="Change", text_color=theme.ACCENT,
            font=ctk.CTkFont(size=theme.SIZE["meta"], weight="bold", underline=True),
        )
        change.grid(row=0, column=2, padx=(theme.PAD["sm"], 0))
        bind_click(change, self._choose_output_dir)

    def _show_recent_projects(self):
        recents = list_recent_sessions(limit=4)
        if not recents:
            return

        frame = ctk.CTkFrame(self.input_section, fg_color="transparent")
        frame.grid(row=2, column=0, sticky="ew", pady=(theme.PAD["lg"], 0))
        frame.columnconfigure(0, weight=1)

        SectionLabel(frame, "Recent projects").grid(
            row=0, column=0, sticky="w", pady=(0, 6))

        for i, summary in enumerate(recents):
            card = Card(frame, hoverable=True)
            card.grid(row=i + 1, column=0, sticky="ew", pady=3)
            card.columnconfigure(1, weight=1)

            initial = (summary.school_name or "?").strip()[:1].upper() or "?"
            IconTile(card, initial, size=34,
                     font_size=theme.SIZE["title"]).grid(
                row=0, column=0, rowspan=2, padx=(theme.PAD["md"], 10),
                pady=theme.PAD["sm"])

            ctk.CTkLabel(
                card, text=summary.school_name,
                font=theme.ui_font(theme.SIZE["body"], "bold"),
                text_color=theme.TEXT, anchor="w",
            ).grid(row=0, column=1, sticky="w", pady=(theme.PAD["sm"], 0))
            ctk.CTkLabel(
                card, text=self._recent_meta(summary),
                font=theme.ui_font(theme.SIZE["meta"]),
                text_color=theme.TEXT_TERTIARY, anchor="w",
            ).grid(row=1, column=1, sticky="w", pady=(0, theme.PAD["sm"]))

            primary_button(
                card, "Open", lambda p=summary.path: self._open_session_path(p),
                height=theme.HEIGHT["button_sm"], width=64,
            ).grid(row=0, column=2, rowspan=2, padx=(theme.PAD["sm"], 6))
            remove_button(
                card,
                lambda p=summary.path, n=summary.school_name:
                    self._delete_session(p, n),
            ).grid(row=0, column=3, rowspan=2, padx=(0, theme.PAD["sm"]))

    @staticmethod
    def _recent_meta(summary) -> str:
        """"Saved Jul 1, 1:38 PM · from northview.pdf" — the ISO timestamp read
        as machine output, so it's rendered the way a person would say it.
        %-d / %-I aren't portable, hence the manual assembly."""
        saved = summary.saved_at or ""
        with contextlib.suppress(ValueError, TypeError):
            when = datetime.fromisoformat(summary.saved_at)
            saved = (f"{when:%b} {when.day}, {when.hour % 12 or 12}"
                     f":{when:%M} {when:%p}")
        parts = [f"Saved {saved}"] if saved else []
        if summary.source_name:
            parts.append(f"from {summary.source_name}")
        return "  ·  ".join(parts)

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

        card = Card(self.input_section, border_color=theme.ACCENT,
                    fg_color=theme.ACCENT_TINT)
        card.grid(row=0, column=0, sticky="ew")
        card.columnconfigure(1, weight=1)

        IconTile(card, "📄", size=40, tint=theme.SURFACE,
                 font_size=20).grid(row=0, column=0, rowspan=2,
                                    padx=(theme.PAD["md"], 10),
                                    pady=theme.PAD["md"])

        name_text = filename if len(filename) <= 52 else filename[:49] + "…"
        ctk.CTkLabel(
            card,
            text=name_text,
            font=theme.ui_font(theme.SIZE["control"], "bold"),
            text_color=theme.TEXT,
            anchor="w",
        ).grid(row=0, column=1, sticky="w", pady=(theme.PAD["md"], 2))

        if parsing:
            det_frame = ctk.CTkFrame(card, fg_color="transparent")
            det_frame.grid(row=1, column=1, sticky="w", pady=(0, theme.PAD["md"]))
            self._det_spinner_lbl = ctk.CTkLabel(
                det_frame, text="⠋", text_color=theme.TEXT_SECOND,
                font=theme.ui_font(theme.SIZE["control"]), width=18,
            )
            self._det_spinner_lbl.pack(side="left")
            ctk.CTkLabel(
                det_frame, text=busy_text, text_color=theme.TEXT_SECOND,
                font=theme.ui_font(theme.SIZE["control"]),
            ).pack(side="left")
            self._start_label_spinner(self._det_spinner_lbl)
        else:
            ctk.CTkLabel(
                card,
                text=f"Detected: {school_name}",
                text_color=theme.ACCENT,
                font=theme.ui_font(theme.SIZE["control"], "bold"),
                anchor="w",
            ).grid(row=1, column=1, sticky="w", pady=(0, theme.PAD["md"]))

        secondary_button(
            card, "Replace", self._replace_pdf,
            height=theme.HEIGHT["button_sm"], width=80,
        ).grid(row=0, column=2, rowspan=2, padx=theme.PAD["md"],
               pady=theme.PAD["md"])

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
        self._show_output_dir_status()

    def _show_output_dir_status(self):
        """Mirror the output folder in the status bar while a project is open —
        the home screen's picker is out of sight once you're editing, and it's
        the first thing you look for after a generate."""
        text = f"Output → {self.output_dir}" if self.state == "editing" else ""
        with contextlib.suppress(Exception):
            self._outdir_lbl.configure(text=text)

    # ------------------------------------------------------------------ #
    # Error card                                                            #
    # ------------------------------------------------------------------ #

    def _make_error_card(self, parent, title: str, exc: Exception, retry):
        card = Card(parent, fg_color=theme.ERROR_TINT,
                    border_color=theme.ERROR_BORDER)
        card.grid(row=0, column=0, sticky="ew", pady=theme.PAD["xs"])
        card.columnconfigure(1, weight=1)

        ctk.CTkLabel(card, text="⚠", text_color=theme.ERROR,
                     font=theme.ui_font(20, "bold")).grid(
            row=0, column=0, padx=(theme.PAD["lg"], theme.PAD["sm"]),
            pady=(theme.PAD["lg"], theme.PAD["xs"]), sticky="n")

        info = ctk.CTkFrame(card, fg_color="transparent")
        info.grid(row=0, column=1, sticky="ew", pady=theme.PAD["md"],
                  padx=(0, theme.PAD["lg"]))
        ctk.CTkLabel(info, text=title,
                     font=theme.ui_font(theme.SIZE["body"], "bold"),
                     text_color=theme.ERROR, anchor="w").pack(anchor="w")
        ctk.CTkLabel(info, text=str(exc)[:200],
                     font=theme.ui_font(theme.SIZE["meta"]),
                     text_color=theme.TEXT_SECOND, anchor="w",
                     wraplength=380).pack(anchor="w", pady=(2, theme.PAD["sm"]))
        primary_button(info, "Try again", retry, width=90,
                       height=theme.HEIGHT["button_md"]).pack(anchor="w")

    # ------------------------------------------------------------------ #
    # Toast                                                                 #
    # ------------------------------------------------------------------ #

    def _open_with_toast(self, path: Path | None):
        if not path:
            return
        open_file(path)
        self._show_toast(f"Opening {path.name} in Excel…")

    def _show_toast(self, message: str, action: tuple | None = None, *,
                    meta: str | None = None, folder: Path | None = None):
        """Slide-in completion card, bottom-right of the window.

        `action` is an optional ("Label", callback) button — used by generation
        completions ("rev 3 ready — Open"); a toast with an action lingers
        longer so it can actually be clicked. `meta` is the second line and
        `folder` adds a "Folder" button that reveals that path.

        It's a raw tk.Toplevel, so every colour goes through theme.resolve()
        and is re-applied on a mode switch. Tk gives raw frames no corner
        radius, so the card and the ✓ tile are square-cornered.
        """
        cursor = "pointinghand" if sys.platform == "darwin" else "hand2"

        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)
        # Tie it to the main window: without this, anything that raises the
        # root (clicking back into the app after a generate) buries the toast
        # behind it, and the "Open" button it exists to offer is unreachable.
        toast.transient(self.root)
        # The toplevel's own background, showing through a 1px inset, IS the
        # card border — a raw tk.Frame has no border_color of its own.
        card = tk.Frame(toast)
        card.pack(fill="both", expand=True, padx=1, pady=1)
        strip = tk.Frame(card, width=3)
        strip.pack(side="left", fill="y")
        body = tk.Frame(card)
        body.pack(side="left", fill="both", expand=True, padx=(11, 14),
                  pady=theme.PAD["md"])

        tile = tk.Frame(body, width=30, height=30)
        tile.pack(side="left", padx=(0, 10))
        tile.pack_propagate(False)
        tile_lbl = tk.Label(tile, text="✓", font=theme.ui_font(15, "bold"))
        tile_lbl.pack(expand=True)

        text_col = tk.Frame(body)
        text_col.pack(side="left", fill="both", expand=True)
        title_lbl = tk.Label(text_col, text=message, anchor="w",
                             font=theme.ui_font(theme.SIZE["body"], "bold"))
        title_lbl.pack(anchor="w")
        meta_lbl = None
        if meta:
            meta_lbl = tk.Label(text_col, text=meta, anchor="w",
                                font=theme.ui_font(theme.SIZE["meta"]))
            meta_lbl.pack(anchor="w", pady=(1, 0))

        def close():
            if toast.winfo_exists():
                toast.destroy()

        # tk.Button ignores bg on macOS's aqua theme, so the two actions are
        # click-bound labels — the only way to hold the accent fill on every OS.
        buttons: list[tuple[tk.Label, bool, tk.Frame]] = []

        def add_button(label: str, callback, primary: bool):
            # The border is a 1px frame showing through an inset, the same
            # trick the card itself uses. highlightthickness on an aqua Label
            # paints a ring straight through the text.
            edge = tk.Frame(body)
            edge.pack(side="left", padx=(theme.PAD["sm"], 0))
            btn = tk.Label(edge, text=label, cursor=cursor, padx=12, pady=6,
                           highlightthickness=0, borderwidth=0,
                           font=theme.ui_font(theme.SIZE["chip"], "bold"))
            btn.pack(padx=1, pady=1)
            for w in (edge, btn):
                w.bind("<Button-1>", lambda _e: (callback(), close()))
            buttons.append((btn, primary, edge))

        if action:
            add_button(action[0], action[1], True)
        if folder is not None:
            add_button("Folder", lambda: reveal_in_folder(folder), False)

        def style(_mode=None):
            if not toast.winfo_exists():
                return
            surface = theme.resolve(theme.SURFACE)
            toast.configure(bg=theme.resolve(theme.BORDER))
            strip.configure(bg=theme.resolve(theme.SUCCESS))
            for frame in (card, body, text_col, tile):
                frame.configure(bg=surface)
            tile.configure(bg=theme.resolve(theme.SUCCESS_TINT))
            tile_lbl.configure(bg=theme.resolve(theme.SUCCESS_TINT),
                               fg=theme.resolve(theme.SUCCESS))
            title_lbl.configure(bg=surface, fg=theme.resolve(theme.TEXT))
            if meta_lbl is not None:
                meta_lbl.configure(bg=surface,
                                   fg=theme.resolve(theme.TEXT_TERTIARY))
            for btn, primary, edge in buttons:
                if primary:
                    btn.configure(bg=theme.resolve(theme.ACCENT),
                                  fg=theme.resolve(theme.ON_ACCENT))
                    edge.configure(bg=theme.resolve(theme.ACCENT))
                else:
                    btn.configure(bg=surface, fg=theme.resolve(theme.TEXT))
                    edge.configure(bg=theme.resolve(theme.BORDER_STRONG))

        style()
        theme.bind_mode_change(toast, style)

        # A full update(), not merely update_idletasks(): the labels carry
        # CTkFonts, and until Tk has actually realised those fonts the toplevel
        # under-reports its requested width — which clipped the trailing action
        # button off the card, the one thing a toast with actions must not do.
        self.root.update_idletasks()
        toast.update()
        tw = toast.winfo_reqwidth()
        th = toast.winfo_reqheight()
        toast.geometry(f"{tw}x{th}")
        rx, ry = self.root.winfo_x(), self.root.winfo_y()
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        tx = rx + rw - tw - 20
        # Float clear of the footer: the generate buttons live there, and a
        # notification that covers the controls it is reporting on is worse
        # than one that sits above them.
        ty_end = ry + rh - th - theme.HEIGHT["footer"] - 16
        ty_start = ty_end + 20
        toast.geometry(f"+{tx}+{ty_start}")

        steps = 8
        for i in range(steps + 1):
            y = int(ty_start + (ty_end - ty_start) * i / steps)
            self.root.after(int(i * 180 / steps),
                            lambda _y=y: toast.geometry(f"+{tx}+{_y}") if toast.winfo_exists() else None)
        linger = 6000 if action else 1800
        self.root.after(linger, close)

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
        self._show_output_dir_status()

    def _show_editor_surface(self):
        """Full-bleed editor; flow column hidden."""
        self._flow_host.grid_remove()
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
        self._outdir_lbl.configure(text="")

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
                self._show_toast(
                    f"Worksheet rev {rev} ready",
                    action=("Open", lambda: open_file(out_path)),
                    meta=f"{out_path.name} · {len(design.zones)} zones",
                    folder=out_path)

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
            zones = len(self.session.design.zones) if self.session else 0
            self._show_toast(
                f"Door chart rev {rev} ready",
                action=("Open", lambda: open_file(chart_path)),
                meta=f"{chart_path.name} · {zones} zones",
                folder=chart_path)

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
        dlg.configure(fg_color=theme.APP_BG)
        dlg.transient(self.root)
        dlg.grab_set()

        ctk.CTkLabel(dlg, text="Generate RemoteLink Account",
                     font=theme.ui_font(16, "bold"),
                     text_color=theme.TEXT).pack(
            anchor="w", padx=20, pady=(18, 2))
        ctk.CTkLabel(
            dlg, text="Builds an encrypted .xml you import into RemoteLink.",
            font=theme.ui_font(theme.SIZE["chip"]),
            text_color=theme.TEXT_SECOND).pack(anchor="w", padx=20, pady=(0, 10))

        form = ctk.CTkFrame(dlg, fg_color="transparent")
        form.pack(fill="x", padx=20)
        form.columnconfigure(1, weight=1)

        ctk.CTkLabel(form, text="Account number", text_color=theme.TEXT,
                     font=theme.ui_font(theme.SIZE["body"])).grid(
            row=0, column=0, sticky="w", pady=6)
        acct_var = ctk.StringVar(value=default_account)
        ctk.CTkEntry(form, textvariable=acct_var, fg_color=theme.SURFACE,
                     border_color=theme.BORDER_STRONG, text_color=theme.TEXT,
                     corner_radius=theme.RADIUS["button"]).grid(
            row=0, column=1, sticky="ew", pady=6)

        ctk.CTkLabel(form, text="Passphrase", text_color=theme.TEXT,
                     font=theme.ui_font(theme.SIZE["body"])).grid(
            row=1, column=0, sticky="w", pady=6)
        pass_var = ctk.StringVar(value="")
        ctk.CTkEntry(form, textvariable=pass_var, show="•",
                     fg_color=theme.SURFACE, border_color=theme.BORDER_STRONG,
                     text_color=theme.TEXT,
                     corner_radius=theme.RADIUS["button"]).grid(
            row=1, column=1, sticky="ew", pady=6)

        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=16)
        secondary_button(btns, "Cancel", dlg.destroy, width=90).pack(side="left")

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

        primary_button(btns, "Generate", submit, width=120).pack(side="right")

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
            self._show_toast(
                f"RemoteLink account {account_num} ready",
                action=("Open", lambda: open_file(path)),
                meta=f"{Path(path).name} · import it into RemoteLink",
                folder=Path(path))

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
        dlg.configure(fg_color=theme.APP_BG)
        dlg.transient(self.root)
        dlg.protocol("WM_DELETE_WINDOW", lambda: self._close_update_dialog())

        cur = updater.current_version_str()
        ctk.CTkLabel(
            dlg, text=f"Version {info['tag'].lstrip('v')} is available",
            font=theme.ui_font(16, "bold"), text_color=theme.TEXT).pack(
            padx=20, pady=(20, 2))
        ctk.CTkLabel(dlg, text=f"You have v{cur}.",
                     font=theme.ui_font(theme.SIZE["chip"]),
                     text_color=theme.TEXT_SECOND).pack(padx=20, pady=(0, 10))

        notes = ctk.CTkTextbox(
            dlg, height=200, wrap="word", fg_color=theme.SURFACE,
            text_color=theme.TEXT, border_width=1, border_color=theme.BORDER,
            corner_radius=theme.RADIUS["card"],
            font=theme.ui_font(theme.SIZE["chip"]))
        notes.pack(fill="both", expand=True, padx=20)
        notes.insert("1.0", info["notes"] or "See the release page for details.")
        notes.configure(state="disabled")

        btns = ctk.CTkFrame(dlg, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=16)

        def skip():
            # Silence the *automatic* prompt for this release only. The Help-menu
            # "Check for Updates…" ignores skip_version, so a manual update still works.
            prefs = load_prefs()
            prefs["skip_version"] = info["tag"]
            save_prefs(prefs)
            self._close_update_dialog()

        # "Later" just closes — the once-a-day launch check re-prompts on the next
        # run. Only "Skip this version" suppresses this release for good.
        ghost_button(btns, "Skip this version", skip, width=130).pack(side="left")
        secondary_button(btns, "Later", self._close_update_dialog,
                         width=80).pack(side="left", padx=(8, 0))

        if updater.can_self_update() and info["asset_url"]:
            primary_button(btns, "Update Now",
                           lambda: self._start_update(info, dlg, btns),
                           width=120).pack(side="right")
        else:
            # Dev run or missing asset — fall back to the download page.
            primary_button(
                btns, "Open Download Page",
                lambda: (webbrowser.open(info["html_url"]),
                         self._close_update_dialog()),
                width=160).pack(side="right")

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
        dlg.configure(fg_color=theme.APP_BG)
        dlg.transient(self.root)
        ctk.CTkLabel(dlg, text=title, font=theme.ui_font(16, "bold"),
                     text_color=theme.TEXT).pack(
            anchor="w", padx=20, pady=(18, 8))
        box = ctk.CTkTextbox(
            dlg, wrap="word", fg_color=theme.SURFACE, text_color=theme.TEXT,
            border_width=1, border_color=theme.BORDER,
            corner_radius=theme.RADIUS["card"],
            font=theme.ui_font(theme.SIZE["chip"]))
        box.pack(fill="both", expand=True, padx=20, pady=(0, 8))
        box.insert("1.0", body)
        box.configure(state="disabled")
        primary_button(dlg, "Close", dlg.destroy, width=90).pack(pady=(0, 16))

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
        status = ctk.CTkLabel(btns, text="Downloading…", text_color=theme.TEXT,
                              font=theme.ui_font(theme.SIZE["chip"]))
        status.pack(side="left")
        bar = ctk.CTkProgressBar(btns, width=200, progress_color=theme.ACCENT,
                                 fg_color=theme.SURFACE_CHIP)
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
