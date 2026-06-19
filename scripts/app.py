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

from paths import resource_path, output_dir
from generate_dmp_ws import (
    build_dmp_design_from_pdf,
    dmp_filename,
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
)
from editor_frame import EditorFrame
import updater

DOOR_CHART_TEMPLATE = resource_path("door_chart_template_blank.xlsx")
PREFS_PATH = Path.home() / ".c1_door_chart_app.json"
ACCENT = "#4a7bb8"
ACCENT_HOVER = "#3a6aa8"
SPINNER_FRAMES = list("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

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

        # Dual-gate: state advances only when BOTH animation AND pipeline finish
        self._anim_done = False
        self._pipe_done = False
        self._pipe_result = None
        self._pipe_error = None

        self._spinner_jobs: list[str] = []

        # Output folder — configurable per machine via the meta section picker.
        self.output_dir: Path = output_dir()

        self._build_layout()
        self._show_drop_zone()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

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
        self.root.bind_all(f"<{mod}-e>", lambda _e=None: self._export_draft())
        self.root.bind_all(f"<{mod}-Shift-F>", lambda _e=None: self._finalize_clicked())

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
        ws_menu.add_command(label="Export Draft", accelerator=accel("E"),
                            command=self._export_draft)
        ws_menu.add_command(label="Finalize…", accelerator=accel("Shift+F"),
                            command=self._finalize_clicked)
        self._menubar.add_cascade(label="Worksheet", menu=ws_menu)

        # ---- Help (non-macOS: no application menu, so updates live here) ----
        if not is_mac:
            help_menu = tk.Menu(self._menubar, tearoff=0)
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
        """Enable the worksheet actions only while a project is open."""
        editing = self.state == "editing" and self.editor is not None
        state = "normal" if editing else "disabled"
        self._worksheet_menu.entryconfigure("Export Draft", state=state)
        self._worksheet_menu.entryconfigure("Finalize…", state=state)

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

    def _finalize_clicked(self):
        if self.state == "editing" and self.editor:
            self.editor.show_finalize_dialog()

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
    # Section 3 — Action region                                            #
    # ------------------------------------------------------------------ #

    def _clear_action_section(self):
        for w in self.action_section.winfo_children():
            w.destroy()
        self._show_flow()
        self.action_section.grid(row=1, column=0, sticky="ew", pady=(16, 0))

    def _show_action_generating_dmp(self, steps: list[str]):
        self._clear_action_section()

        ctk.CTkLabel(
            self.action_section,
            text="Generating DMP Worksheet",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        cl_frame = ctk.CTkFrame(self.action_section, fg_color="transparent")
        cl_frame.grid(row=1, column=0, sticky="ew")
        self._run_checklist(cl_frame, steps, self._on_dmp_anim_done)

    def _show_action_review_dmp(self, header: str = "Review DMP Worksheet",
                                btn1_text: str = "Open DMP in Excel",
                                btn2_text: str = "Looks good — generate door chart",
                                note: str = ""):
        self._clear_action_section()

        ctk.CTkLabel(
            self.action_section,
            text=header,
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 12 if not note else 4))

        if note:
            ctk.CTkLabel(
                self.action_section,
                text=note,
                font=ctk.CTkFont(size=11),
                text_color=ACCENT,
                anchor="w",
                justify="left",
                wraplength=520,
            ).grid(row=1, column=0, sticky="w", pady=(0, 12))

        btn_row = ctk.CTkFrame(self.action_section, fg_color="transparent")
        btn_row.grid(row=2, column=0, sticky="ew")
        btn_row.columnconfigure(0, weight=1)
        btn_row.columnconfigure(1, weight=1)

        ctk.CTkButton(
            btn_row,
            text=btn1_text,
            height=40,
            fg_color="transparent",
            border_width=2,
            border_color=ACCENT,
            text_color=ACCENT,
            hover_color=("gray90", "gray25"),
            command=lambda: self._open_with_toast(self.dmp_path),
        ).grid(row=0, column=0, sticky="ew", padx=(0, 6))

        ctk.CTkButton(
            btn_row,
            text=btn2_text,
            height=40,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            font=ctk.CTkFont(weight="bold"),
            command=self._start_generating_chart,
        ).grid(row=0, column=1, sticky="ew", padx=(6, 0))

    def _show_action_generating_chart(self, steps: list[str]):
        self._clear_action_section()

        ctk.CTkLabel(
            self.action_section,
            text="Generating Door Chart",
            font=ctk.CTkFont(size=13, weight="bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))

        cl_frame = ctk.CTkFrame(self.action_section, fg_color="transparent")
        cl_frame.grid(row=1, column=0, sticky="ew")
        self._run_checklist(cl_frame, steps, self._on_chart_anim_done)

    def _show_action_done(self):
        self._clear_action_section()

        banner = ctk.CTkFrame(self.action_section, fg_color="#ecfaef", corner_radius=10)
        banner.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        banner.columnconfigure(1, weight=1)

        check_bg = ctk.CTkFrame(banner, fg_color="#34c759", corner_radius=14, width=28, height=28)
        check_bg.grid(row=0, column=0, padx=(16, 10), pady=16)
        check_bg.grid_propagate(False)
        ctk.CTkLabel(check_bg, text="✓", text_color="white", font=ctk.CTkFont(size=14, weight="bold")).place(
            relx=0.5, rely=0.5, anchor="center"
        )

        info = ctk.CTkFrame(banner, fg_color="transparent")
        info.grid(row=0, column=1, sticky="w", pady=16)
        ctk.CTkLabel(info, text="Done!", font=ctk.CTkFont(size=15, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(
            info,
            text=f"Both files written to {self.output_dir}",
            font=ctk.CTkFont(family="Courier", size=11),
            text_color="gray50",
            wraplength=460,
            justify="left",
        ).pack(anchor="w")

        btn_row = ctk.CTkFrame(self.action_section, fg_color="transparent")
        btn_row.grid(row=1, column=0, sticky="ew")
        for col in range(3):
            btn_row.columnconfigure(col, weight=1)

        ctk.CTkButton(
            btn_row,
            text="Open DMP",
            height=38,
            fg_color="transparent",
            border_width=2,
            border_color=ACCENT,
            text_color=ACCENT,
            hover_color=("gray90", "gray25"),
            command=lambda: self._open_with_toast(self.dmp_path),
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4))

        ctk.CTkButton(
            btn_row,
            text="Open Door Chart",
            height=38,
            fg_color="transparent",
            border_width=2,
            border_color=ACCENT,
            text_color=ACCENT,
            hover_color=("gray90", "gray25"),
            command=lambda: self._open_with_toast(self.door_chart_path),
        ).grid(row=0, column=1, sticky="ew", padx=4)

        ctk.CTkButton(
            btn_row,
            text="Process another →",
            height=38,
            fg_color=ACCENT,
            hover_color=ACCENT_HOVER,
            command=self._process_another,
        ).grid(row=0, column=2, sticky="ew", padx=(4, 0))

    def _show_action_error(self, title: str, exc: Exception, retry):
        self._clear_action_section()
        self._make_error_card(self.action_section, title, exc, retry)

    # ------------------------------------------------------------------ #
    # Animated checklist                                                    #
    # ------------------------------------------------------------------ #

    def _run_checklist(self, container: ctk.CTkFrame, steps: list[str], on_done):
        STEP_INTERVAL = 480
        PENDING_DURATION = 320
        spinner_labels: list[ctk.CTkLabel] = []

        def _append_pending(i: int):
            if not container.winfo_exists():
                return
            row = ctk.CTkFrame(container, fg_color="transparent")
            row.pack(anchor="w", pady=2)
            spin_idx = [0]
            sp = ctk.CTkLabel(row, text=SPINNER_FRAMES[0], text_color="gray50",
                              font=ctk.CTkFont(size=13), width=20)
            sp.pack(side="left")
            ctk.CTkLabel(row, text=steps[i], font=ctk.CTkFont(size=12)).pack(side="left", padx=4)
            spinner_labels.append(sp)

            def _spin():
                if not sp.winfo_exists():
                    return
                spin_idx[0] += 1
                sp.configure(text=SPINNER_FRAMES[spin_idx[0] % len(SPINNER_FRAMES)])
                job = self.root.after(80, _spin)
                self._spinner_jobs.append(job)
            _spin()

        def _promote(i: int):
            if i < len(spinner_labels) and spinner_labels[i].winfo_exists():
                spinner_labels[i].configure(text="✓", text_color="#34c759")

        for i in range(len(steps)):
            delay = 0 if i == 0 else i * STEP_INTERVAL
            self.root.after(delay, _append_pending, i)
            self.root.after(delay + PENDING_DURATION, _promote, i)

        total = (len(steps) - 1) * STEP_INTERVAL + PENDING_DURATION + 380
        self.root.after(total, on_done)

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

    def _show_toast(self, message: str):
        self.root.update_idletasks()
        rx, ry = self.root.winfo_x(), self.root.winfo_y()
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        tw, th = 360, 48
        tx = rx + (rw - tw) // 2
        ty_end = ry + rh - 70
        ty_start = ty_end + 20

        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True)
        toast.configure(bg="#1c1c1e")
        toast.geometry(f"{tw}x{th}+{tx}+{ty_start}")
        toast.attributes("-alpha", 0.92)
        tk.Label(toast, text=message, bg="#1c1c1e", fg="white",
                 font=("Helvetica", 12), padx=16, pady=12).pack(expand=True)

        steps = 8
        for i in range(steps + 1):
            y = int(ty_start + (ty_end - ty_start) * i / steps)
            self.root.after(int(i * 180 / steps),
                            lambda _y=y: toast.geometry(f"{tw}x{th}+{tx}+{_y}") if toast.winfo_exists() else None)
        self.root.after(1800, lambda: toast.destroy() if toast.winfo_exists() else None)

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
        """Load an already-generated DMP worksheet (.xlsx) and jump straight to the
        review/door-chart step, skipping PDF parsing and worksheet generation.

        The door-chart flow (_start_generating_chart) re-parses self.dmp_path from
        disk, so any edits the user makes in Excel between here and 'Generate Door
        Charts' are picked up automatically.
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
            if getattr(design, "dmp_status", "") == "DRAFT":
                school = design.site_info.school_name or "this school"
                self.state = "idle"
                self.dmp_path = None
                self._show_parse_error(
                    ValueError(
                        "This is a DRAFT export — drafts can't be re-imported. "
                        f"Open the saved project for {school} instead."
                    ),
                    title="Draft worksheet refused",
                )
                return
            school = design.site_info.school_name or "Unknown"
            self._show_file_card(xlsx_path.name, parsing=False, school_name=school)
            self._enter_editor(Session(design=design, source_kind="xlsx",
                                       source_name=xlsx_path.name))

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

        self._teardown_editor()
        self.editor = EditorFrame(
            self.editor_section, self.root, session,
            on_finalize=self._finalize_from_editor,
            on_status_change=self._on_editor_status,
            on_validation_change=self._on_editor_validation,
            on_generate_charts=(self._charts_from_worksheet
                                if session.source_kind == "xlsx" else None),
        )
        self.editor.grid(row=0, column=0, sticky="nsew")
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

    def _export_draft(self):
        """Write a DRAFT-stamped worksheet from the current in-memory design."""
        if not self.session:
            return
        if self.editor and self.editor.dirty and messagebox.askyesno(
            "Save project?", "Save the project before exporting the draft?",
        ):
            self.editor.save()

        design = self.session.design
        sync_master_zones(design)
        out_dir = self.output_dir
        school_slug = _slugify(design.site_info.school_name or "output")

        def work():
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / dmp_filename(school_slug, stamp="DRAFT")
            with contextlib.redirect_stdout(self._redirector), \
                 contextlib.redirect_stderr(self._redirector):
                write_dmp_xlsx(design, DEFAULT_TEMPLATE, out_path, stamp="DRAFT")
            return out_path

        def on_done(out_path):
            if messagebox.askyesno(
                "Draft exported",
                f"Draft written to:\n{out_path.name}\n\nOpen it now?",
            ):
                open_file(out_path)

        def on_error(exc):
            messagebox.showerror("Draft export failed", str(exc))

        self._run_async(work, on_done, on_error)

    def _finalize_from_editor(self):
        """Editor's Finalize button → the existing generation flow (which still
        escalates conflicts/topology via the modal dialogs)."""
        if not self.session:
            return
        if self.editor and self.editor.dirty and messagebox.askyesno(
            "Save project?", "Save the project before generating?",
        ):
            self.editor.save()
        sync_master_zones(self.session.design)
        self._set_toolbar_enabled(False)
        self._start_generating_dmp()

    def _back_to_editor(self):
        """Return to the editor after a failed/aborted generation."""
        self.state = "editing"
        self.action_section.grid_remove()
        if self.editor:
            self._show_editor_surface()
            self._set_toolbar_enabled(True)
        elif self.session:
            self._enter_editor(self.session)

    def _charts_from_worksheet(self):
        """Quick path for imported worksheets: door charts from the file as-is,
        ignoring any unsaved editor changes (the file on disk is the input)."""
        if self.editor and not self.editor.maybe_close():
            return
        self._set_toolbar_enabled(False)
        self._start_generating_chart()

    # ------------------------------------------------------------------ #
    # Flow: generate DMP                                                    #
    # ------------------------------------------------------------------ #

    def _start_generating_dmp(self):
        # Conflicts and topology review now live in the editor (SPLITTERS tab)
        # and are enforced by the finalize gate rather than modal escalations.

        # Job details now live on the design itself (written through by the
        # editor's SITE tab). Persist the per-machine ones as defaults.
        design = self.parsed_design
        save_prefs({**load_prefs(),
                    "phone": design.site_info.phone or "",
                    "install_tech": design.site_info.install_tech or "",
                    "install_date": design.site_info.install_date or "",
                    "ip_address": design.site_info.ip_address or "",
                    "default_gateway": design.site_info.default_gateway or ""})

        self.state = "generating_dmp"
        self._anim_done = False
        self._pipe_done = False
        self._pipe_result = None
        self._pipe_error = None

        n_rsps      = len(design.rsps)
        n_kps       = len(getattr(design, "keypads", []))
        n_zones     = len(getattr(design, "zones", None) or design.master_zones)
        n_splitters = len(design.splitters)

        steps = [
            "Searchable PDF ready",
            f"Zone schedule parsed ({n_rsps} RSPs, {n_kps} keypads, {n_zones} zones)",
            f"Topology extracted ({n_splitters} splitters)",
            "DMP worksheet written",
        ]
        self._show_action_generating_dmp(steps)

        pdf_path = self.pdf_path
        out_dir = self.output_dir

        def work():
            ensure_searchable_pdf(pdf_path)
            school_slug = _slugify(design.site_info.school_name or "output")
            out_dir.mkdir(parents=True, exist_ok=True)
            out_name = dmp_filename(school_slug, stamp="FINAL")
            dmp_output = out_dir / out_name
            with contextlib.redirect_stdout(self._redirector), \
                 contextlib.redirect_stderr(self._redirector):
                write_dmp_xlsx(design, DEFAULT_TEMPLATE, dmp_output, stamp="FINAL")
            return dmp_output

        def on_done(result):
            if self.state != "generating_dmp":
                return
            self._pipe_result = result
            self._pipe_done = True
            self._check_dmp_gate()

        def on_error(exc):
            if self.state != "generating_dmp":
                return
            self._pipe_error = exc
            self._show_action_error("DMP generation failed", exc, self._back_to_editor)

        self._run_async(work, on_done, on_error)

    def _on_dmp_anim_done(self):
        if self.state != "generating_dmp":
            return
        self._anim_done = True
        self._check_dmp_gate()

    def _check_dmp_gate(self):
        if self._anim_done and self._pipe_done and self._pipe_error is None:
            self.dmp_path = self._pipe_result
            self.state = "review_dmp"
            self._show_action_review_dmp()

    # ------------------------------------------------------------------ #
    # Flow: generate chart                                                  #
    # ------------------------------------------------------------------ #

    def _start_generating_chart(self):
        self.state = "generating_chart"
        self._anim_done = False
        self._pipe_done = False
        self._pipe_result = None
        self._pipe_error = None

        steps = [
            "Read DMP worksheet",
            "Populated Header (school + address)",
            "Populated Master sheet (RSP locations, splitter topology)",
            "Door chart written",
        ]
        self._show_action_generating_chart(steps)

        dmp_path = self.dmp_path

        out_dir = self.output_dir

        def work():
            with contextlib.redirect_stdout(self._redirector), \
                 contextlib.redirect_stderr(self._redirector):
                dmp_design = parse_dmp_worksheet(dmp_path)
            school_slug = _slugify(dmp_design.site_info.school_name or "output")
            out_dir.mkdir(parents=True, exist_ok=True)
            out_name = f"{school_slug}_door_chart_{date.today().isoformat()}.xlsx"
            chart_output = out_dir / out_name
            with contextlib.redirect_stdout(self._redirector), \
                 contextlib.redirect_stderr(self._redirector):
                inject(DOOR_CHART_TEMPLATE, dmp_design, chart_output)
            return chart_output

        def on_done(result):
            if self.state != "generating_chart":
                return
            self._pipe_result = result
            self._pipe_done = True
            self._check_chart_gate()

        def on_error(exc):
            if self.state != "generating_chart":
                return
            self._pipe_error = exc
            self.state = "review_dmp"
            self._show_action_error("Door chart generation failed", exc, self._retry_chart)

        self._run_async(work, on_done, on_error)

    def _retry_chart(self):
        self.state = "review_dmp"
        self._show_action_review_dmp()

    def _on_chart_anim_done(self):
        if self.state != "generating_chart":
            return
        self._anim_done = True
        self._check_chart_gate()

    def _check_chart_gate(self):
        if self._anim_done and self._pipe_done and self._pipe_error is None:
            self.door_chart_path = self._pipe_result
            self.state = "done"
            self._show_action_done()

    # ------------------------------------------------------------------ #
    # Reset                                                                 #
    # ------------------------------------------------------------------ #

    def _process_another(self):
        if self.editor and not self.editor.maybe_close():
            return
        self.pdf_path = None
        self.dmp_path = None
        self.door_chart_path = None
        self.parsed_design = None
        self.session = None
        self.state = "idle"
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

    def _quit_for_update(self):
        """Quit so the detached helper can replace files and relaunch."""
        if self.state == "editing" and self.editor and not self.editor.maybe_close():
            return  # user cancelled the save prompt — leave the app open
        self.root.destroy()

    def _on_close(self):
        if self.state in ("generating_dmp", "generating_chart"):
            if messagebox.askyesno("Quit?", "Generation in progress — quit anyway?"):
                self.root.destroy()
            return
        if self.state == "editing" and self.editor and not self.editor.maybe_close():
            return
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    app = App()
    app.run()


if __name__ == "__main__":
    main()
