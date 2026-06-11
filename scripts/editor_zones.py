"""ZONES tab — searchable, inline-editable grid over design.zones.

ttk.Treeview (native, virtualized — fine at 480 rows) with a single reusable
overlay Entry/Combobox for cell editing; no extra dependency to ship in the
exe. Each commit mutates the backing ZoneInfo and notifies the editor, which
re-syncs master_zones and refreshes validation.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

import customtkinter as ctk

from parse_dmp_worksheet import DMPDesign, ZoneInfo

ACCENT = "#4a7bb8"
ERROR_BG = "#fde8e8"

DEVICE_TYPES = ["Motion", "Door Contact", "Glass Break", "Panic",
                "Supervisory", "Spare"]

COLUMNS = ("zone", "description", "device_type", "partition", "expander")
HEADINGS = {"zone": "ZONE", "description": "DESCRIPTION",
            "device_type": "DEVICE TYPE", "partition": "PART#",
            "expander": "EXP#"}
WIDTHS = {"zone": 64, "description": 240, "device_type": 110,
          "partition": 52, "expander": 46}
EDITABLE = {"description", "device_type", "partition"}

FILTERS = ["All", "Needs attention", "Spares", "Errors"]


class ZonesTab(ctk.CTkFrame):
    def __init__(self, master, design: DMPDesign, on_change):
        super().__init__(master, fg_color="transparent")
        self.design = design
        self.on_change = on_change  # called after any committed edit
        self._error_zones: set[int] = set()
        self._edit_widget: tk.Widget | None = None
        self._edit_item: str | None = None
        self._edit_col: str | None = None

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
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        bar.columnconfigure(0, weight=1)

        self._search_var = ctk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._apply_filter())
        ctk.CTkEntry(
            bar, height=30, placeholder_text="Search zone # or description…",
            textvariable=self._search_var,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self._filter_seg = ctk.CTkSegmentedButton(
            bar, values=FILTERS, height=30,
            selected_color=ACCENT, selected_hover_color=ACCENT,
            command=lambda _v: self._apply_filter(),
        )
        self._filter_seg.set("All")
        self._filter_seg.grid(row=0, column=1)

    # ------------------------------------------------------------------ #
    # Tree                                                                  #
    # ------------------------------------------------------------------ #

    def _build_tree(self):
        holder = ctk.CTkFrame(self, fg_color="transparent")
        holder.grid(row=1, column=0, sticky="nsew")
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)

        style = ttk.Style(holder)
        # Approximate the CTk light theme; 'clam' honors fieldbackground/rowheight.
        with_theme = "clam" if "clam" in style.theme_names() else style.theme_use()
        style.theme_use(with_theme)
        style.configure("Zones.Treeview", rowheight=28, font=("", 12),
                        background="white", fieldbackground="white")
        style.configure("Zones.Treeview.Heading", font=("", 11, "bold"))
        style.map("Zones.Treeview",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", "white")])

        self.tree = ttk.Treeview(
            holder, columns=COLUMNS, show="headings",
            style="Zones.Treeview", selectmode="browse", height=11,
        )
        for col in COLUMNS:
            self.tree.heading(col, text=HEADINGS[col])
            self.tree.column(col, width=WIDTHS[col],
                             stretch=(col == "description"),
                             anchor="w" if col in ("description", "device_type") else "center")
        self.tree.tag_configure("error", background=ERROR_BG)

        vsb = ttk.Scrollbar(holder, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")

        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Return>", self._on_return)
        self.tree.bind("<F2>", self._on_return)

    # ------------------------------------------------------------------ #
    # Data <-> rows                                                         #
    # ------------------------------------------------------------------ #

    def _zone_to_expander(self) -> dict[int, int]:
        return {z: r.number for r in self.design.rsps for z in r.zones}

    def _values_for(self, zi: ZoneInfo, exp_map: dict[int, int]) -> tuple:
        return (f"Z{zi.number}", zi.location or "", zi.device_type or "",
                zi.partition if zi.partition is not None else "",
                exp_map.get(zi.number, ""))

    def refresh(self):
        """Rebuild all rows from design.zones (item id == zone number)."""
        self._cancel_edit()
        self.tree.delete(*self.tree.get_children())
        exp_map = self._zone_to_expander()
        for zi in sorted(self.design.zones, key=lambda z: z.number):
            self.tree.insert("", "end", iid=str(zi.number),
                             values=self._values_for(zi, exp_map))
        self._retag_errors()
        self._apply_filter()

    def _zone_info(self, number: int) -> ZoneInfo | None:
        for zi in self.design.zones:
            if zi.number == number:
                return zi
        return None

    def set_error_zones(self, zone_numbers: set[int]):
        self._error_zones = zone_numbers
        self._retag_errors()

    def _retag_errors(self):
        for iid in self.tree.get_children(""):
            self.tree.item(iid, tags=("error",) if int(iid) in self._error_zones else ())

    def select_zone(self, number: int):
        """Scroll to and select a zone row (used by 'Go to' in the finalize gate)."""
        iid = str(number)
        if self.tree.exists(iid):
            self._search_var.set("")
            self._filter_seg.set("All")
            self._apply_filter()
            self.tree.see(iid)
            self.tree.selection_set(iid)

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
        needle = self._search_var.get().strip().lower()
        mode = self._filter_seg.get()
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
        current = self.tree.set(iid, col)

        if col == "device_type":
            widget = ttk.Combobox(self.tree, values=DEVICE_TYPES)
            widget.set(current)
        else:
            widget = ttk.Entry(self.tree)
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
        value = widget.get().strip()
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
        self.on_change()
