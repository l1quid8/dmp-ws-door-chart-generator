"""SPLITTERS / KEYPADS / POWER tabs of the project editor.

Form-style editors over the corresponding DMPDesign lists, including
post-CAD hardware changes: each tab can add and remove its hardware
(hardware.py owns the rules; tabs own the dialogs/cards). Structure changes
flow through on_structure_change so the editor can refresh sibling tabs —
a new expander must appear in the ZONES grid and splitter output menus.

The SPLITTERS tab also absorbs the two old pre-generation modal dialogs:
unresolved source-data conflicts render as banner cards (deferrable — they
block finalize, not editing), and splitter wiring is permanently editable
with a 'reviewed' checkbox feeding the topology.unconfirmed rule.

Beside the wiring cards sits the topology panel: a read-only tree derived
purely from the design (splitter inputs/outputs, RSPs, keypads, the XR550
location) so a tech can verify at a glance what the cards *mean* — which
splitter hangs off which bus, what each port feeds, what's still spare.
It owns no state; build_topology() is a pure function over DMPDesign and is
re-run whenever a value the tree actually shows has changed.
"""

from __future__ import annotations

import contextlib
import re
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import messagebox
from typing import Callable

import customtkinter as ctk

import theme
from hardware import (
    HardwareError,
    add_expander,
    add_keypad,
    add_splitter,
    existing_locations,
    remove_expander,
    remove_keypad,
    remove_splitter,
    renumber_splitter,
)
from session import Session
from ui_widgets import (
    AutocompleteEntry,
    Card,
    Chip,
    SectionLabel,
    accent_outline_button,
    attach_tooltip,
    flash,
    primary_button,
    remove_button,
    secondary_button,
)


def auto_hide_scrollbar(scrollframe: ctk.CTkScrollableFrame) -> None:
    """Show a CTkScrollableFrame's scrollbar only when its content overflows.

    CustomTkinter renders the scrollbar trough permanently; on a roomy window
    an un-scrollable panel reads as a random empty toolbar.

    The `_busy` guard is load-bearing, not defensive tidiness. CTkScrollbar.set()
    calls _draw(), and _draw() calls update_idletasks() — which drains the very
    <Configure> events that gridding the scrollbar just queued, re-entering this
    handler. Without the guard the cycle set → _draw → update_idletasks → set
    never terminates and the app hangs hard (seen on the pre-generate sheet as
    soon as it had enough issues to need scrolling).
    """
    scrollbar = scrollframe._scrollbar
    canvas = scrollframe._parent_canvas
    state = {"busy": False}

    def update(_event=None):
        if state["busy"]:
            return
        state["busy"] = True
        try:
            _apply()
        finally:
            state["busy"] = False

    def _apply():
        fits = scrollframe.winfo_reqheight() <= canvas.winfo_height() + 1
        managed = bool(scrollbar.winfo_manager())
        if fits and managed:
            scrollbar.grid_remove()
        elif not fits and not managed:
            scrollbar.grid()

    scrollframe.bind("<Configure>", update, add="+")
    canvas.bind("<Configure>", update, add="+")
    update()


# ------------------------------------------------------------------ #
# Topology model (pure — no Tk)                                         #
# ------------------------------------------------------------------ #
#
# Everything below the dataclasses is deliberately widget-free: it is the
# part that has to be right in the field (cycles, orphans, hand-typed
# values), so it is testable without a display, and the panel can compare
# two builds for equality to decide whether a repaint is even needed.

@dataclass
class TopoNode:
    """One row of the tree.

    kind: "splitter" | "rsp" | "keypad" | "spare" | "link" | "missing" | "other"
    ref:  the splitter id a click should jump to ("" when not applicable).
    """
    kind: str
    label: str
    meta: str = ""
    ref: str = ""
    children: list["TopoNode"] = field(default_factory=list)


@dataclass
class TopoGroup:
    """A bus (or the UNLINKED catch-all) and the splitters hanging off it."""
    label: str
    nodes: list[TopoNode] = field(default_factory=list)


@dataclass
class Topology:
    root_label: str = "XR550 PANEL"
    root_meta: str = ""
    groups: list[TopoGroup] = field(default_factory=list)


_BUS_RE = re.compile(r"^\s*(\d+)\s*BUS\b")
_RSP_RE = re.compile(r"^RSP[\s\-_]*(\d+)$", re.I)
_KEYPAD_RE = re.compile(r"^KEYPAD\s*#?\s*(\d+)$", re.I)
_GROUP_NUM_RE = re.compile(r"^(\d+) BUS")

_UNLINKED = "UNLINKED"


def _first_input(splitter) -> str:
    """The splitter's live input string — the same value the card's IN row edits."""
    return next((v for v in (splitter.inputs or {}).values() if v and v.strip()), "")


def _bus_label(text: str) -> str:
    """Group heading for a bus-feed input string."""
    up = text.upper()
    if "KEYPAD BUS" in up:
        return "KEYPAD BUS"
    match = _BUS_RE.match(up)
    if match:
        return f"{match.group(1)} BUS (LX)"
    # Hand-typed or legacy wording we don't recognise: still a panel feed,
    # just not one of the named buses.
    return "PANEL BUS"


def _classify_input(text: str, by_id: dict) -> tuple[str, str]:
    """("bus", label) | ("splitter", upstream id) | ("orphan", raw text).

    "From <id>" naming a splitter that no longer exists is an orphan, not a
    parent — removal cascades reset outputs but a hand-typed input survives.
    """
    value = (text or "").strip()
    if not value:
        return ("orphan", "")
    if value[:5].lower() == "from ":
        ref = value[5:].strip()
        if ref in by_id:
            return ("splitter", ref)
        return ("orphan", value)
    return ("bus", _bus_label(value))


def _zone_range(rsp) -> str:
    return f"Z{min(rsp.zones)}–Z{max(rsp.zones)}" if rsp.zones else ""


def _group_sort_key(label: str) -> tuple[int, int]:
    match = _GROUP_NUM_RE.match(label)
    if match:
        return (0, int(match.group(1)))
    if label == "KEYPAD BUS":
        return (1, 0)
    return (2, 0)


