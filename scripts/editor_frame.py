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

import theme
from hardware import snapshot_refs, diff_refs
from session import Session, save_session, sync_master_zones, write_recovery, clear_recovery
from validation import validate_design, badge_counts, badge_counts_by_severity
from editor_zones import ZonesTab
from editor_tabs import (
    KeypadsTab,
    PowerTab,
    SplittersTab,
    auto_hide_scrollbar,
    prompt_add_expander,
)
from ui_widgets import (
    Card,
    Chip,
    ShortcutChip,
    add_hover,
    bind_click,
    primary_button,
    secondary_button,
)

RECOVERY_DEBOUNCE_MS = 5000

# Tab strip metrics from the handoff: 22px between tabs, 5px label→badge gap.
TAB_GAP = 22
BADGE_GAP = 5

# The pre-generate sheet is anchored over the editor, so it sizes itself.
SHEET_WIDTH = 460
# How many issue rows the pre-generate sheet shows before it starts scrolling,
# and the height one row occupies.
SHEET_VISIBLE_ROWS = 4
SHEET_ROW_HEIGHT = 46


def _format_install_date(d) -> str:
    """Today's date the way techs write it on the worksheet, e.g. 'JULY 21st 2026'.

    Matches the free-text style already used in the field (uppercase month, an
    ordinal day, four-digit year) so the prefilled default rarely needs editing."""
    n = d.day
    # 11th/12th/13th are the ordinal exceptions; otherwise key off the last digit.
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{d.strftime('%B').upper()} {n}{suffix} {d.year}"

TAB_TITLES = ["SITE", "ZONES", "SPLITTERS", "KEYPADS", "POWER"]


def _bind_click_tree(widget, command):
    """Make a composite widget clickable all the way through.

    ui_widgets.bind_click reaches a CTkFrame's *canvas*, which its child labels
    cover — so a click on the text of a chip or a tab would fall through. Walk
    the frames and bind each level; stop at CTkLabel, whose own bind() already
    covers both its canvas and its inner tk.Label.
    """
    bind_click(widget, command)
    if isinstance(widget, ctk.CTkFrame):
        for child in widget.winfo_children():
            _bind_click_tree(child, command)


def _bind_hover_tree(widget, on_enter, on_leave):
    """Enter/Leave over a composite widget, including its children."""
    widget.bind("<Enter>", on_enter, add="+")
    widget.bind("<Leave>", on_leave, add="+")
    if isinstance(widget, ctk.CTkFrame):
        for child in widget.winfo_children():
            _bind_hover_tree(child, on_enter, on_leave)


