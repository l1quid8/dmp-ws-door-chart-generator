"""ZONES tab — searchable, inline-editable grid over design.zones.

ttk.Treeview (native, virtualized — fine at 480 rows) with a single reusable
overlay Entry/Combobox for cell editing; no extra dependency to ship in the
exe. Each commit mutates the backing ZoneInfo and notifies the editor, which
re-syncs master_zones and refreshes validation.

Two of the handoff's grid decorations can't be built with a Treeview: a cell
holds a string, never a widget, so the "FILL IN" tag and the outline chip
around the device type have no widget to be. They are approximated as text
conventions (see _values_for) with the row tint carrying the real signal.

ttk widgets also ignore CustomTkinter's appearance mode entirely, so every
colour here goes through theme.resolve() and the whole style block is re-run
from a bind_mode_change subscription.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import customtkinter as ctk

import theme
from parse_dmp_worksheet import DMPDesign, ZoneInfo
from ui_widgets import Card, Chip, accent_outline_button, attach_tooltip

DEVICE_TYPES = ["Motion", "Door Contact", "Glass Break", "Panic",
                "Supervisory", "Spare"]

COLUMNS = ("zone", "description", "device_type", "partition", "expander")
HEADINGS = {"zone": "ZONE", "description": "DESCRIPTION",
            "device_type": "DEVICE TYPE", "partition": "PART#",
            "expander": "EXP#"}
# The spec allots PART# 60 and EXP# 56, but a ttk heading reserves ~10px of
# internal padding on top of the text, and the letter-spaced label needs 58/45.
# Widening the two numeric columns beats clipping their headers; DESCRIPTION is
# the flex column, so it gives up the difference.
WIDTHS = {"zone": 70, "description": 260, "device_type": 150,
          "partition": 72, "expander": 62}
EDITABLE = {"description", "device_type", "partition"}

FILTERS = ["All", "Needs attention", "Spares", "Errors"]

# The "FILL IN" tag the handoff draws inside attention rows. A Treeview cell is
# text-only, so it is appended to the description *for display* and stripped
# again before anything reaches the model — see _undecorate.
FILL_IN = "FILL IN"
FILL_IN_SUFFIX = "   ·  " + FILL_IN


class ZonesTab(ctk.CTkFrame):
    def __init__(self, master, design: DMPDesign, on_change, on_add_expander=None):
        super().__init__(master, fg_color="transparent")
        self.design = design
        self.on_change = on_change  # called after any committed edit
        self.on_add_expander = on_add_expander  # opens the add-expander dialog
        self._error_zones: set[int] = set()
        self._edit_widget: tk.Widget | None = None
        self._edit_item: str | None = None
        self._edit_col: str | None = None
        self._filter_mode = "All"
        self._hover_iid: str | None = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self._build_toolbar()
        self._build_tree()
        self.refresh()

    # ------------------------------------------------------------------ #
    # Toolbar: search + filter chips                                        #
    # ------------------------------------------------------------------ #

    def _build_toolbar(self):
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", pady=(0, theme.PAD["md"]))
        bar.columnconfigure(2, weight=1)

        # CTkEntry has no leading-icon slot, so the field is a bordered frame
        # holding the magnifier glyph next to a chrome-less entry. The border
        # and radius live on the frame; the entry itself draws nothing.
        box = ctk.CTkFrame(
            bar, width=280, height=theme.HEIGHT["input"], fg_color=theme.SURFACE,
            border_width=1, border_color=theme.BORDER_STRONG,
            corner_radius=theme.RADIUS["button"])
        box.grid(row=0, column=0, sticky="w", padx=(0, theme.PAD["sm"]))
        box.grid_propagate(False)
        box.columnconfigure(1, weight=1)
        box.rowconfigure(0, weight=1)
        ctk.CTkLabel(box, text="⌕", text_color=theme.TEXT_TERTIARY,
                     font=theme.ui_font(16)).grid(row=0, column=0, padx=(9, 0))

        # No textvariable: CustomTkinter 5.2.2 suppresses the placeholder for
        # any entry that has one (ctk_entry._activate_placeholder compares the
        # Variable object to ""), and the placeholder is part of the spec.
        self._search = ctk.CTkEntry(
            box, border_width=0, fg_color="transparent",
            height=theme.HEIGHT["input"] - 4,
            font=theme.ui_font(theme.SIZE["control"]),
            text_color=theme.TEXT, placeholder_text_color=theme.TEXT_TERTIARY,
            placeholder_text="Search zone # or description…")
        self._search.grid(row=0, column=1, sticky="ew", padx=(2, 8))
        self._search.bind("<KeyRelease>", lambda _e: self._apply_filter())

        chips = ctk.CTkFrame(bar, fg_color="transparent")
        chips.grid(row=0, column=1, sticky="w")
        self._filter_chips: dict[str, Chip] = {}
        for col, mode in enumerate(FILTERS):
            chip = Chip(chips, mode, variant="outline", size=theme.SIZE["chip"],
                        padx=11, pady=4,
                        on_click=lambda m=mode: self._select_filter(m))
            chip.grid(row=0, column=col, padx=(0 if col == 0 else 6, 0))
            self._filter_chips[mode] = chip
        # The chips replaced a single segmented button; the help now hangs off
        # their shared container so it still surfaces from any of them.
        attach_tooltip(
            chips,
            "Filter the zone list:\n"
            "• All — every zone\n"
            "• Needs attention — blank or “NEW” descriptions to fill in\n"
            "• Spares — unused points marked SPARE\n"
            "• Errors — zones that fail a finalize check")
        self._paint_chips()

        if self.on_add_expander:
            accent_outline_button(
                bar, "+ Add Expander", self.on_add_expander,
                height=theme.HEIGHT["button_md"], width=130,
            ).grid(row=0, column=3, padx=(theme.PAD["sm"], 0))

    def _select_filter(self, mode: str):
        self._filter_mode = mode
        self._paint_chips()
        self._apply_filter()

    def _paint_chips(self):
        for mode, chip in self._filter_chips.items():
            chip.set_variant("accent" if mode == self._filter_mode else "outline")

    def _update_counts(self):
        """Recount from _row_passes, the same predicate that hides the rows —
        a chip's number and its filtered row count therefore can't disagree."""
        for mode, chip in self._filter_chips.items():
            hits = sum(1 for zi in self.design.zones
                       if self._row_passes(zi, "", mode))
            chip.set_text(f"{mode} · {hits}")

    # ------------------------------------------------------------------ #
    # Tree                                                                  #
    # ------------------------------------------------------------------ #

    def _build_tree(self):
        # A Treeview can't have rounded corners, so the Card supplies the
        # border and radius and the tree sits 1px inside it.
        card = Card(self)
        card.grid(row=1, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(0, weight=1)

        style = ttk.Style(card)
        # 'clam' is the only stock theme that honors fieldbackground/rowheight.
        style.theme_use("clam" if "clam" in style.theme_names() else style.theme_use())
        # One font for the whole tree: ttk can't give the zone column its own
        # monospace face, so the mono treatment the spec asks for on zone ids
        # is dropped rather than applied to every cell.
        self._ui_family = theme.ui_font().cget("family")

        # height is a minimum request; the sticky+weight grid stretches the
        # tree to fill whatever the window gives it.
        self.tree = ttk.Treeview(
            card, columns=COLUMNS, show="headings",
            style="Zones.Treeview", selectmode="browse", height=12,
        )
        for col in COLUMNS:
            anchor = "w" if col in ("description", "device_type") else "center"
            self.tree.heading(col, text=theme.tracked(HEADINGS[col]), anchor=anchor)
            # Description is the only flexible column; the rest hold the
            # handoff's fixed widths at every window size.
            self.tree.column(col, width=WIDTHS[col], anchor=anchor,
                             minwidth=160 if col == "description" else WIDTHS[col],
                             stretch=(col == "description"))

        vsb = ctk.CTkScrollbar(card, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(1, 0), pady=1)
        vsb.grid(row=0, column=1, sticky="ns", padx=(2, 4), pady=6)

        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Return>", self._on_return)
        self.tree.bind("<F2>", self._on_return)
        self.tree.bind("<Motion>", self._on_motion)
        self.tree.bind("<Leave>", self._on_leave)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._retag_hover_row())

        self._apply_tree_style()
        theme.bind_mode_change(self, self._apply_tree_style)

    def _apply_tree_style(self, _mode: str | None = None):
        """(Re)paint every raw-Tk surface in the tab for the active mode."""
        style = ttk.Style(self)
        surface = theme.resolve(theme.SURFACE)
        text = theme.resolve(theme.TEXT)

        style.configure(
            "Zones.Treeview", rowheight=theme.HEIGHT["row"],
            font=(self._ui_family, theme.SIZE["body"]),
            background=surface, fieldbackground=surface, foreground=text,
            borderwidth=0, relief="flat")
        style.configure(
            "Zones.Treeview.Heading",
            font=(self._ui_family, theme.SIZE["label"], "bold"),
            background=theme.resolve(theme.SURFACE_SUBTLE),
            foreground=theme.resolve(theme.TEXT_TERTIARY),
            borderwidth=0, relief="flat", padding=(6, 7))
        style.map("Zones.Treeview",
                  background=[("selected", theme.resolve(theme.ACCENT))],
                  foreground=[("selected", theme.resolve(theme.ON_ACCENT))])
        # Without this the heading flips to the stock grey under the cursor.
        style.map("Zones.Treeview.Heading",
                  background=[("active", theme.resolve(theme.SURFACE_SUBTLE))],
                  relief=[("active", "flat"), ("pressed", "flat")])

        self.tree.tag_configure("error", background=theme.resolve(theme.ERROR_TINT))
        self.tree.tag_configure("attention", background=theme.resolve(theme.WARNING_ROW))
        self.tree.tag_configure("hover", background=theme.resolve(theme.ROW_HOVER))

        # The inline-edit overlay is ttk too, so it needs the same treatment.
        for name in ("Zones.TEntry", "Zones.TCombobox"):
            style.configure(
                name, fieldbackground=surface, background=surface,
                foreground=text, insertcolor=text, arrowcolor=text,
                bordercolor=theme.resolve(theme.ACCENT),
                lightcolor=theme.resolve(theme.ACCENT),
                darkcolor=theme.resolve(theme.ACCENT),
                selectbackground=theme.resolve(theme.ACCENT),
                selectforeground=theme.resolve(theme.ON_ACCENT),
                padding=(4, 2))
        style.map("Zones.TCombobox",
                  fieldbackground=[("readonly", surface)],
                  foreground=[("readonly", text)])
        if self._edit_widget is not None:
            # A live overlay keeps its own colours until it is re-created.
            self._cancel_edit()

    # ------------------------------------------------------------------ #
    # Data <-> rows                                                         #
    # ------------------------------------------------------------------ #

    def _zone_to_expander(self) -> dict[int, int]:
        return {z: r.number for r in self.design.rsps for z in r.zones}

    def _needs_attention(self, zi: ZoneInfo) -> bool:
        """Exactly the rows the "Needs attention" filter matches — same
        predicate, so the tint, the chip count and the filter never diverge."""
        return self._row_passes(zi, "", "Needs attention")

    def _values_for(self, zi: ZoneInfo, exp_map: dict[int, int]) -> tuple:
        """Display values only. Never write these back into a ZoneInfo: the
        description carries the FILL_IN decoration the spec's inline tag stands
        in for, and the device type is plain text where the spec draws a chip.
        """
        desc = zi.location or ""
        if self._needs_attention(zi):
            desc = f"{desc}{FILL_IN_SUFFIX}" if desc else FILL_IN
        return (f"Z{zi.number}", desc, zi.device_type or "",
                zi.partition if zi.partition is not None else "",
                exp_map.get(zi.number, ""))

    @staticmethod
    def _undecorate(value: str) -> str:
        """Strip the FILL_IN display tag. Belt-and-braces: _begin_edit already
        seeds description edits from the model, so this only fires if a
        decorated string is pasted back in by hand."""
        if value.endswith(FILL_IN_SUFFIX):
            value = value[: -len(FILL_IN_SUFFIX)]
        elif value.strip() == FILL_IN:
            value = ""
        return value.strip()

    def refresh(self):
        """Rebuild all rows from design.zones (item id == zone number).

        Deletion must go through the tracked iid list — rows hidden by the
        search/filter are *detached*, so tree.get_children() misses them and
        a rebuild would collide on re-insert ("Item already exists").
        """
        self._cancel_edit()
        for iid in getattr(self, "_all_iids", []):
            if self.tree.exists(iid):
                self.tree.delete(iid)
        self._all_iids: list[str] = []
        self._hover_iid = None
        exp_map = self._zone_to_expander()
        for zi in sorted(self.design.zones, key=lambda z: z.number):
            iid = str(zi.number)
            self.tree.insert("", "end", iid=iid,
                             values=self._values_for(zi, exp_map))
            self._all_iids.append(iid)
        self._retag_rows()
        self._apply_filter()

    def _zone_info(self, number: int) -> ZoneInfo | None:
        for zi in self.design.zones:
            if zi.number == number:
                return zi
        return None

    def set_error_zones(self, zone_numbers: set[int]):
        self._error_zones = zone_numbers
        self._retag_rows()
        self._update_counts()
        # Only the Errors view can go stale on a validation refresh, and
        # re-filtering cancels any open edit — so pay that cost only there.
        if self._filter_mode == "Errors":
            self._apply_filter()

    def _row_tags(self, iid: str, zi: ZoneInfo | None) -> tuple:
        """One tag per row, by precedence: errors win over attention, and hover
        never displaces either. A single tag sidesteps ttk's undocumented
        multi-tag resolution order, which differs across Tk patch releases."""
        if int(iid) in self._error_zones:
            return ("error",)
        if zi is not None and self._needs_attention(zi):
            return ("attention",)
        if iid == self._hover_iid and iid not in self.tree.selection():
            return ("hover",)
        return ()

    def _retag_rows(self):
        # Walk the tracked iids, not get_children — detached (filtered-out)
        # rows must keep their tags current too.
        by_number = {zi.number: zi for zi in self.design.zones}
        for iid in getattr(self, "_all_iids", []):
            if self.tree.exists(iid):
                self.tree.item(iid, tags=self._row_tags(iid, by_number.get(int(iid))))

    def _retag_row(self, iid: str, zi: ZoneInfo | None = None):
        if self.tree.exists(iid):
            self.tree.item(iid, tags=self._row_tags(iid, zi or self._zone_info(int(iid))))

    def select_zone(self, number: int):
        """Scroll to and select a zone row (used by 'Go to' in the finalize gate)."""
        iid = str(number)
        if self.tree.exists(iid):
            self._search.delete(0, "end")
            self._select_filter("All")
            self.tree.see(iid)
            self.tree.selection_set(iid)

    # ------------------------------------------------------------------ #
    # Hover tint                                                            #
    # ------------------------------------------------------------------ #

    def _on_motion(self, event):
        iid = self.tree.identify_row(event.y)
        if iid == self._hover_iid:
            return
        previous, self._hover_iid = self._hover_iid, iid or None
        for target in (previous, self._hover_iid):
            if target:
                self._retag_row(target)

    def _on_leave(self, _event=None):
        if self._hover_iid:
            stale, self._hover_iid = self._hover_iid, None
            self._retag_row(stale)

    def _retag_hover_row(self):
        # Selection just changed; the hovered row may have gained or lost the
        # selected state, which outranks the hover tint.
        if self._hover_iid:
            self._retag_row(self._hover_iid)

    # ------------------------------------------------------------------ #
    # Search / filter                                                       #
    # ------------------------------------------------------------------ #

    def _row_passes(self, zi: ZoneInfo, needle: str, mode: str) -> bool:
        if mode == "Needs attention":
            desc = (zi.location or "").strip()
            if desc and desc.upper() != "NEW":
                return False
        elif mode == "Spares":
            if (zi.location or "").strip().upper() != "SPARE":
                return False
        elif mode == "Errors":
            if zi.number not in self._error_zones:
                return False
        if needle:
            hay = f"z{zi.number} {zi.location or ''}".lower()
            if needle not in hay:
                return False
        return True

    def _apply_filter(self):
        self._cancel_edit()
        self._update_counts()
        needle = self._search.get().strip().lower()
        mode = self._filter_mode
        visible_index = 0
        for zi in sorted(self.design.zones, key=lambda z: z.number):
            iid = str(zi.number)
            if not self.tree.exists(iid):
                continue
            if self._row_passes(zi, needle, mode):
                self.tree.reattach(iid, "", visible_index)
                visible_index += 1
            else:
                self.tree.detach(iid)

    # ------------------------------------------------------------------ #
    # Inline editing                                                        #
    # ------------------------------------------------------------------ #

    def _on_double_click(self, event):
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        iid = self.tree.identify_row(event.y)
        col_id = self.tree.identify_column(event.x)
        if iid and col_id:
            col = COLUMNS[int(col_id[1:]) - 1]
            if col in EDITABLE:
                self._begin_edit(iid, col)

    def _on_return(self, _event):
        sel = self.tree.selection()
        if sel and self._edit_widget is None:
            self._begin_edit(sel[0], "description")
            return "break"

    def _begin_edit(self, iid: str, col: str):
        self._cancel_edit()
        bbox = self.tree.bbox(iid, col)
        if not bbox:
            return
        x, y, w, h = bbox
        # Seed from the model, not the cell: the description cell may carry the
        # FILL_IN decoration, which must never become the edited value.
        zi = self._zone_info(int(iid))
        if col == "description" and zi is not None:
            current = zi.location or ""
        else:
            current = self.tree.set(iid, col)

        if col == "device_type":
            widget = ttk.Combobox(self.tree, values=DEVICE_TYPES,
                                  style="Zones.TCombobox",
                                  font=(self._ui_family, theme.SIZE["body"]))
            widget.set(current)
        else:
            widget = ttk.Entry(self.tree, style="Zones.TEntry",
                               font=(self._ui_family, theme.SIZE["body"]))
            widget.insert(0, current)
            widget.select_range(0, "end")
        widget.place(x=x, y=y, width=w, height=h)
        widget.focus_set()
        widget.bind("<Return>", lambda _e: self._commit_edit())
        widget.bind("<Escape>", lambda _e: self._cancel_edit())
        widget.bind("<FocusOut>", lambda _e: self._commit_edit())

        self._edit_widget, self._edit_item, self._edit_col = widget, iid, col

    def _cancel_edit(self):
        if self._edit_widget is not None:
            widget, self._edit_widget = self._edit_widget, None
            self._edit_item = self._edit_col = None
            widget.destroy()

    def _commit_edit(self):
        if self._edit_widget is None:
            return
        widget, iid, col = self._edit_widget, self._edit_item, self._edit_col
        self._edit_widget = None
        self._edit_item = self._edit_col = None
        value = self._undecorate(widget.get())
        widget.destroy()

        zi = self._zone_info(int(iid))
        if zi is None:
            return

        if col == "description":
            # Convention: unused points are exactly 'SPARE' (uppercase).
            if value.upper() == "SPARE":
                value = "SPARE"
            zi.location = value or None
            if value == "SPARE" and (zi.device_type or "").lower() != "spare":
                zi.device_type = "Spare"
        elif col == "device_type":
            zi.device_type = value or None
            if value.lower() == "spare" and (zi.location or "").upper() != "SPARE":
                zi.location = "SPARE"
        elif col == "partition":
            try:
                zi.partition = int(value) if value else None
            except ValueError:
                pass  # keep previous partition on junk input

        self.tree.item(iid, values=self._values_for(zi, self._zone_to_expander()))
        self._retag_row(iid, zi)
        self._update_counts()
        self.on_change()