def build_topology(design) -> Topology:
    """Derive the whole tree from the design. No state, no side effects.

    Splitters whose input names a bus hang off that bus group; a splitter
    whose input is "From X" hangs under X. Anything a bus can't reach —
    orphans, blank inputs, mutual A→B/B→A cycles — lands under UNLINKED, so
    every splitter is rendered exactly once and none silently vanishes.
    """
    splitters = list(getattr(design, "splitters", None) or [])
    rsps = {r.number: r for r in (getattr(design, "rsps", None) or [])}
    keypads = {k.number: k for k in (getattr(design, "keypads", None) or [])}
    by_id = {s.id: s for s in splitters}
    parents = {s.id: _classify_input(_first_input(s), by_id) for s in splitters}

    # The one guard that keeps a mis-wired job from hanging the app: a
    # splitter is expanded at most once for the whole build, so no descent
    # can revisit a node and no cycle can recurse.
    emitted: set[str] = set()

    def leaf(text: str, owner_id: str) -> TopoNode:
        value = (text or "").strip()
        if not value or value.lower() == "spare":
            return TopoNode("spare", "Spare")
        if value[:3].lower() == "to ":
            ref = value[3:].strip()
            target = by_id.get(ref)
            if target is None:
                return TopoNode("missing", value)
            if parents.get(ref) == ("splitter", owner_id) and ref not in emitted:
                return expand(target)
            # The target lives elsewhere in the tree (its own input says so),
            # or we already drew it: show the link, don't duplicate the node.
            return TopoNode("link", f"To {ref}", ref=ref)
        match = _RSP_RE.match(value)
        if match:
            number = int(match.group(1))
            rsp = rsps.get(number)
            return TopoNode("rsp", f"RSP-{number}",
                            meta=_zone_range(rsp) if rsp else "not in design")
        match = _KEYPAD_RE.match(value)
        if match:
            number = int(match.group(1))
            keypad = keypads.get(number)
            return TopoNode("keypad", f"KEYPAD #{number}",
                            meta=(keypad.location or "") if keypad
                                 else "not in design")
        return TopoNode("other", value)

    def expand(splitter) -> TopoNode:
        emitted.add(splitter.id)          # before descending — self-links too
        node = TopoNode("splitter", splitter.id, ref=splitter.id)
        for raw in (splitter.outputs or []):
            node.children.append(leaf(raw, splitter.id))
        # A downstream splitter whose input claims this parent but which the
        # parent's own outputs never mention still belongs here.
        for other in splitters:
            if other.id not in emitted and \
                    parents.get(other.id) == ("splitter", splitter.id):
                node.children.append(expand(other))
        return node

    groups: dict[str, list[TopoNode]] = {}
    for splitter in splitters:
        kind, value = parents[splitter.id]
        if kind == "bus" and splitter.id not in emitted:
            groups.setdefault(value, []).append(expand(splitter))

    ordered = [TopoGroup(label, groups[label])
               for label in sorted(groups, key=_group_sort_key)]

    # Whatever no bus reached. Expanding in design order means a cycle's
    # first member becomes the visible root and the rest hang under it.
    unlinked = [expand(s) for s in splitters if s.id not in emitted]
    if unlinked:
        ordered.append(TopoGroup(_UNLINKED, unlinked))

    site = getattr(design, "site_info", None)
    return Topology(root_label="XR550 PANEL",
                    root_meta=(getattr(site, "xr550_location", "") or ""),
                    groups=ordered)


# ------------------------------------------------------------------ #
# Local styling helpers                                                 #
# ------------------------------------------------------------------ #
#
# CTkOptionMenu has no border_* options, so the spec's bordered dropdowns
# are a 1px-border frame wrapping a borderless menu whose fill matches the
# frame. The tone table below drives both halves.
# (fill, text, border)
_MENU_TONES = {
    "connected": (theme.SURFACE, theme.TEXT, theme.BORDER_STRONG),
    "link": (theme.ACCENT_TINT, theme.ACCENT, theme.ACCENT),
    "spare": (theme.SURFACE_CHIP, theme.TEXT_TERTIARY, theme.BORDER),
}


def _menu_tone(value: str) -> str:
    text = (value or "").strip()
    if not text or text.lower() in ("spare", "— select source —"):
        return "spare"
    if text[:3].lower() == "to ":
        return "link"
    return "connected"


def _style_menu(holder: ctk.CTkFrame, menu: ctk.CTkOptionMenu, tone: str) -> None:
    fill, fg, border = _MENU_TONES[tone]
    holder.configure(fg_color=fill, border_color=border)
    menu.configure(fg_color=fill, button_color=fill, text_color=fg)


def _bordered_menu(parent, values, command, *, tone: str,
                   width: int = 108) -> tuple[ctk.CTkFrame, ctk.CTkOptionMenu]:
    """A h26 dropdown with a state-carrying border. Returns (holder, menu)."""
    fill, fg, border = _MENU_TONES[tone]
    holder = ctk.CTkFrame(parent, fg_color=fill, border_width=1,
                          border_color=border,
                          corner_radius=theme.RADIUS["control"])
    menu = ctk.CTkOptionMenu(
        holder, values=values, width=width,
        height=theme.HEIGHT["control"] - 2,
        corner_radius=theme.RADIUS["control"],
        fg_color=fill, button_color=fill,
        button_hover_color=theme.HOVER_SUBTLE, text_color=fg,
        font=theme.ui_font(theme.SIZE["chip"]),
        dropdown_fg_color=theme.SURFACE, dropdown_text_color=theme.TEXT,
        dropdown_hover_color=theme.HOVER_SUBTLE,
        dropdown_font=theme.ui_font(theme.SIZE["chip"]),
        command=command,
    )
    menu.pack(fill="x", padx=1, pady=1)
    return holder, menu


def _styled_checkbox(parent, text: str, variable, command, *,
                     accent=None, text_color=None) -> ctk.CTkCheckBox:
    """A 17px checkbox in the app's palette — the spec's only checkbox recipe."""
    accent = accent or theme.ACCENT
    return ctk.CTkCheckBox(
        parent, text=text, variable=variable, command=command,
        width=17, checkbox_width=17, checkbox_height=17,
        corner_radius=theme.RADIUS["tag"], border_width=2,
        fg_color=accent, hover_color=accent, border_color=accent,
        checkmark_color=theme.ON_ACCENT,
        text_color=text_color or theme.TEXT,
        font=theme.ui_font(theme.SIZE["chip"], "bold"),
    )