class TabBar(ctk.CTkFrame):
    """A tab strip over a page stack, with CTkTabview's API.

    CTkTabview drives its tabs from a segmented button, which can host neither
    the validation badges nor the underline treatment the handoff calls for.
    add() / tab() / set() / get() keep CTkTabview's contract exactly, so the
    call sites don't care which one they're talking to; set_badges() is the
    addition.
    """

    def __init__(self, master, **kw):
        kw.setdefault("fg_color", "transparent")
        kw.setdefault("corner_radius", 0)
        super().__init__(master, **kw)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        self._strip = ctk.CTkFrame(self, fg_color=theme.SURFACE, corner_radius=0,
                                   height=theme.HEIGHT["tabbar"])
        self._strip.grid(row=0, column=0, sticky="ew")
        self._strip.grid_propagate(False)
        self._strip.rowconfigure(0, weight=1)

        # width=1 on every childless frame: CTkFrame defaults to 200×200, and
        # with nothing inside to shrink it that default becomes its real size.
        ctk.CTkFrame(self, height=1, width=1, corner_radius=0,
                     fg_color=theme.BORDER,
                     ).grid(row=1, column=0, sticky="ew")

        self._body = ctk.CTkFrame(self, fg_color=theme.APP_BG, corner_radius=0)
        self._body.grid(row=2, column=0, sticky="nsew")
        self._body.columnconfigure(0, weight=1)
        self._body.rowconfigure(0, weight=1)

        self._order: list[str] = []
        self._pages: dict[str, ctk.CTkFrame] = {}
        self._items: dict[str, dict] = {}
        self._current: str | None = None

    # -- CTkTabview API -----------------------------------------------------

    def add(self, title: str) -> ctk.CTkFrame:
        page = ctk.CTkFrame(self._body, fg_color="transparent")
        page.grid(row=0, column=0, sticky="nsew")
        self._pages[title] = page

        col = len(self._order)
        item = ctk.CTkFrame(self._strip, fg_color="transparent")
        item.grid(row=0, column=col, sticky="ns",
                  padx=(theme.PAD["lg"] if col == 0 else TAB_GAP, 0))
        item.columnconfigure(0, weight=1)
        item.rowconfigure(0, weight=1)
        # The spacer column has to stay to the right of the newest tab, or the
        # strip centres its tabs instead of packing them left.
        self._strip.columnconfigure(col, weight=0)
        self._strip.columnconfigure(col + 1, weight=1)

        label_row = ctk.CTkFrame(item, fg_color="transparent")
        label_row.grid(row=0, column=0)
        bold = theme.ui_font(theme.SIZE["control"], "bold")
        # Reserve the bold width up front: the active tab is heavier than the
        # inactive one, and without a floor the whole strip shifts on select.
        label_row.columnconfigure(0, minsize=bold.measure(title))
        label = ctk.CTkLabel(label_row, text=title, text_color=theme.TEXT_SECOND,
                             font=theme.ui_font(theme.SIZE["control"]))
        label.grid(row=0, column=0)

        underline = ctk.CTkFrame(item, height=2, width=1, corner_radius=0,
                                 fg_color=theme.SURFACE)
        underline.grid(row=1, column=0, sticky="ew")

        self._items[title] = {"item": item, "row": label_row, "label": label,
                              "underline": underline, "badges": [],
                              "font_idle": label.cget("font"), "font_active": bold}
        self._order.append(title)
        _bind_click_tree(item, lambda t=title: self.set(t))
        add_hover(label, text_color=theme.TEXT)
        if self._current is None:
            self.set(title)
        return page

    def tab(self, title: str) -> ctk.CTkFrame:
        return self._pages[title]

    def set(self, title: str):
        if title not in self._pages:
            return
        self._current = title
        self._pages[title].tkraise()
        for name, item in self._items.items():
            active = name == title
            item["label"].configure(
                text_color=theme.TEXT if active else theme.TEXT_SECOND,
                font=item["font_active"] if active else item["font_idle"])
            item["underline"].configure(
                fg_color=theme.ACCENT if active else theme.SURFACE)

    def get(self) -> str:
        return self._current or ""

    # -- badges -------------------------------------------------------------

    def set_badges(self, counts: dict[str, dict[str, int]]):
        """Paint badge_counts_by_severity() into the tab labels."""
        for title, item in self._items.items():
            for badge in item["badges"]:
                badge.destroy()
            item["badges"] = []
            bucket = counts.get(title) or {}
            if bucket.get("error"):
                item["badges"].append(Chip(
                    item["row"], str(bucket["error"]), variant="error_solid",
                    size=theme.SIZE["badge"], pill=False, padx=5, pady=1))
            if bucket.get("warning"):
                item["badges"].append(Chip(
                    item["row"], "⚠", variant="warning",
                    size=theme.SIZE["badge"], pill=False, padx=5, pady=1))
            for col, badge in enumerate(item["badges"], start=1):
                badge.grid(row=0, column=col, padx=(BADGE_GAP, 0))
                _bind_click_tree(badge, lambda t=title: self.set(t))


