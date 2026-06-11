"""The unified project editor — the working document of the field-edit workflow.

EditorFrame hosts a tab per output sheet group (SITE / ZONES / SPLITTERS /
KEYPADS / POWER) over a Session's DMPDesign and fills the application's main
area. Edits write straight onto the design; the session file only changes on
an explicit Save, with a debounced background recovery file guarding against
crashes.

The frame owns the save/dirty/recovery *logic*; the surrounding app shell
owns the *display* (toolbar title, dirty dot, status bar) and is notified
through the on_status_change / on_validation_change callbacks.
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from tkinter import messagebox

import customtkinter as ctk

from session import Session, save_session, sync_master_zones, write_recovery, clear_recovery
from validation import validate_design, badge_counts
from editor_zones import ZonesTab
from editor_tabs import SplittersTab, KeypadsTab, PowerTab, auto_hide_scrollbar

ACCENT = "#4a7bb8"
ACCENT_HOVER = "#3a6aa8"

RECOVERY_DEBOUNCE_MS = 5000

TAB_TITLES = ["SITE", "ZONES", "SPLITTERS", "KEYPADS", "POWER"]


class EditorFrame(ctk.CTkFrame):
    """Tabbed editor over a Session. The app owns generation flows and chrome;
    this frame calls back into them via the callbacks passed at construction."""

    def __init__(self, master, root, session: Session, *,
                 on_finalize, on_status_change=None, on_validation_change=None,
                 on_generate_charts=None):
        super().__init__(master, fg_color="transparent")
        self.root = root
        self.session = session
        self.dirty = False
        self._recovery_job: str | None = None
        self._on_finalize = on_finalize
        self.on_status_change = on_status_change or (lambda text, dirty: None)
        self.on_validation_change = on_validation_change or (lambda text, ok: None)
        self._on_generate_charts = on_generate_charts
        self._site_vars: dict[str, ctk.StringVar] = {}
        self._suspend_traces = False

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self._build_tabs()
        self.refresh_validation()

        # New, never-saved projects start dirty — there is unsaved work by definition.
        if session.saved_at is None:
            self.mark_dirty(write_recovery_now=False)
        else:
            self._notify_status()

    # ------------------------------------------------------------------ #
    # Save model                                                            #
    # ------------------------------------------------------------------ #

    def mark_dirty(self, write_recovery_now: bool = False):
        self.dirty = True
        self._notify_status()
        if self._recovery_job is not None:
            with contextlib.suppress(Exception):
                self.root.after_cancel(self._recovery_job)
        delay = 0 if write_recovery_now else RECOVERY_DEBOUNCE_MS
        self._recovery_job = self.root.after(delay, self._write_recovery)

    def _write_recovery(self):
        self._recovery_job = None
        if not self.dirty:
            return
        with contextlib.suppress(Exception):
            write_recovery(self.session)

    def save(self) -> bool:
        try:
            save_session(self.session)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))
            return False
        self.dirty = False
        if self._recovery_job is not None:
            with contextlib.suppress(Exception):
                self.root.after_cancel(self._recovery_job)
            self._recovery_job = None
        self._notify_status()
        return True

    def maybe_close(self) -> bool:
        """Save/discard/cancel guard before leaving the editor.
        Returns True when it's OK to proceed."""
        if not self.dirty:
            return True
        school = self.session.design.site_info.school_name or "this project"
        answer = messagebox.askyesnocancel(
            "Unsaved changes",
            f"Save changes to {school} before closing?",
        )
        if answer is None:
            return False
        if answer:
            return self.save()
        # Discard: the recovery file would otherwise resurrect the discarded
        # edits on next open.
        if self.session.path:
            clear_recovery(self.session.path)
        return True

    def _notify_status(self):
        if self.dirty:
            self.on_status_change("●  Unsaved changes", True)
            return
        when = ""
        if self.session.saved_at:
            with contextlib.suppress(Exception):
                when = datetime.fromisoformat(self.session.saved_at).strftime("%-I:%M %p")
        self.on_status_change(f"Saved {when} ✓" if when else "Saved ✓", False)

    # ------------------------------------------------------------------ #
    # Tabs                                                                  #
    # ------------------------------------------------------------------ #

    def _build_tabs(self):
        self.tabs = ctk.CTkTabview(
            self,
            segmented_button_selected_color=ACCENT,
            segmented_button_selected_hover_color=ACCENT_HOVER,
        )
        self.tabs.grid(row=0, column=0, sticky="nsew")
        for title in TAB_TITLES:
            self.tabs.add(title)
            self.tabs.tab(title).columnconfigure(0, weight=1)
            self.tabs.tab(title).rowconfigure(0, weight=1)
        self.tabs.set("ZONES")  # the common field-correction surface lands first

        self._build_site_tab(self.tabs.tab("SITE"))

        self.zones = ZonesTab(self.tabs.tab("ZONES"), self.session.design,
                              self._on_zones_edit)
        self.zones.grid(row=0, column=0, sticky="nsew")

        for title, cls, attr in [("SPLITTERS", SplittersTab, "splitters_tab"),
                                 ("KEYPADS", KeypadsTab, "keypads_tab"),
                                 ("POWER", PowerTab, "power_tab")]:
            widget = cls(self.tabs.tab(title), self.session, self._on_design_edit)
            widget.grid(row=0, column=0, sticky="nsew")
            setattr(self, attr, widget)

        if self._on_generate_charts and self.session.source_kind == "xlsx":
            ctk.CTkButton(
                self, text="Generate door charts from this worksheet (as-is)",
                height=28, fg_color="transparent", text_color=ACCENT,
                hover_color=("gray90", "gray25"),
                command=self._on_generate_charts,
            ).grid(row=1, column=0, sticky="w", pady=(4, 0))

    def _on_zones_edit(self):
        sync_master_zones(self.session.design)
        self.mark_dirty()
        self.refresh_validation()

    def _on_design_edit(self):
        """Splitter/keypad/power edits: RSP locations feed master rows too."""
        sync_master_zones(self.session.design)
        self.mark_dirty()
        self.refresh_validation()

    # ------------------------------------------------------------------ #
    # Finalize gate                                                         #
    # ------------------------------------------------------------------ #

    def goto_issue(self, issue):
        """Jump to the tab (and zone row) an Issue points at."""
        if issue.tab in TAB_TITLES:
            self.tabs.set(issue.tab)
        ref = issue.ref or ""
        if ref.startswith("zone:") and hasattr(self, "zones"):
            with contextlib.suppress(ValueError):
                self.zones.select_zone(int(ref.split(":", 1)[1]))

    def show_finalize_dialog(self):
        issues = self.refresh_validation()
        errors = [i for i in issues if i.severity == "error"]
        warnings = [i for i in issues if i.severity == "warning"]
        design = self.session.design

        win = ctk.CTkToplevel(self.root)
        win.title("Finalize worksheet")
        win.geometry("620x520")
        win.transient(self.root)
        win.grab_set()

        if errors:
            head = f"{len(errors)} issue{'s' if len(errors) != 1 else ''} to resolve before FINAL"
            color = "#c05621"
        else:
            head = "All checks passed — ready to generate the FINAL worksheet"
            color = "#2f855a"
        ctk.CTkLabel(win, text=head, font=ctk.CTkFont(size=14, weight="bold"),
                     text_color=color).pack(anchor="w", padx=20, pady=(18, 8))

        body = ctk.CTkScrollableFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=4)
        body.columnconfigure(0, weight=1)
        auto_hide_scrollbar(body)

        row = 0
        by_tab: dict[str, list] = {}
        for issue in errors + warnings:
            by_tab.setdefault(issue.tab, []).append(issue)
        for tab_name in TAB_TITLES:
            tab_issues = by_tab.get(tab_name)
            if not tab_issues:
                continue
            ctk.CTkLabel(body, text=tab_name,
                         font=ctk.CTkFont(size=12, weight="bold"),
                         ).grid(row=row, column=0, sticky="w", padx=8, pady=(8, 2))
            row += 1
            for issue in tab_issues:
                line = ctk.CTkFrame(body, fg_color="transparent")
                line.grid(row=row, column=0, sticky="ew", padx=8)
                line.columnconfigure(1, weight=1)
                is_err = issue.severity == "error"
                ctk.CTkLabel(line, text="✗" if is_err else "⚠", width=20,
                             text_color="#c0392b" if is_err else "#c05621",
                             font=ctk.CTkFont(size=12, weight="bold"),
                             ).grid(row=0, column=0)
                ctk.CTkLabel(line, text=issue.message, anchor="w",
                             font=ctk.CTkFont(size=11), wraplength=420,
                             justify="left",
                             ).grid(row=0, column=1, sticky="w")

                def goto(i=issue):
                    win.grab_release()
                    win.destroy()
                    self.goto_issue(i)

                ctk.CTkButton(line, text="Go to", width=56, height=24,
                              fg_color="transparent", border_width=1,
                              border_color=ACCENT, text_color=ACCENT,
                              hover_color=("gray90", "gray25"), command=goto,
                              ).grid(row=0, column=2, padx=(6, 0), pady=1)
                row += 1

        if not errors:
            n_zones = len(design.zones)
            n_spares = sum(1 for z in design.zones
                           if (z.location or "").strip().upper() == "SPARE")
            summary = (f"{design.site_info.school_name or 'Unknown school'}\n"
                       f"{n_zones} zones ({n_spares} spare) · "
                       f"{len(design.rsps)} RSPs · {len(design.keypads)} keypads · "
                       f"{len(design.splitters)} splitters")
            ctk.CTkLabel(body, text=summary, font=ctk.CTkFont(size=12),
                         justify="left", anchor="w",
                         ).grid(row=row, column=0, sticky="w", padx=8, pady=12)

        btn_row = ctk.CTkFrame(win, fg_color="transparent")
        btn_row.pack(fill="x", padx=20, pady=(6, 18))
        btn_row.columnconfigure(0, weight=1)
        btn_row.columnconfigure(1, weight=1)

        def close():
            win.grab_release()
            win.destroy()

        ctk.CTkButton(btn_row, text="Back to editing", height=40,
                      fg_color="transparent", border_width=2, border_color="gray60",
                      text_color=("gray30", "gray80"),
                      hover_color=("gray90", "gray25"), command=close,
                      ).grid(row=0, column=0, sticky="ew", padx=(0, 6))

        def generate():
            win.grab_release()
            win.destroy()
            self._on_finalize()

        gen_btn = ctk.CTkButton(btn_row, text="Generate FINAL worksheet", height=40,
                                fg_color=ACCENT, hover_color=ACCENT_HOVER,
                                font=ctk.CTkFont(weight="bold"), command=generate)
        gen_btn.grid(row=0, column=1, sticky="ew", padx=(6, 0))
        if errors:
            gen_btn.configure(state="disabled")

    # ------------------------------------------------------------------ #
    # Validation                                                            #
    # ------------------------------------------------------------------ #

    def refresh_validation(self):
        issues = validate_design(self.session.design,
                                 topology_confirmed=self.session.topology_confirmed)
        counts = badge_counts(issues)
        if counts:
            text = "   ".join(f"{tab} ⚠{n}" for tab, n in counts.items())
        else:
            text = "✓ ready to finalize"
        self.on_validation_change(text, not counts)
        if hasattr(self, "zones"):
            error_zones = set()
            for issue in issues:
                if issue.severity == "error" and (issue.ref or "").startswith("zone:"):
                    error_zones.add(int(issue.ref.split(":", 1)[1]))
            self.zones.set_error_zones(error_zones)
        return issues

    # ------------------------------------------------------------------ #
    # SITE tab                                                              #
    # ------------------------------------------------------------------ #

    _SITE_FIELDS = [
        ("School name",       "school_name"),
        ("School code",       "school_code"),
        ("Main phone",        "phone"),
        ("Install tech name", "install_tech"),
        ("Install date",      "install_date"),
        ("IP address",        "ip_address"),
        ("Default gateway",   "default_gateway"),
        ("XR550 panel location", "xr550_location"),
    ]

    def _build_site_tab(self, tab):
        # The SITE form doesn't need the full height — a centered, fixed-width
        # block reads better than fields stretched across a wide window.
        holder = ctk.CTkFrame(tab, fg_color="transparent", width=560)
        holder.grid(row=0, column=0, pady=(8, 0))
        holder.columnconfigure(0, weight=1, minsize=270)
        holder.columnconfigure(1, weight=1, minsize=270)

        site = self.session.design.site_info
        self._suspend_traces = True
        for i, (label, attr) in enumerate(self._SITE_FIELDS):
            col, row = i % 2, i // 2
            cell = ctk.CTkFrame(holder, fg_color="transparent")
            cell.grid(row=row, column=col, sticky="ew",
                      padx=(0 if col == 0 else 8, 0), pady=4)
            cell.columnconfigure(0, weight=1)
            ctk.CTkLabel(cell, text=label, font=ctk.CTkFont(size=11),
                         text_color="gray50", anchor="w").grid(row=0, column=0, sticky="w")
            var = ctk.StringVar(value=getattr(site, attr, None) or "")
            var.trace_add("write", lambda *_a, a=attr, v=var: self._on_site_edit(a, v))
            entry = ctk.CTkEntry(cell, height=36, placeholder_text=label, textvariable=var)
            entry.grid(row=1, column=0, sticky="ew")
            self._site_vars[attr] = var
        self._suspend_traces = False

    def _on_site_edit(self, attr: str, var: ctk.StringVar):
        if self._suspend_traces:
            return
        setattr(self.session.design.site_info, attr, var.get().strip() or None)
        self.mark_dirty()
        self.refresh_validation()
        # School name doubles as the project title in the toolbar.
        if attr == "school_name":
            self._notify_status()

    def prefill_site_defaults(self, prefs: dict):
        """Fill empty site fields from per-machine prefs (phone, tech, …) the way
        the old job-details form did. Doesn't overwrite parsed values."""
        from datetime import date as _date
        defaults = {
            "phone": prefs.get("phone", ""),
            "install_tech": prefs.get("install_tech", ""),
            "install_date": prefs.get("install_date", _date.today().isoformat()),
            "ip_address": prefs.get("ip_address", ""),
            "default_gateway": prefs.get("default_gateway", ""),
        }
        self._suspend_traces = True
        site = self.session.design.site_info
        for attr, value in defaults.items():
            if value and not (getattr(site, attr, None) or "").strip():
                setattr(site, attr, value)
                self._site_vars[attr].set(value)
        self._suspend_traces = False
        self.refresh_validation()