def _styled_radio(parent, text: str, variable, value) -> ctk.CTkRadioButton:
    return ctk.CTkRadioButton(
        parent, text=text, variable=variable, value=value,
        radiobutton_width=17, radiobutton_height=17, border_width_checked=5,
        fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
        border_color=theme.BORDER_STRONG, text_color=theme.TEXT,
        font=theme.ui_font(theme.SIZE["chip"]),
    )


def _styled_entry(parent, **kw) -> ctk.CTkEntry:
    kw.setdefault("height", theme.HEIGHT["control"] + 2)
    kw.setdefault("corner_radius", theme.RADIUS["button"])
    kw.setdefault("border_width", 1)
    kw.setdefault("border_color", theme.BORDER_STRONG)
    kw.setdefault("fg_color", theme.SURFACE)
    kw.setdefault("text_color", theme.TEXT)
    kw.setdefault("font", theme.ui_font(theme.SIZE["chip"]))
    return ctk.CTkEntry(parent, **kw)


def _empty_note(parent, text: str) -> ctk.CTkLabel:
    return ctk.CTkLabel(parent, text=text, text_color=theme.TEXT_TERTIARY,
                        font=theme.ui_font(theme.SIZE["chip"]))


# ------------------------------------------------------------------ #
# Add-hardware dialogs (shared)                                         #
# ------------------------------------------------------------------ #

def _dialog_shell(root, title: str) -> ctk.CTkToplevel:
    win = ctk.CTkToplevel(root)
    win.title(title)
    win.transient(root)
    win.grab_set()
    win.resizable(False, False)
    win.configure(fg_color=theme.APP_BG)
    return win


def _dialog_title(win, text: str) -> None:
    ctk.CTkLabel(win, text=text, text_color=theme.TEXT,
                 font=theme.ui_font(theme.SIZE["body"], "bold"),
                 ).pack(anchor="w", padx=20, pady=(16, 8))


def _dialog_buttons(win, confirm_text: str, on_confirm) -> None:
    row = ctk.CTkFrame(win, fg_color="transparent")
    row.pack(fill="x", padx=20, pady=(8, 16))
    row.columnconfigure(0, weight=1)
    row.columnconfigure(1, weight=1)
    secondary_button(row, "Cancel",
                     lambda: (win.grab_release(), win.destroy()),
                     ).grid(row=0, column=0, sticky="ew", padx=(0, 6))
    primary_button(row, confirm_text, on_confirm,
                   ).grid(row=0, column=1, sticky="ew", padx=(6, 0))


def prompt_add_expander(root, session: Session, on_done) -> None:
    win = _dialog_shell(root, "Add expander")
    _dialog_title(win, "Add a 714 expander (RSP + power supply + zone block)")

    model_var = tk.StringVar(value="714-16")
    for model, label in [("714-16", "714-16  (16 points)"),
                         ("714-8", "714-8  (8 points)")]:
        _styled_radio(win, label, model_var, model).pack(anchor="w", padx=28, pady=3)

    loc = AutocompleteEntry(win, lambda: existing_locations(session.design),
                            width=320, height=theme.HEIGHT["button"],
                            placeholder_text="Location (e.g. BLDG A 1ST FLR IDF)")
    loc.pack(padx=20, pady=(10, 0))

    def confirm():
        try:
            add_expander(session.design, model_var.get(),
                         loc.get().strip() or None)
        except HardwareError as exc:
            messagebox.showerror("Can't add expander", str(exc), parent=win)
            return
        win.grab_release()
        win.destroy()
        on_done()

    _dialog_buttons(win, "Add expander", confirm)


def prompt_add_splitter(root, session: Session, on_done) -> None:
    win = _dialog_shell(root, "Add splitter")
    _dialog_title(win, "Add a 710 splitter-repeater")

    type_var = tk.StringVar(value="LX")
    for stype, label in [("LX", "LX bus  (710-LX500-N — RSP feeds)"),
                         ("KP", "KP bus  (710-KP-N — keypad feeds)")]:
        _styled_radio(win, label, type_var, stype).pack(anchor="w", padx=28, pady=3)

    loc = AutocompleteEntry(win, lambda: existing_locations(session.design),
                            width=320, height=theme.HEIGHT["button"],
                            placeholder_text="Location")
    loc.pack(padx=20, pady=(10, 0))

    def confirm():
        try:
            add_splitter(session.design, type_var.get(), loc.get().strip() or None)
        except HardwareError as exc:
            messagebox.showerror("Can't add splitter", str(exc), parent=win)
            return
        win.grab_release()
        win.destroy()
        on_done()

    _dialog_buttons(win, "Add splitter", confirm)


def _keypad_source_choices(session: Session) -> list[str]:
    # Keypads ride the KP bus: fed straight from the panel (MSP) or from a
    # KP-type splitter output.
    return ["MSP"] + [s.id for s in session.design.splitters
                      if s.splitter_type == "KP"]


def prompt_add_keypad(root, session: Session, on_done) -> None:
    win = _dialog_shell(root, "Add keypad")
    _dialog_title(win, "Add a keypad")

    loc = AutocompleteEntry(win, lambda: existing_locations(session.design),
                            width=320, height=theme.HEIGHT["button"],
                            placeholder_text="Location")
    loc.pack(padx=20, pady=(2, 8))

    holder, source_menu = _bordered_menu(
        win, _keypad_source_choices(session), None,
        tone="connected", width=318)
    holder.pack(padx=20)

    glob_var = tk.BooleanVar(value=False)
    _styled_checkbox(win, "Global keypad", glob_var, None,
                     ).pack(anchor="w", padx=20, pady=(10, 0))

    def confirm():
        try:
            add_keypad(session.design, loc.get().strip() or None,
                       source_menu.get(), glob_var.get())
        except HardwareError as exc:
            messagebox.showerror("Can't add keypad", str(exc), parent=win)
            return
        win.grab_release()
        win.destroy()
        on_done()

    _dialog_buttons(win, "Add keypad", confirm)


# ------------------------------------------------------------------ #
# SPLITTERS                                                             #
# ------------------------------------------------------------------ #

CARDS_COLUMN_WIDTH = 440