class _PrimaryAction(ctk.CTkFrame):
    """The footer's primary button, as a frame so it can hold a child widget.

    A CTkButton is a single canvas item — there is nowhere to put the ⌘E chip
    the handoff draws inside the button. configure() accepts `text` and
    `state`, which is the whole surface set_generating() drives.
    """

    def __init__(self, master, text: str, shortcut_key: str, command, *,
                 height: int = theme.HEIGHT["button"]):
        super().__init__(master, fg_color=theme.ACCENT,
                         corner_radius=theme.RADIUS["button"])
        self._command = command
        self._state = "normal"
        self._hovering = False
        self.rowconfigure(0, weight=1, minsize=height)
        self.columnconfigure(0, weight=1)

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=0, column=0, padx=14)
        self._label = ctk.CTkLabel(
            row, text=text, text_color=theme.ON_ACCENT,
            font=theme.ui_font(theme.SIZE["control"], "bold"))
        self._label.grid(row=0, column=0)
        self._chip = ShortcutChip(row, shortcut_key)
        self._chip.grid(row=0, column=1, padx=(theme.PAD["sm"], 0))

        _bind_click_tree(self, self._invoke)
        _bind_hover_tree(self, self._on_enter, self._on_leave)

    def configure(self, require_redraw=False, **kwargs):
        if "text" in kwargs:
            self._label.configure(text=kwargs.pop("text"))
        if "state" in kwargs:
            self._set_state(kwargs.pop("state"))
        if kwargs or require_redraw:
            super().configure(require_redraw=require_redraw, **kwargs)

    def _set_state(self, state: str):
        self._state = state
        if state == "disabled":
            super().configure(fg_color=theme.BORDER_STRONG)
            self._label.configure(text_color=theme.TEXT_TERTIARY)
            self._chip.grid_remove()
        else:
            super().configure(fg_color=theme.ACCENT)
            self._label.configure(text_color=theme.ON_ACCENT)
            self._chip.grid()

    def _invoke(self):
        if self._state != "disabled":
            self._command()

    def _on_enter(self, _event=None):
        self._hovering = True
        if self._state != "disabled":
            super().configure(fg_color=theme.ACCENT_HOVER)

    def _on_leave(self, _event=None):
        # Crossing onto a child fires Leave then Enter; defer so that pair
        # doesn't flash the button back to its rest colour.
        self._hovering = False
        self.after(30, self._settle_hover)

    def _settle_hover(self):
        if self._hovering or not self.winfo_exists():
            return
        if self._state != "disabled":
            super().configure(fg_color=theme.ACCENT)