class SplittersTab(ctk.CTkFrame):
    """Wiring cards on the left, the derived topology tree on the right."""

    def __init__(self, master, session: Session, on_change,
                 on_structure_change=None, on_hardware_change=None, *,
                 on_navigate: Callable[[str], None] | None = None):
        super().__init__(master, fg_color="transparent")
        self.session = session
        self.on_change = on_change
        self.on_structure_change = on_structure_change or on_change
        # Removals route through here so the editor can report cascade fallout;
        # falls back to a plain mutate + structure-refresh when unset.
        self.on_hardware_change = on_hardware_change or (
            lambda mutate: (mutate(), self.on_structure_change()))
        # Set by the editor to its tab-switcher; the topology leaves use it to
        # send the tech to the tab that owns the device they clicked.
        self.on_navigate = on_navigate

        self._cards: dict[str, ctk.CTkFrame] = {}
        self._topology: Topology | None = None
        self._topo_after: str | None = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        # Conflict + review banners span both panes: they describe the tab,
        # not one column, and they must not scroll away with the cards.
        self.top = ctk.CTkFrame(self, fg_color="transparent")
        self.top.grid(row=0, column=0, sticky="ew")
        self.top.columnconfigure(0, weight=1)

        panes = ctk.CTkFrame(self, fg_color="transparent")
        panes.grid(row=1, column=0, sticky="nsew", pady=(theme.PAD["md"], 0))
        panes.columnconfigure(0, weight=0, minsize=CARDS_COLUMN_WIDTH)
        panes.columnconfigure(1, weight=1)
        panes.rowconfigure(0, weight=1)

        self.body = ctk.CTkScrollableFrame(panes, fg_color="transparent",
                                           width=CARDS_COLUMN_WIDTH)
        self.body.grid(row=0, column=0, sticky="nsew",
                       padx=(0, theme.PAD["md"]))
        self.body.columnconfigure(0, weight=1)
        auto_hide_scrollbar(self.body)

        self._build_topology_panel(panes)
        self.refresh()

    # ---- topology panel shell ----

    def _build_topology_panel(self, parent) -> None:
        card = Card(parent)
        card.grid(row=0, column=1, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(2, weight=1)
        SectionLabel(card, "Topology").grid(
            row=0, column=0, sticky="w", padx=theme.PAD["md"],
            pady=(theme.PAD["md"], 0))
        ctk.CTkLabel(card, text="click a node to jump to its card",
                     text_color=theme.TEXT_TERTIARY, anchor="w",
                     font=theme.ui_font(theme.SIZE["meta"])).grid(
            row=1, column=0, sticky="w", padx=theme.PAD["md"], pady=(1, 6))
        self._topo_body = ctk.CTkScrollableFrame(card, fg_color="transparent")
        self._topo_body.grid(row=2, column=0, sticky="nsew",
                             padx=(theme.PAD["sm"], theme.PAD["xs"]),
                             pady=(0, theme.PAD["sm"]))
        self._topo_body.columnconfigure(0, weight=1)
        auto_hide_scrollbar(self._topo_body)

    # ---- full refresh ----

    def refresh(self):
        for w in self.top.winfo_children():
            w.destroy()
        for w in self.body.winfo_children():
            w.destroy()
        self._cards.clear()

        row = 0
        for conflict in [c for c in self.session.design.conflicts
                         if getattr(c, "kind", "") == "RSP"]:
            self._build_conflict_banner(conflict).grid(
                row=row, column=0, sticky="ew", pady=(0, theme.PAD["sm"]))
            row += 1
        self._build_header_row().grid(row=row, column=0, sticky="ew")

        design = self.session.design
        if not design.splitters:
            _empty_note(self.body, "No splitters in this design.").grid(
                row=0, column=0, pady=24)
        else:
            for i, splitter in enumerate(design.splitters):
                card = self._build_splitter_card(splitter)
                card.grid(row=i, column=0, sticky="ew", pady=(0, theme.PAD["sm"]))
                self._cards[splitter.id] = card

        self._rebuild_topology(force=True)

    # ---- conflict banners ----

    def _build_conflict_banner(self, conflict) -> ctk.CTkFrame:
        card = Card(self.top, fg_color=theme.BANNER_BG,
                    border_color=theme.BANNER_BORDER, corner_radius=9)
        card.columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card, text=f"⚠  The CAD prints disagree: {conflict.label}",
            font=theme.ui_font(theme.SIZE["chip"], "bold"),
            text_color=theme.BANNER_TEXT, anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=14, pady=(10, 2))

        value_var = tk.StringVar(value=conflict.options[0][0])
        r = 1
        for value, source in conflict.options:
            _styled_radio(card, f"{value}    ({source})", value_var, value).grid(
                row=r, column=0, sticky="w", padx=22, pady=2)
            r += 1
        custom = _styled_entry(card,
                               placeholder_text="…or type the correct value")
        custom.grid(row=r, column=0, sticky="ew", padx=22, pady=(4, 6))

        def resolve():
            from generate_dmp_ws import apply_location_conflict
            chosen = custom.get().strip() or value_var.get()
            apply_location_conflict(self.session.design, conflict, chosen)
            self.session.design.conflicts.remove(conflict)
            self.on_change()
            self.refresh()

        primary_button(card, "Use this value", resolve,
                       height=theme.HEIGHT["button_md"], width=140).grid(
            row=r + 1, column=0, sticky="w", padx=22, pady=(0, 10))
        return card

    # ---- header: review banner + add button ----

    def _build_header_row(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.top, fg_color="transparent")
        frame.columnconfigure(0, weight=1)

        accent_outline_button(frame, "+ Add Splitter", self._add_clicked,
                              width=150).grid(row=0, column=1, sticky="e")

        # Riser-derived wiring is trustworthy; the auto-derived convention is a
        # guess, so it gets the amber treatment until a human signs it off.
        riser = self.session.design.topology_source == "riser"
        if riser:
            glyph, note = "ℹ", "Wiring below was read from the riser diagram."
            bg, border, fg = theme.SURFACE, theme.BORDER, theme.TEXT_TERTIARY
            check_accent = theme.ACCENT
        else:
            glyph = "⚠"
            note = ("Riser extraction was incomplete — the wiring below is a "
                    "best-guess convention. Check it against the riser diagram.")
            bg, border, fg = (theme.BANNER_BG, theme.BANNER_BORDER,
                              theme.BANNER_TEXT)
            check_accent = theme.WARNING

        banner = Card(frame, fg_color=bg, border_color=border, corner_radius=9)
        banner.grid(row=1, column=0, columnspan=2, sticky="ew",
                    pady=(theme.PAD["sm"], 0))
        banner.columnconfigure(1, weight=1)
        ctk.CTkLabel(banner, text=glyph, text_color=fg,
                     font=theme.ui_font(theme.SIZE["body"], "bold")).grid(
            row=0, column=0, sticky="w", padx=(14, theme.PAD["sm"]), pady=10)
        ctk.CTkLabel(banner, text=note, text_color=fg, anchor="w",
                     justify="left", wraplength=620,
                     font=theme.ui_font(theme.SIZE["chip"])).grid(
            row=0, column=1, sticky="w", pady=10)

        self._reviewed_var = tk.BooleanVar(value=self.session.topology_confirmed)

        def toggled():
            self.session.topology_confirmed = self._reviewed_var.get()
            self.on_change()

        reviewed_cb = _styled_checkbox(banner, "Wiring reviewed",
                                       self._reviewed_var, toggled,
                                       accent=check_accent, text_color=fg)
        reviewed_cb.grid(row=0, column=2, sticky="e", padx=(theme.PAD["md"], 14),
                         pady=10)
        attach_tooltip(
            reviewed_cb,
            "Confirms the splitter wiring matches the riser diagram. Until it's "
            "checked, generating a worksheet shows a warning. Adding or removing "
            "hardware re-opens this for review.")
        return frame

    def _add_clicked(self):
        prompt_add_splitter(self.winfo_toplevel(), self.session,
                            self.on_structure_change)

    def _remove_clicked(self, splitter):
        if not messagebox.askyesno(
            "Remove splitter?",
            f"Remove {splitter.id}?\n\nOutputs on other splitters that point "
            "to it become Spare; keypads it fed will need a new source.",
        ):
            return
        self.on_hardware_change(
            lambda: remove_splitter(self.session.design, splitter.id))

    def _renumber_clicked(self, splitter, entry):
        if not entry.winfo_exists():
            return  # widget already torn down by a prior rebuild
        text = entry.get().strip()
        cur_num = splitter.id.rsplit("-", 1)[-1]
        if not text.isdigit():
            entry.delete(0, "end")
            entry.insert(0, cur_num)
            return
        new_number = int(text)
        if str(new_number) == cur_num:
            return  # unchanged
        try:
            renumber_splitter(self.session.design, splitter.id, new_number)
        except HardwareError as exc:
            messagebox.showwarning("Can't renumber splitter", str(exc),
                                   parent=self.winfo_toplevel())
            entry.delete(0, "end")
            entry.insert(0, cur_num)
            return
        self.on_structure_change()

    # ---- splitter cards ----

    def _output_choices(self, splitter) -> list[str]:
        design = self.session.design
        rsp_names = [f"RSP-{r.number}" for r in design.rsps]
        kp_names = [f"KEYPAD #{k.number}" for k in design.keypads if k.number != 1]
        others = [f"To {o.id}" for o in design.splitters if o.id != splitter.id]
        return ["Spare"] + rsp_names + kp_names + others

    def _input_choices(self, splitter) -> list[str]:
        design = self.session.design
        if splitter.splitter_type == "LX":
            bus = [f"{n} BUS IN FROM XR/550" for n in (500, 600, 700)]
        else:
            bus = ["KEYPAD BUS IN FROM XR/550"]
        upstream = [f"From {o.id}" for o in design.splitters if o.id != splitter.id]
        return bus + upstream

    def _build_splitter_card(self, splitter) -> ctk.CTkFrame:
        card = Card(self.body)
        card.columnconfigure(1, weight=1)

        id_row = ctk.CTkFrame(card, fg_color="transparent")
        id_row.grid(row=0, column=0, sticky="w", padx=(12, 0), pady=(11, 0))
        prefix = "710-LX500-" if splitter.splitter_type == "LX" else "710-KP-"
        ctk.CTkLabel(id_row, text=prefix, text_color=theme.TEXT,
                     font=theme.mono_font(13, "bold")).pack(side="left")
        num_entry = _styled_entry(id_row, width=40,
                                  height=theme.HEIGHT["control"],
                                  justify="center",
                                  font=theme.mono_font(13, "bold"))
        cur_num = splitter.id.rsplit("-", 1)[-1]
        num_entry.insert(0, cur_num)
        num_entry.pack(side="left")
        num_entry.bind("<Return>",
                       lambda _e, s=splitter, w=num_entry: self._renumber_clicked(s, w))
        num_entry.bind("<FocusOut>",
                       lambda _e, s=splitter, w=num_entry: self._renumber_clicked(s, w))

        loc_var = tk.StringVar(value=splitter.location or "")

        def loc_edited(*_a):
            splitter.location = loc_var.get().strip() or None
            self.on_change()

        loc_var.trace_add("write", loc_edited)
        _styled_entry(card, textvariable=loc_var,
                      placeholder_text="Splitter location",
                      ).grid(row=0, column=1, sticky="ew",
                             padx=(theme.PAD["sm"], theme.PAD["xs"]),
                             pady=(11, 0))

        remove_button(card, lambda s=splitter: self._remove_clicked(s)).grid(
            row=0, column=2, padx=(0, theme.PAD["sm"]), pady=(11, 0))

        # -- IN: the current feed, styled as the chip the spec asks for while
        # staying an editable combobox (a tech retypes these by hand).
        in_row = ctk.CTkFrame(card, fg_color="transparent")
        in_row.grid(row=1, column=0, columnspan=3, sticky="ew",
                    padx=12, pady=(theme.PAD["sm"], 0))
        in_row.columnconfigure(1, weight=1)
        ctk.CTkLabel(in_row, text="IN", width=30, anchor="w",
                     text_color=theme.TEXT_TERTIARY,
                     font=theme.ui_font(theme.SIZE["badge"], "bold")).grid(
            row=0, column=0, sticky="w")

        first_input = _first_input(splitter)
        in_var = tk.StringVar(value=first_input)
        combo = ctk.CTkComboBox(
            in_row, variable=in_var, height=theme.HEIGHT["control"],
            corner_radius=theme.RADIUS["control"], border_width=1,
            values=self._input_choices(splitter),
            font=theme.mono_font(theme.SIZE["meta"]),
            dropdown_fg_color=theme.SURFACE, dropdown_text_color=theme.TEXT,
            dropdown_hover_color=theme.HOVER_SUBTLE,
            dropdown_font=theme.ui_font(theme.SIZE["chip"]),
        )
        combo.grid(row=0, column=1, sticky="ew")
        self._tone_input(combo, first_input)
        in_var.trace_add("write", lambda *_a, s=splitter, v=in_var, c=combo:
                         (self._set_input(s, v.get()), self._tone_input(c, v.get())))

        # -- OUT: three ports, each carrying its state in its border.
        out_row = ctk.CTkFrame(card, fg_color="transparent")
        out_row.grid(row=2, column=0, columnspan=3, sticky="ew",
                     padx=12, pady=(6, 11))
        ctk.CTkLabel(out_row, text="OUT", width=30, anchor="w",
                     text_color=theme.TEXT_TERTIARY,
                     font=theme.ui_font(theme.SIZE["badge"], "bold")).grid(
            row=0, column=0, sticky="w")

        choices = self._output_choices(splitter)
        outs = list(splitter.outputs or [])
        for i in range(3):
            current = outs[i] if i < len(outs) else "Spare"
            values = list(choices) if current in choices else [current] + list(choices)
            holder, menu = _bordered_menu(out_row, values, None,
                                          tone=_menu_tone(current))
            menu.configure(
                command=lambda value, s=splitter, idx=i, h=holder, m=menu:
                (_style_menu(h, m, _menu_tone(value)), self._set_output(s, idx, value)))
            menu.set(current)
            out_row.columnconfigure(i + 1, weight=1)
            holder.grid(row=0, column=i + 1, sticky="ew",
                        padx=(theme.PAD["xs"], 0))
        return card

    def _tone_input(self, combo: ctk.CTkComboBox, value: str) -> None:
        """Bus feeds read neutral; a splitter-to-splitter feed reads accent."""
        link = (value or "").strip()[:5].lower() == "from "
        if link:
            combo.configure(fg_color=theme.ACCENT_TINT, border_color=theme.ACCENT,
                            text_color=theme.ACCENT, button_color=theme.ACCENT_TINT,
                            button_hover_color=theme.ACCENT_TINT)
        else:
            combo.configure(fg_color=theme.SURFACE_CHIP,
                            border_color=theme.BORDER_STRONG,
                            text_color=theme.TEXT_SECOND,
                            button_color=theme.SURFACE_CHIP,
                            button_hover_color=theme.HOVER_SUBTLE)

    def _set_input(self, splitter, value: str):
        val = value.strip()
        # preserve the existing input key if present, else derive from bus type
        key = next(iter(splitter.inputs), None) or (
            "LX-Bus In" if splitter.splitter_type == "LX" else "KP-Bus In")
        splitter.inputs = {key: val} if val else {}
        self.on_change()
        self._schedule_topology_rebuild()

    def _set_output(self, splitter, index: int, value: str):
        outs = list(splitter.outputs or [])
        while len(outs) <= index:
            outs.append("Spare")
        outs[index] = value
        splitter.outputs = outs
        self.on_change()
        self._schedule_topology_rebuild()

    # ---- topology panel ----

    def _schedule_topology_rebuild(self) -> None:
        """Coalesce rebuilds: the IN combobox is a text field, so its trace
        fires per keystroke while a tech types "From 710-…"."""
        if self._topo_after is not None:
            with contextlib.suppress(Exception):
                self.after_cancel(self._topo_after)
        self._topo_after = self.after(150, self._rebuild_topology)

    def _rebuild_topology(self, *, force: bool = False) -> None:
        self._topo_after = None
        if not self._topo_body.winfo_exists():
            return
        topology = build_topology(self.session.design)
        # Nothing the tree *shows* changed — splitter locations, for one, live
        # only on the cards — so leave the panel (and its scroll position) be.
        if not force and topology == self._topology:
            return
        self._topology = topology
        for w in self._topo_body.winfo_children():
            w.destroy()
        self._render_topology(topology)

    def _render_topology(self, topology: Topology) -> None:
        row = 0
        root = ctk.CTkFrame(self._topo_body, fg_color="transparent")
        root.grid(row=row, column=0, sticky="w", pady=(2, 6))
        # The root is the one solid chip in the tree; Chip's variants are all
        # tinted, so it is composed here rather than shoehorned into one.
        root_chip = ctk.CTkFrame(root, fg_color=theme.NODE_ROOT_BG,
                                 corner_radius=theme.RADIUS["control"])
        root_chip.pack(side="left")
        ctk.CTkLabel(root_chip, text=topology.root_label,
                     text_color=theme.NODE_ROOT_TEXT,
                     font=theme.mono_font(theme.SIZE["chip"], "bold")).grid(
            row=0, column=0, padx=9, pady=4)
        if topology.root_meta:
            ctk.CTkLabel(root, text=topology.root_meta,
                         text_color=theme.TEXT_TERTIARY,
                         font=theme.ui_font(theme.SIZE["meta"])).pack(
                side="left", padx=(theme.PAD["sm"], 0))
        row += 1

        if not topology.groups:
            _empty_note(self._topo_body, "Nothing wired yet.").grid(
                row=row, column=0, sticky="w", pady=theme.PAD["md"])
            return

        for group in topology.groups:
            block = ctk.CTkFrame(self._topo_body, fg_color="transparent")
            block.grid(row=row, column=0, sticky="ew", pady=(0, 6))
            block.columnconfigure(1, weight=1)
            SectionLabel(block, group.label).grid(
                row=0, column=0, columnspan=2, sticky="w", padx=(2, 0),
                pady=(0, 3))
            self._render_children(block, group.nodes, start_row=1)
            row += 1

    def _render_children(self, parent, nodes: list[TopoNode], *,
                         start_row: int) -> None:
        """Draw the indent guide (a 2px rail) plus one subtree per node."""
        # height=1, not the CTkFrame default: a childless frame requests 200×200,
        # and with grid_propagate off that floor would push every nested row
        # 200px tall. sticky="ns" then stretches the rail to whatever height the
        # subtree beside it actually needs.
        rail = ctk.CTkFrame(parent, width=2, height=1, fg_color=theme.BORDER,
                            corner_radius=0)
        rail.grid(row=start_row, column=0, sticky="ns", padx=(6, 0))
        rail.grid_propagate(False)
        stack = ctk.CTkFrame(parent, fg_color="transparent")
        stack.grid(row=start_row, column=1, sticky="ew")
        stack.columnconfigure(0, weight=1)
        for i, node in enumerate(nodes):
            self._render_node(stack, node).grid(row=i, column=0, sticky="ew",
                                                pady=1)

    def _render_node(self, parent, node: TopoNode) -> ctk.CTkFrame:
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.columnconfigure(1, weight=1)

        line = ctk.CTkFrame(wrap, fg_color="transparent")
        line.grid(row=0, column=0, columnspan=2, sticky="w")
        # The 10×2 tick that ties this node back to its parent's rail.
        tick = ctk.CTkFrame(line, width=10, height=2, fg_color=theme.BORDER,
                            corner_radius=0)
        tick.pack(side="left", pady=(2, 0))
        tick.pack_propagate(False)
        self._node_chip(line, node).pack(side="left", padx=(4, 0))
        if node.meta:
            ctk.CTkLabel(line, text=node.meta, text_color=theme.TEXT_TERTIARY,
                         font=theme.ui_font(theme.SIZE["badge"])).pack(
                side="left", padx=(6, 0))

        if node.children:
            self._render_children(wrap, node.children, start_row=1)
        return wrap

    def _node_chip(self, parent, node: TopoNode) -> ctk.CTkFrame:
        click = self._node_command(node)
        if node.kind == "splitter":
            return Chip(parent, node.label, variant="accent_tint", pill=False,
                        mono=True, size=theme.SIZE["chip"], padx=8, pady=3,
                        border_width=2, corner_radius=theme.RADIUS["control"],
                        on_click=click)
        if node.kind == "spare":
            return Chip(parent, "Spare", variant="dashed", pill=False,
                        size=theme.SIZE["meta"], bold=False, padx=8, pady=2,
                        corner_radius=theme.RADIUS["chip"])
        if node.kind == "link":
            return Chip(parent, node.label, variant="accent_tint", pill=False,
                        mono=True, size=theme.SIZE["meta"], padx=8, pady=2,
                        bold=False, corner_radius=theme.RADIUS["chip"],
                        on_click=click)
        if node.kind == "missing":
            return Chip(parent, node.label, variant="warning", pill=False,
                        size=theme.SIZE["meta"], padx=8, pady=2,
                        corner_radius=theme.RADIUS["chip"])
        # rsp / keypad / other: a quiet outline chip.
        return Chip(parent, node.label, variant="outline", pill=False, mono=True,
                    size=theme.SIZE["meta"], bold=False, padx=8, pady=2,
                    corner_radius=theme.RADIUS["chip"], on_click=click)

    def _node_command(self, node: TopoNode) -> Callable[[], None] | None:
        if node.kind in ("splitter", "link") and node.ref:
            return lambda ref=node.ref: self._focus_card(ref)
        if self.on_navigate is None:
            return None
        if node.kind == "rsp":
            return lambda: self.on_navigate("POWER")
        if node.kind == "keypad":
            return lambda: self.on_navigate("KEYPADS")
        return None

    def _focus_card(self, splitter_id: str) -> None:
        """Scroll the clicked node's card into view and pulse it."""
        card = self._cards.get(splitter_id)
        if card is None or not card.winfo_exists():
            return
        # Reaches into CustomTkinter internals — the scrollable frame exposes
        # no public scroll-to-child API. Never let a layout race raise here.
        with contextlib.suppress(Exception):
            self.body.update_idletasks()
            content = max(self.body.winfo_reqheight(), 1)
            self.body._parent_canvas.yview_moveto(
                min(1.0, max(0.0, card.winfo_y() / content)))
        flash(card, theme.ACCENT_TINT)