class EditorFrame(ctk.CTkFrame):
    """Tabbed editor over a Session. The app owns generation flows and chrome;
    this frame calls back into them via the callbacks passed at construction."""

    def __init__(self, master, root, session: Session, *,
                 on_generate_worksheet=None, on_generate_chart=None,
                 on_generate_remotelink=None,
                 on_status_change=None, on_validation_change=None):
        super().__init__(master, fg_color="transparent")
        self.root = root
        self.session = session
        self.dirty = False
        # Bumped on every edit; the app snapshots it after a worksheet generate
        # to warn when a door chart would be built from a stale worksheet.
        self.edit_epoch = 0
        self._recovery_job: str | None = None
        self._on_generate_worksheet = on_generate_worksheet or (lambda: None)
        self._on_generate_chart = on_generate_chart or (lambda: None)
        self._on_generate_remotelink = on_generate_remotelink or (lambda: None)
        self.on_status_change = on_status_change or (lambda text, dirty: None)
        self.on_validation_change = on_validation_change or (lambda text, ok: None)
        self._site_vars: dict[str, ctk.StringVar] = {}
        self._suspend_traces = False
        # The pre-generate sheet lives inside this frame now, so nothing stops
        # a second one opening on top of the first — this is the interlock.
        self._sheet: dict | None = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        # Closing the project with the sheet open would strand its Escape
        # binding on the root window.
        self.bind("<Destroy>", lambda _e: self._close_sheet(), add="+")
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
        self.edit_epoch += 1
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
        self._update_save_button()
        if self.dirty:
            self.on_status_change("●  Unsaved changes", True)
            return
        when = ""
        if self.session.saved_at:
            with contextlib.suppress(Exception):
                when = datetime.fromisoformat(self.session.saved_at).strftime("%-I:%M %p")
        self.on_status_change(f"Saved {when} ✓" if when else "Saved ✓", False)

    def _update_save_button(self):
        """Mirror the dirty state on the in-editor Save button: actionable when
        there's unsaved work, a quiet 'Saved ✓' confirmation when there isn't."""
        btn = getattr(self, "_save_btn", None)
        if btn is None:
            return
        if self.dirty:
            btn.configure(text="Save", state="normal", fg_color=theme.ACCENT,
                          hover_color=theme.ACCENT_HOVER,
                          text_color=theme.ON_ACCENT)
        else:
            btn.configure(text="Saved ✓", state="disabled",
                          fg_color=theme.SURFACE_CHIP,
                          text_color_disabled=theme.TEXT_TERTIARY)

    # ------------------------------------------------------------------ #
    # Tabs                                                                  #
    # ------------------------------------------------------------------ #

    def _build_tabs(self):
        self.tabs = TabBar(self)
        self.tabs.grid(row=0, column=0, sticky="nsew")
        for title in TAB_TITLES:
            self.tabs.add(title)
            self.tabs.tab(title).columnconfigure(0, weight=1)
            self.tabs.tab(title).rowconfigure(0, weight=1)
        self.tabs.set("ZONES")  # the common field-correction surface lands first

        self._build_site_tab(self.tabs.tab("SITE"))

        self.zones = ZonesTab(self.tabs.tab("ZONES"), self.session.design,
                              self._on_zones_edit,
                              on_add_expander=self._add_expander_clicked)
        self.zones.grid(row=0, column=0, sticky="nsew")

        for title, cls, attr in [("SPLITTERS", SplittersTab, "splitters_tab"),
                                 ("KEYPADS", KeypadsTab, "keypads_tab"),
                                 ("POWER", PowerTab, "power_tab")]:
            widget = cls(self.tabs.tab(title), self.session, self._on_design_edit,
                         on_structure_change=self._on_structure_change,
                         on_hardware_change=self.apply_hardware_change,
                         on_navigate=self.tabs.set)
            widget.grid(row=0, column=0, sticky="nsew")
            setattr(self, attr, widget)

        self._build_footer()

    def _build_footer(self):
        """Footer bar: save state and open-issue chips left, the three generate
        actions right. Generation runs in the background and never replaces the
        editor — outputs are revision-numbered artifacts you refresh at will."""
        # A 1px top rule, drawn as a border-coloured backing strip: CTkFrame
        # borders are all four sides or none.
        wrap = ctk.CTkFrame(self, fg_color=theme.BORDER, corner_radius=0)
        wrap.grid(row=1, column=0, sticky="ew")
        wrap.columnconfigure(0, weight=1)

        bar = ctk.CTkFrame(wrap, fg_color=theme.CHROME, corner_radius=0,
                           height=theme.HEIGHT["footer"])
        bar.grid(row=0, column=0, sticky="ew", pady=(1, 0))
        bar.grid_propagate(False)
        bar.columnconfigure(1, weight=1)
        bar.rowconfigure(0, weight=1)

        left = ctk.CTkFrame(bar, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w", padx=(theme.PAD["lg"], 0))

        # No save-state *text* here: the Save button below already carries the
        # state, and the header pill carries the timestamp. A third copy in the
        # footer just made the same fact shout three times.
        self._save_btn = ctk.CTkButton(
            left, text="Save", width=88, height=theme.HEIGHT["button_sm"],
            corner_radius=theme.RADIUS["button"], command=self.save,
            font=theme.ui_font(theme.SIZE["control"], "bold"),
            fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
            text_color=theme.ON_ACCENT)
        self._save_btn.pack(side="left", padx=(0, theme.PAD["sm"]))

        self._issue_chips = ctk.CTkFrame(left, fg_color="transparent",
                                         width=1, height=1)
        self._issue_chips.pack(side="left")

        right = ctk.CTkFrame(bar, fg_color="transparent")
        right.grid(row=0, column=2, sticky="e", padx=(0, theme.PAD["lg"]))
        self._gen_chart_btn = secondary_button(
            right, "Generate Door Chart", self._on_generate_chart)
        self._gen_chart_btn.pack(side="left", padx=(0, theme.PAD["sm"]))
        self._gen_rl_btn = secondary_button(
            right, "RemoteLink Account", self._on_generate_remotelink)
        self._gen_rl_btn.pack(side="left", padx=(0, theme.PAD["sm"]))
        self._gen_ws_btn = _PrimaryAction(
            right, "Generate Worksheet", "E", self._on_generate_worksheet)
        self._gen_ws_btn.pack(side="left")

        self._update_save_button()

    def set_generating(self, which: str | None):
        """Reflect a running generation on the buttons: `which` is
        'worksheet', 'chart', 'remotelink', or None when idle. All disable while
        one runs (they share the design and the output pipeline)."""
        running = which is not None
        self._gen_ws_btn.configure(
            text="Generating…" if which == "worksheet" else "Generate Worksheet",
            state="disabled" if running else "normal")
        self._gen_chart_btn.configure(
            text="Generating…" if which == "chart" else "Generate Door Chart",
            state="disabled" if running else "normal")
        self._gen_rl_btn.configure(
            text="Generating…" if which == "remotelink"
                 else "RemoteLink Account",
            state="disabled" if running else "normal")

    def _on_zones_edit(self):
        sync_master_zones(self.session.design)
        self.mark_dirty()
        self.refresh_validation()

    def _on_design_edit(self):
        """Splitter/keypad/power edits: RSP locations feed master rows too."""
        sync_master_zones(self.session.design)
        self.mark_dirty()
        self.refresh_validation()

    def _on_structure_change(self):
        """Hardware was added or removed: every tab's choices and rows shift."""
        sync_master_zones(self.session.design)
        self.mark_dirty()
        self.refresh_validation()
        self.refresh_all_tabs()

    def apply_hardware_change(self, mutate):
        """Run a removal that may cascade, then surface what it rewired.

        remove_* heals dangling references in place (outputs → Spare, keypad
        sources → blank). That's silent today, so a tech can ship wiring they
        never reviewed. We snapshot around the mutation, and when it changed
        anything we mark the topology unreviewed again and route the tech to
        the affected cards — never auto-rewiring on their behalf.
        """
        before = snapshot_refs(self.session.design)
        mutate()
        changes = diff_refs(before, snapshot_refs(self.session.design))
        if changes:
            self.session.topology_confirmed = False
        sync_master_zones(self.session.design)
        self.mark_dirty()
        self.refresh_validation()
        self.refresh_all_tabs()  # rebuilds SPLITTERS header → re-reads reset flag
        if changes:
            self._report_structure_change(changes)

    def _report_structure_change(self, changes):
        """Modal summary of cascade fallout with per-tab 'Go to' routing.

        Still an OS window: this interrupts a destructive edit the tech didn't
        ask for, so it should survive whatever they click next.
        """
        win = ctk.CTkToplevel(self.root)
        win.title("Wiring updated")
        win.geometry("560x360")
        win.configure(fg_color=theme.APP_BG)
        win.transient(self.root)
        win.grab_set()

        n = len(changes)
        ctk.CTkLabel(
            win,
            text=f"{n} connection{'s' if n != 1 else ''} changed — review before generating",
            font=theme.ui_font(theme.SIZE["title"], "bold"),
            text_color=theme.WARNING,
        ).pack(anchor="w", padx=20, pady=(18, 2))
        ctk.CTkLabel(
            win, text="Removing that hardware left these set to Spare or unsourced. "
            "“Wiring reviewed” was unchecked so you can confirm the new topology.",
            wraplength=520, justify="left", text_color=theme.TEXT_SECOND,
            font=theme.ui_font(theme.SIZE["chip"]),
        ).pack(anchor="w", padx=20, pady=(0, 8))

        body = ctk.CTkScrollableFrame(win, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=12, pady=4)
        body.columnconfigure(0, weight=1)
        auto_hide_scrollbar(body)
        for r, ch in enumerate(changes):
            ctk.CTkLabel(body, text=f"•  {ch.message}", anchor="w",
                         justify="left", wraplength=520,
                         text_color=theme.TEXT,
                         font=theme.ui_font(theme.SIZE["chip"])).grid(
                row=r, column=0, sticky="w", padx=8, pady=2)

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(fill="x", padx=20, pady=(4, 16))
        affected_tabs = [t for t in ("SPLITTERS", "KEYPADS")
                         if any(c.tab == t for c in changes)]
        for tab_name in affected_tabs:
            secondary_button(
                btns, f"Review {tab_name}",
                lambda t=tab_name: (self.tabs.set(t), win.grab_release(),
                                    win.destroy()),
                height=theme.HEIGHT["input"],
            ).pack(side="left", padx=(0, theme.PAD["sm"]))
        primary_button(btns, "Dismiss",
                       lambda: (win.grab_release(), win.destroy()),
                       height=theme.HEIGHT["input"]).pack(side="right")

    def _add_expander_clicked(self):
        prompt_add_expander(self.root, self.session, self._on_structure_change)

    def refresh_all_tabs(self):
        """Rebuild every tab from the (mutated) design lists."""
        self.zones.refresh()
        self.splitters_tab.refresh()
        self.keypads_tab.refresh()
        self.power_tab.refresh()

    # ------------------------------------------------------------------ #
    # Pre-generate issue summary (warn, never block)                        #
    # ------------------------------------------------------------------ #

    def goto_issue(self, issue):
        """Jump to the tab (and zone row) an Issue points at."""
        if issue.tab in TAB_TITLES:
            self.tabs.set(issue.tab)
        ref = issue.ref or ""
        if ref.startswith("zone:") and hasattr(self, "zones"):
            with contextlib.suppress(ValueError):
                self.zones.select_zone(int(ref.split(":", 1)[1]))

    def show_issues_dialog(self, on_proceed, *, proceed_label: str,
                           note: str | None = None):
        """Run `on_proceed` immediately when the design is clean; otherwise
        summarize the open issues and let the tech choose "generate anyway"
        or jump to a problem. Generation is never blocked — the printed sheet
        is itself a review pass with the superintendent."""
        issues = self.refresh_validation()
        if not issues and not note:
            on_proceed()
            return
        if self._sheet is not None:
            self._sheet["card"].lift()
            return

        # The scrim dims the tab area only: the sheet is anchored to the footer
        # it belongs to, so covering the footer would leave it pointing at
        # nothing. Tk has no alpha, so it's an opaque slate rather than a wash.
        scrim = ctk.CTkFrame(self.tabs, fg_color=theme.SCRIM,
                             corner_radius=0)
        scrim.place(x=0, y=0, relwidth=1, relheight=1)
        scrim.lift()

        card = Card(self, corner_radius=theme.RADIUS["card"] + 2)
        # place() refuses width/height, and a configured width loses to grid
        # propagation — a column floor is the way to size the sheet.
        card.columnconfigure(0, weight=1, minsize=SHEET_WIDTH)
        card.place(relx=1.0, rely=1.0, anchor="se", x=-theme.PAD["lg"],
                   y=-(theme.HEIGHT["footer"] + theme.PAD["sm"] + 1))
        card.lift()

        escape_id = self.root.bind("<Escape>", lambda _e: self._close_sheet(),
                                   add="+")
        self._sheet = {"scrim": scrim, "card": card, "escape_id": escape_id}
        scrim.bind("<Button-1>", lambda _e: self._close_sheet())

        n = len(issues)
        head = (f"{n} open issue{'s' if n != 1 else ''}" if issues
                else "Heads up before generating")
        ctk.CTkLabel(card, text=head, anchor="w", text_color=theme.TEXT,
                     font=theme.ui_font(theme.SIZE["title"], "bold"),
                     ).grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 0))
        ctk.CTkLabel(
            card, anchor="w", justify="left", wraplength=410,
            text="Generation is never blocked — fix now or print and mark up "
                 "with the super.",
            text_color=theme.TEXT_SECOND, font=theme.ui_font(theme.SIZE["chip"]),
        ).grid(row=1, column=0, sticky="ew", padx=18, pady=(2, 10))

        row = 2
        if note:
            banner = Card(card, fg_color=theme.WARNING_ROW,
                          border_color=theme.BANNER_BORDER, corner_radius=8)
            banner.grid(row=row, column=0, sticky="ew", padx=18, pady=(0, 8))
            banner.columnconfigure(0, weight=1)
            ctk.CTkLabel(banner, text=f"⚠  {note}", anchor="w", justify="left",
                         wraplength=380, text_color=theme.BANNER_TEXT,
                         font=theme.ui_font(theme.SIZE["chip"], "bold"),
                         ).grid(row=0, column=0, sticky="ew", padx=10, pady=8)
            row += 1

        if issues:
            # Four rows fit above the fold; past that the sheet scrolls rather
            # than growing past the top of the editor. Short lists get a plain
            # frame: a scroll container here would reserve its full height and
            # park a scrollbar beside content that already fits.
            #
            # The scrolling branch deliberately does NOT get auto_hide_scrollbar.
            # With a fixed height inside a place()d card, hiding the scrollbar
            # changes the canvas width, which re-fires the geometry callback
            # that decided to hide it — CTkScrollbar.set() → _draw() →
            # update_idletasks() → set() then spins forever and wedges the app.
            if len(issues) <= SHEET_VISIBLE_ROWS:
                body = ctk.CTkFrame(card, fg_color="transparent")
            else:
                body = ctk.CTkScrollableFrame(
                    card, fg_color="transparent",
                    height=SHEET_VISIBLE_ROWS * SHEET_ROW_HEIGHT)
            body.grid(row=row, column=0, sticky="ew", padx=12, pady=(0, 4))
            body.columnconfigure(0, weight=1)
            for i, issue in enumerate(issues):
                self._build_issue_row(body, issue).grid(
                    row=i, column=0, sticky="ew", padx=6, pady=3)
            row += 1

        footer = ctk.CTkFrame(card, fg_color="transparent")
        footer.grid(row=row, column=0, sticky="ew", padx=18, pady=(8, 16))
        footer.columnconfigure(0, weight=1, uniform="sheet")
        footer.columnconfigure(1, weight=1, uniform="sheet")

        def proceed():
            self._close_sheet()
            on_proceed()

        secondary_button(footer, "Review first", self._close_sheet, height=36,
                         ).grid(row=0, column=0, sticky="ew", padx=(0, 6))
        primary_button(footer, proceed_label, proceed, height=36,
                       ).grid(row=0, column=1, sticky="ew", padx=(6, 0))

    def _build_issue_row(self, parent, issue):
        is_err = issue.severity == "error"
        row = Card(parent, corner_radius=8,
                   fg_color=theme.ERROR_TINT if is_err else theme.WARNING_ROW,
                   border_color=theme.ERROR_BORDER if is_err
                   else theme.BANNER_BORDER)
        row.columnconfigure(1, weight=1)

        dot = ctk.CTkFrame(row, width=7, height=7, corner_radius=4,
                           fg_color=theme.ERROR if is_err else theme.WARNING)
        dot.grid(row=0, column=0, padx=(10, 8), pady=8)
        dot.grid_propagate(False)

        ctk.CTkLabel(row, text=issue.message, anchor="w", justify="left",
                     wraplength=250, text_color=theme.TEXT,
                     font=theme.ui_font(theme.SIZE["chip"], "bold"),
                     ).grid(row=0, column=1, sticky="w", pady=8)
        ctk.CTkLabel(row, text=issue.tab, text_color=theme.TEXT_TERTIARY,
                     font=theme.ui_font(theme.SIZE["label"]),
                     ).grid(row=0, column=2, padx=8, pady=8)

        goto = ctk.CTkLabel(row, text="Go to →", text_color=theme.ACCENT,
                            font=theme.ui_font(theme.SIZE["chip"], "bold"))
        goto.grid(row=0, column=3, padx=(0, 10), pady=8)
        bind_click(goto, lambda i=issue: (self._close_sheet(), self.goto_issue(i)))
        add_hover(goto, text_color=theme.ACCENT_HOVER)
        return row

    def _close_sheet(self):
        """Dismiss the pre-generate sheet — Escape, the scrim, "Review first"."""
        sheet, self._sheet = self._sheet, None
        if sheet is None:
            return
        with contextlib.suppress(Exception):
            self.root.unbind("<Escape>", sheet["escape_id"])
        for key in ("card", "scrim"):
            with contextlib.suppress(Exception):
                sheet[key].destroy()

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
            text = "✓ no issues"
        self.on_validation_change(text, not counts)
        if hasattr(self, "tabs"):
            self.tabs.set_badges(badge_counts_by_severity(issues))
        self._refresh_issue_chips(issues)
        if hasattr(self, "zones"):
            error_zones = set()
            for issue in issues:
                if issue.severity == "error" and (issue.ref or "").startswith("zone:"):
                    error_zones.add(int(issue.ref.split(":", 1)[1]))
            self.zones.set_error_zones(error_zones)
        return issues

    def _refresh_issue_chips(self, issues):
        """One footer chip per tab with open issues; clicking jumps to it."""
        holder = getattr(self, "_issue_chips", None)
        if holder is None:
            return
        for widget in holder.winfo_children():
            widget.destroy()
        # A CTkFrame keeps the size its children gave it even after they're
        # gone, so reset it — otherwise a cleared issue leaves a hole here.
        holder.configure(width=1, height=1)
        holder.pack_forget()
        by_tab: dict[str, list] = {}
        for issue in issues:
            by_tab.setdefault(issue.tab, []).append(issue)
        for tab_name in TAB_TITLES:
            tab_issues = by_tab.get(tab_name)
            if not tab_issues:
                continue
            # "SPLITTERS ⚠1" reads as a wiring fault; the unreviewed flag is a
            # to-do, so it says so.
            if (len(tab_issues) == 1
                    and tab_issues[0].code == "topology.unconfirmed"):
                text = "Wiring unreviewed"
            else:
                text = f"{tab_name} ⚠{len(tab_issues)}"
            if not holder.winfo_manager():
                holder.pack(side="left")
            chip = Chip(holder, text, variant="warning",
                        size=theme.SIZE["meta"], padx=10, pady=3)
            chip.pack(side="left", padx=(0, 6))
            add_hover(chip, border_color=theme.ACCENT)
            _bind_click_tree(chip, lambda t=tab_name: self.tabs.set(t))

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
        holder.grid(row=0, column=0, pady=(theme.PAD["lg"], 0))
        holder.columnconfigure(0, weight=1, minsize=270)
        holder.columnconfigure(1, weight=1, minsize=270)

        site = self.session.design.site_info
        self._suspend_traces = True
        for i, (label, attr) in enumerate(self._SITE_FIELDS):
            col, row = i % 2, i // 2
            cell = ctk.CTkFrame(holder, fg_color="transparent")
            cell.grid(row=row, column=col, sticky="ew",
                      padx=(0 if col == 0 else theme.PAD["sm"], 0),
                      pady=theme.PAD["xs"])
            cell.columnconfigure(0, weight=1)
            ctk.CTkLabel(cell, text=label, anchor="w",
                         font=theme.ui_font(theme.SIZE["label"]),
                         text_color=theme.TEXT_TERTIARY,
                         ).grid(row=0, column=0, sticky="w", pady=(0, 2))
            var = ctk.StringVar(value=getattr(site, attr, None) or "")
            var.trace_add("write", lambda *_a, a=attr, v=var: self._on_site_edit(a, v))
            entry = ctk.CTkEntry(
                cell, height=theme.HEIGHT["input"], placeholder_text=label,
                textvariable=var, fg_color=theme.SURFACE,
                border_color=theme.BORDER_STRONG, border_width=1,
                corner_radius=theme.RADIUS["button"], text_color=theme.TEXT,
                placeholder_text_color=theme.TEXT_TERTIARY,
                font=theme.ui_font(theme.SIZE["body"]))
            entry.grid(row=1, column=0, sticky="ew")
            add_hover(entry, border_color=theme.ACCENT)
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
        """Fill empty site fields from per-machine prefs (tech, IP, …) the way
        the old job-details form did. Doesn't overwrite parsed values.

        Phone is intentionally not prefilled from prefs: it's school-specific
        (auto-looked-up per site from the bundled directory), so a prefs value
        would be a stale number from a previous project.

        Install date is likewise NOT remembered across projects: unlike the
        machine-stable tech/IP/gateway, a date carried forward is always stale
        (it produced yesterday's date on today's job). It defaults to today,
        formatted the way techs write it, and stays fully editable."""
        from datetime import date as _date
        defaults = {
            "install_tech": prefs.get("install_tech", ""),
            "install_date": _format_install_date(_date.today()),
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