# ------------------------------------------------------------------ #
# KEYPADS                                                               #
# ------------------------------------------------------------------ #

class KeypadsTab(ctk.CTkFrame):
    def __init__(self, master, session: Session, on_change,
                 on_structure_change=None, on_hardware_change=None, *,
                 on_navigate: Callable[[str], None] | None = None):
        super().__init__(master, fg_color="transparent")
        self.session = session
        self.on_change = on_change
        self.on_structure_change = on_structure_change or on_change
        # Removals route through here so the editor can report cascade fallout;
        # falls back to a plain mutate + structure-refresh when unset.
        self.on_hardware_change = on_hardware_change or (
            lambda mutate: (mutate(), self.on_structure_change()))
        # Accepted for a uniform tab contract; this tab has nothing to link to.
        self.on_navigate = on_navigate
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.body.grid(row=0, column=0, sticky="nsew")
        self.body.columnconfigure(0, weight=1)
        auto_hide_scrollbar(self.body)
        self.refresh()

    def refresh(self):
        for w in self.body.winfo_children():
            w.destroy()

        header = ctk.CTkFrame(self.body, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        header.columnconfigure(0, weight=1)
        SectionLabel(header, "Keypads").grid(row=0, column=0, sticky="w")
        accent_outline_button(header, "+ Add Keypad", self._add_clicked,
                              width=150).grid(row=0, column=1, sticky="e")

        keypads = self.session.design.keypads
        if not keypads:
            _empty_note(self.body, "No keypads in this design.").grid(
                row=1, column=0, pady=24)
            return
        for i, kp in enumerate(keypads):
            self._build_keypad_card(kp).grid(row=i + 1, column=0,
                                             sticky="ew", pady=4)

    def _add_clicked(self):
        prompt_add_keypad(self.winfo_toplevel(), self.session,
                          self.on_structure_change)

    def _remove_clicked(self, kp):
        if not messagebox.askyesno(
            "Remove keypad?",
            f"Remove KEYPAD #{kp.number}?\n\nSplitter outputs feeding it "
            "become Spare.",
        ):
            return
        self.on_hardware_change(
            lambda: remove_keypad(self.session.design, kp.number))

    def _build_keypad_card(self, kp) -> ctk.CTkFrame:
        card = Card(self.body)
        card.columnconfigure(1, weight=1)

        ctk.CTkLabel(card, text=f"KEYPAD #{kp.number}", text_color=theme.TEXT,
                     font=theme.mono_font(13, "bold"),
                     ).grid(row=0, column=0, sticky="w", padx=12, pady=(11, 0))

        loc_var = tk.StringVar(value=kp.location or "")

        def loc_edited(*_a, k=kp):
            k.location = loc_var.get().strip() or None
            self.on_change()

        loc_var.trace_add("write", loc_edited)
        _styled_entry(card, textvariable=loc_var,
                      placeholder_text="Keypad location",
                      ).grid(row=0, column=1, sticky="ew",
                             padx=(theme.PAD["sm"], theme.PAD["xs"]),
                             pady=(11, 0))

        remove_button(card, lambda k=kp: self._remove_clicked(k)).grid(
            row=0, column=2, padx=(0, theme.PAD["sm"]), pady=(11, 0))

        # Source is editable — a removed splitter blanks it and the
        # keypad.source_missing rule sends the tech back here.
        src_row = ctk.CTkFrame(card, fg_color="transparent")
        src_row.grid(row=1, column=0, columnspan=2, sticky="w",
                     padx=12, pady=(6, 11))
        ctk.CTkLabel(src_row, text="SOURCE", text_color=theme.TEXT_TERTIARY,
                     font=theme.ui_font(theme.SIZE["badge"], "bold"),
                     ).pack(side="left")
        choices = _keypad_source_choices(self.session)
        current = (kp.source or "").strip()
        values = choices if not current or current in choices \
            else [current] + choices
        holder, menu = _bordered_menu(
            src_row, values, lambda value, k=kp: self._set_source(k, value),
            tone=_menu_tone(current), width=200)
        menu.set(current or "— select source —")
        holder.pack(side="left", padx=(theme.PAD["sm"], 0))

        glob_var = tk.BooleanVar(value=kp.global_keypad)

        def glob_toggled(k=kp, var=glob_var):
            k.global_keypad = var.get()
            self.on_change()

        _styled_checkbox(card, "Global keypad", glob_var, glob_toggled,
                         text_color=theme.TEXT_SECOND).grid(
            row=1, column=1, columnspan=2, sticky="e", padx=(0, 12),
            pady=(0, 11))
        return card

    def _set_source(self, kp, value: str):
        kp.source = value
        self.on_change()


# ------------------------------------------------------------------ #
# POWER (RSP / power-supply pairs)                                      #
# ------------------------------------------------------------------ #

class PowerTab(ctk.CTkFrame):
    """RSP / power-supply locations and expander add/remove. An RSP and its
    power supply share a room, so one entry writes both (same contract as
    apply_location_conflict)."""

    def __init__(self, master, session: Session, on_change,
                 on_structure_change=None, on_hardware_change=None, *,
                 on_navigate: Callable[[str], None] | None = None):
        super().__init__(master, fg_color="transparent")
        self.session = session
        self.on_change = on_change
        self.on_structure_change = on_structure_change or on_change
        # Removals route through here so the editor can report cascade fallout;
        # falls back to a plain mutate + structure-refresh when unset.
        self.on_hardware_change = on_hardware_change or (
            lambda mutate: (mutate(), self.on_structure_change()))
        # Accepted for a uniform tab contract; this tab has nothing to link to.
        self.on_navigate = on_navigate
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.body.grid(row=0, column=0, sticky="nsew")
        self.body.columnconfigure(0, weight=1)
        auto_hide_scrollbar(self.body)
        self.refresh()

    def refresh(self):
        for w in self.body.winfo_children():
            w.destroy()

        design = self.session.design
        ps_by_number = {ps.number: ps for ps in design.power_supplies}

        header = ctk.CTkFrame(self.body, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        header.columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text="One location per RSP — the expander, its power supply, and "
                 "the 66 block live in the same room. Edits propagate to "
                 "every sheet.",
            font=theme.ui_font(theme.SIZE["meta"]),
            text_color=theme.TEXT_TERTIARY,
            wraplength=440, justify="left", anchor="w",
        ).grid(row=0, column=0, sticky="w")
        accent_outline_button(header, "+ Add Expander", self._add_clicked,
                              width=150).grid(row=0, column=1, sticky="e",
                                              padx=(theme.PAD["sm"], 0))

        if not design.rsps:
            _empty_note(self.body, "No RSPs in this design.").grid(
                row=1, column=0, pady=24)
            return

        for i, rsp in enumerate(design.rsps):
            self._build_rsp_card(rsp, ps_by_number).grid(
                row=i + 1, column=0, sticky="ew", pady=4)

    def _add_clicked(self):
        prompt_add_expander(self.winfo_toplevel(), self.session,
                            self.on_structure_change)

    def _remove_clicked(self, rsp):
        zr = f"Z{min(rsp.zones)}–Z{max(rsp.zones)}" if rsp.zones else "no zones"
        if not messagebox.askyesno(
            "Remove expander?",
            f"Remove RSP-{rsp.number} / PS-{rsp.number} ({rsp.model})?\n\n"
            f"This deletes its entire zone block ({zr}) including any "
            "descriptions you've entered. Splitter outputs feeding it become "
            "Spare. Module numbering keeps the gap (zone addresses are "
            "physical).",
        ):
            return
        self.on_hardware_change(
            lambda: remove_expander(self.session.design, rsp.number))

    def _build_rsp_card(self, rsp, ps_by_number) -> ctk.CTkFrame:
        card = Card(self.body)
        card.columnconfigure(1, weight=1)

        zr = f"Z{min(rsp.zones)}–Z{max(rsp.zones)}" if rsp.zones else "no zones"
        ctk.CTkLabel(card, text=f"RSP-{rsp.number} / PS-{rsp.number}",
                     text_color=theme.TEXT, font=theme.mono_font(13, "bold"),
                     ).grid(row=0, column=0, sticky="w", padx=12, pady=(11, 0))
        meta = ctk.CTkFrame(card, fg_color="transparent")
        meta.grid(row=1, column=0, sticky="w", padx=12, pady=(4, 11))
        Chip(meta, rsp.model, variant="outline", pill=False,
             size=theme.SIZE["meta"], bold=False, padx=7, pady=2).pack(side="left")
        Chip(meta, zr, variant="neutral", pill=False, mono=True,
             size=theme.SIZE["meta"], bold=False, padx=7, pady=2).pack(
            side="left", padx=(theme.PAD["xs"], 0))

        loc_var = tk.StringVar(value=rsp.location or "")

        def loc_edited(*_a, r=rsp):
            value = loc_var.get().strip() or None
            r.location = value
            ps = ps_by_number.get(r.number)
            if ps is not None:
                ps.location = value
            self.on_change()

        loc_var.trace_add("write", loc_edited)
        _styled_entry(card, textvariable=loc_var,
                      placeholder_text="RSP location",
                      ).grid(row=0, column=1, rowspan=2, sticky="ew",
                             padx=(theme.PAD["sm"], theme.PAD["xs"]),
                             pady=(11, 11))

        remove_button(card, lambda r=rsp: self._remove_clicked(r)).grid(
            row=0, column=2, rowspan=2, padx=(0, theme.PAD["sm"]))
        return card
