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
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk

from hardware import (
    HardwareError,
    add_expander,
    add_keypad,
    add_splitter,
    remove_expander,
    remove_keypad,
    remove_splitter,
    renumber_splitter,
)
from session import Session

ACCENT = "#4a7bb8"
ACCENT_HOVER = "#3a6aa8"
BANNER_BG = "#fdf3e7"


def auto_hide_scrollbar(scrollframe: ctk.CTkScrollableFrame) -> None:
    """Show a CTkScrollableFrame's scrollbar only when its content overflows.

    CustomTkinter renders the scrollbar trough permanently; on a roomy window
    an un-scrollable panel reads as a random empty toolbar.
    """
    scrollbar = scrollframe._scrollbar
    canvas = scrollframe._parent_canvas

    def update(_event=None):
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
# Add-hardware dialogs (shared)                                         #
# ------------------------------------------------------------------ #

def _dialog_shell(root, title: str) -> ctk.CTkToplevel:
    win = ctk.CTkToplevel(root)
    win.title(title)
    win.transient(root)
    win.grab_set()
    win.resizable(False, False)
    return win


def _dialog_buttons(win, confirm_text: str, on_confirm) -> None:
    row = ctk.CTkFrame(win, fg_color="transparent")
    row.pack(fill="x", padx=20, pady=(8, 16))
    row.columnconfigure(0, weight=1)
    row.columnconfigure(1, weight=1)
    ctk.CTkButton(row, text="Cancel", height=34, fg_color="transparent",
                  border_width=1, border_color="gray60",
                  text_color=("gray30", "gray80"),
                  hover_color=("gray90", "gray25"),
                  command=lambda: (win.grab_release(), win.destroy()),
                  ).grid(row=0, column=0, sticky="ew", padx=(0, 6))
    ctk.CTkButton(row, text=confirm_text, height=34, fg_color=ACCENT,
                  hover_color=ACCENT_HOVER, font=ctk.CTkFont(weight="bold"),
                  command=on_confirm,
                  ).grid(row=0, column=1, sticky="ew", padx=(6, 0))


def prompt_add_expander(root, session: Session, on_done) -> None:
    win = _dialog_shell(root, "Add expander")
    ctk.CTkLabel(win, text="Add a 714 expander (RSP + power supply + zone block)",
                 font=ctk.CTkFont(size=13, weight="bold"),
                 ).pack(anchor="w", padx=20, pady=(16, 8))

    model_var = tk.StringVar(value="714-16")
    for model, label in [("714-16", "714-16  (16 points)"),
                         ("714-8", "714-8  (8 points)")]:
        ctk.CTkRadioButton(win, text=label, variable=model_var, value=model,
                           ).pack(anchor="w", padx=28, pady=3)

    loc = ctk.CTkEntry(win, width=320, height=34,
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
    ctk.CTkLabel(win, text="Add a 710 splitter-repeater",
                 font=ctk.CTkFont(size=13, weight="bold"),
                 ).pack(anchor="w", padx=20, pady=(16, 8))

    type_var = tk.StringVar(value="LX")
    for stype, label in [("LX", "LX bus  (710-LX500-N — RSP feeds)"),
                         ("KP", "KP bus  (710-KP-N — keypad feeds)")]:
        ctk.CTkRadioButton(win, text=label, variable=type_var, value=stype,
                           ).pack(anchor="w", padx=28, pady=3)

    loc = ctk.CTkEntry(win, width=320, height=34, placeholder_text="Location")
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
    ctk.CTkLabel(win, text="Add a keypad",
                 font=ctk.CTkFont(size=13, weight="bold"),
                 ).pack(anchor="w", padx=20, pady=(16, 8))

    loc = ctk.CTkEntry(win, width=320, height=34, placeholder_text="Location")
    loc.pack(padx=20, pady=(2, 8))

    source_menu = ctk.CTkOptionMenu(
        win, values=_keypad_source_choices(session), width=320, height=32,
        fg_color=("gray90", "gray25"), text_color=("gray15", "gray90"),
        button_color=ACCENT, button_hover_color=ACCENT_HOVER,
    )
    source_menu.pack(padx=20)

    glob_var = tk.BooleanVar(value=False)
    ctk.CTkCheckBox(win, text="Global keypad", variable=glob_var,
                    checkmark_color="white", fg_color=ACCENT,
                    hover_color=ACCENT_HOVER,
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


def _add_button(parent, text: str, command) -> ctk.CTkButton:
    return ctk.CTkButton(parent, text=text, height=30, width=150,
                         fg_color="transparent", border_width=1,
                         border_color=ACCENT, text_color=ACCENT,
                         hover_color=("gray90", "gray25"), command=command)


def _remove_button(parent, command) -> ctk.CTkButton:
    return ctk.CTkButton(parent, text="✕", width=28, height=24,
                         fg_color="transparent", text_color="gray50",
                         hover_color=("#f3dada", "gray25"), command=command)


# ------------------------------------------------------------------ #
# SPLITTERS                                                             #
# ------------------------------------------------------------------ #

class SplittersTab(ctk.CTkFrame):
    def __init__(self, master, session: Session, on_change,
                 on_structure_change=None):
        super().__init__(master, fg_color="transparent")
        self.session = session
        self.on_change = on_change
        self.on_structure_change = on_structure_change or on_change
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
        row = 0
        for conflict in [c for c in self.session.design.conflicts
                         if getattr(c, "kind", "") == "RSP"]:
            self._build_conflict_banner(conflict).grid(
                row=row, column=0, sticky="ew", pady=(0, 8))
            row += 1
        self._build_header_row().grid(row=row, column=0, sticky="ew", pady=(0, 8))
        row += 1
        design = self.session.design
        if not design.splitters:
            ctk.CTkLabel(self.body, text="No splitters in this design.",
                         text_color="gray50").grid(row=row, column=0, pady=24)
            return
        for splitter in design.splitters:
            self._build_splitter_card(splitter).grid(
                row=row, column=0, sticky="ew", pady=4)
            row += 1

    # ---- conflict banners ----

    def _build_conflict_banner(self, conflict) -> ctk.CTkFrame:
        card = ctk.CTkFrame(self.body, corner_radius=8, fg_color=BANNER_BG,
                            border_width=1, border_color="#e8b87c")
        card.columnconfigure(0, weight=1)
        ctk.CTkLabel(
            card, text=f"⚠  The CAD prints disagree: {conflict.label}",
            font=ctk.CTkFont(size=12, weight="bold"), anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 2))

        value_var = tk.StringVar(value=conflict.options[0][0])
        r = 1
        for value, source in conflict.options:
            ctk.CTkRadioButton(
                card, text=f"{value}    ({source})", variable=value_var, value=value,
                font=ctk.CTkFont(size=11),
            ).grid(row=r, column=0, sticky="w", padx=20, pady=2)
            r += 1
        custom = ctk.CTkEntry(card, placeholder_text="…or type the correct value",
                              height=30)
        custom.grid(row=r, column=0, sticky="ew", padx=20, pady=(4, 6))

        def resolve():
            from generate_dmp_ws import apply_location_conflict
            chosen = custom.get().strip() or value_var.get()
            apply_location_conflict(self.session.design, conflict, chosen)
            self.session.design.conflicts.remove(conflict)
            self.on_change()
            self.refresh()

        ctk.CTkButton(
            card, text="Use this value", height=30, width=140,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, command=resolve,
        ).grid(row=r + 1, column=0, sticky="w", padx=20, pady=(0, 10))
        return card

    # ---- header: reviewed checkbox + add button ----

    def _build_header_row(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.body, fg_color="transparent")
        frame.columnconfigure(0, weight=1)
        source = self.session.design.topology_source or "auto-derived"
        if source == "riser":
            note = "Wiring below was read from the riser diagram."
        else:
            note = ("Riser extraction was incomplete — the wiring below is a "
                    "best-guess convention. Check it against the riser diagram.")
        ctk.CTkLabel(frame, text=note, font=ctk.CTkFont(size=11),
                     text_color="gray50", anchor="w", wraplength=480,
                     justify="left").grid(row=0, column=0, sticky="w")

        _add_button(frame, "+ Add Splitter", self._add_clicked).grid(
            row=0, column=1, rowspan=2, sticky="e", padx=(8, 0))

        self._reviewed_var = tk.BooleanVar(value=self.session.topology_confirmed)

        def toggled():
            self.session.topology_confirmed = self._reviewed_var.get()
            self.on_change()

        ctk.CTkCheckBox(
            frame, text="Wiring reviewed against the riser diagram",
            variable=self._reviewed_var, command=toggled,
            font=ctk.CTkFont(size=12, weight="bold"),
            checkmark_color="white", fg_color=ACCENT, hover_color=ACCENT_HOVER,
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))
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
        remove_splitter(self.session.design, splitter.id)
        self.on_structure_change()

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
        card = ctk.CTkFrame(self.body, corner_radius=8, fg_color=("gray97", "gray20"))
        card.columnconfigure(1, weight=1)

        id_row = ctk.CTkFrame(card, fg_color="transparent")
        id_row.grid(row=0, column=0, sticky="w", padx=12, pady=(10, 0))
        prefix = "710-LX500-" if splitter.splitter_type == "LX" else "710-KP-"
        ctk.CTkLabel(id_row, text=prefix,
                     font=ctk.CTkFont(size=12, weight="bold")).pack(side="left")
        num_entry = ctk.CTkEntry(id_row, width=44, height=28, justify="center",
                                 font=ctk.CTkFont(size=12, weight="bold"))
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
        ctk.CTkEntry(card, textvariable=loc_var, height=30,
                     placeholder_text="Splitter location",
                     ).grid(row=0, column=1, sticky="ew", padx=(8, 4), pady=(10, 0))

        _remove_button(card, lambda s=splitter: self._remove_clicked(s)).grid(
            row=0, column=2, padx=(0, 8), pady=(10, 0))

        first_input = next((v for v in splitter.inputs.values() if v), "")
        ctk.CTkLabel(card, text="input:", anchor="w",
                     font=ctk.CTkFont(size=11), text_color="gray50",
                     ).grid(row=1, column=0, sticky="w", padx=(12, 0), pady=(0, 2))
        in_var = tk.StringVar(value=first_input)
        in_var.trace_add("write", lambda *_a, s=splitter, v=in_var:
                         self._set_input(s, v.get()))
        ctk.CTkComboBox(card, variable=in_var, height=28,
                        values=self._input_choices(splitter),
                        button_color=ACCENT, button_hover_color=ACCENT_HOVER,
                        ).grid(row=1, column=1, sticky="ew", padx=(8, 4), pady=(0, 2))

        choices = self._output_choices(splitter)
        outs = list(splitter.outputs or [])
        for i in range(3):
            current = outs[i] if i < len(outs) else "Spare"
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.grid(row=2 + i, column=0, columnspan=3, sticky="ew",
                     padx=20, pady=2)
            ctk.CTkLabel(row, text=f"Output {i + 1}:", width=80, anchor="w",
                         font=ctk.CTkFont(size=11)).pack(side="left")
            values = list(choices) if current in choices else [current] + list(choices)
            menu = ctk.CTkOptionMenu(
                row, values=values, width=340, height=28,
                fg_color=("gray90", "gray25"), text_color=("gray15", "gray90"),
                button_color=ACCENT, button_hover_color=ACCENT_HOVER,
                command=lambda value, s=splitter, idx=i: self._set_output(s, idx, value),
            )
            menu.set(current)
            menu.pack(side="left", padx=(6, 0))
        ctk.CTkLabel(card, text="", height=4).grid(row=5, column=0, pady=(0, 2))
        return card

    def _set_input(self, splitter, value: str):
        val = value.strip()
        # preserve the existing input key if present, else derive from bus type
        key = next(iter(splitter.inputs), None) or (
            "LX-Bus In" if splitter.splitter_type == "LX" else "KP-Bus In")
        splitter.inputs = {key: val} if val else {}
        self.on_change()

    def _set_output(self, splitter, index: int, value: str):
        outs = list(splitter.outputs or [])
        while len(outs) <= index:
            outs.append("Spare")
        outs[index] = value
        splitter.outputs = outs
        self.on_change()


# ------------------------------------------------------------------ #
# KEYPADS                                                               #
# ------------------------------------------------------------------ #

class KeypadsTab(ctk.CTkFrame):
    def __init__(self, master, session: Session, on_change,
                 on_structure_change=None):
        super().__init__(master, fg_color="transparent")
        self.session = session
        self.on_change = on_change
        self.on_structure_change = on_structure_change or on_change
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
        _add_button(header, "+ Add Keypad", self._add_clicked).grid(
            row=0, column=1, sticky="e")

        keypads = self.session.design.keypads
        if not keypads:
            ctk.CTkLabel(self.body, text="No keypads in this design.",
                         text_color="gray50").grid(row=1, column=0, pady=24)
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
        remove_keypad(self.session.design, kp.number)
        self.on_structure_change()

    def _build_keypad_card(self, kp) -> ctk.CTkFrame:
        card = ctk.CTkFrame(self.body, corner_radius=8, fg_color=("gray97", "gray20"))
        card.columnconfigure(1, weight=1)

        ctk.CTkLabel(card, text=f"KEYPAD #{kp.number}",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 0))

        loc_var = tk.StringVar(value=kp.location or "")

        def loc_edited(*_a, k=kp, var=None):
            k.location = loc_var.get().strip() or None
            self.on_change()

        loc_var.trace_add("write", loc_edited)
        ctk.CTkEntry(card, textvariable=loc_var, height=30,
                     placeholder_text="Keypad location",
                     ).grid(row=0, column=1, sticky="ew", padx=(8, 4), pady=(10, 0))

        _remove_button(card, lambda k=kp: self._remove_clicked(k)).grid(
            row=0, column=2, padx=(0, 8), pady=(10, 0))

        # Source is editable — a removed splitter blanks it and the
        # keypad.source_missing rule sends the tech back here.
        src_row = ctk.CTkFrame(card, fg_color="transparent")
        src_row.grid(row=1, column=0, columnspan=2, sticky="w",
                     padx=12, pady=(2, 8))
        ctk.CTkLabel(src_row, text="source:", font=ctk.CTkFont(size=11),
                     text_color="gray50").pack(side="left")
        choices = _keypad_source_choices(self.session)
        current = (kp.source or "").strip()
        values = choices if not current or current in choices \
            else [current] + choices
        menu = ctk.CTkOptionMenu(
            src_row, values=values, width=200, height=26,
            fg_color=("gray90", "gray25"), text_color=("gray15", "gray90"),
            button_color=ACCENT, button_hover_color=ACCENT_HOVER,
            command=lambda value, k=kp: self._set_source(k, value),
        )
        menu.set(current or "— select source —")
        menu.pack(side="left", padx=(6, 0))

        glob_var = tk.BooleanVar(value=kp.global_keypad)

        def glob_toggled(k=kp, var=glob_var):
            k.global_keypad = var.get()
            self.on_change()

        ctk.CTkCheckBox(
            card, text="Global keypad", variable=glob_var, command=glob_toggled,
            font=ctk.CTkFont(size=11),
            checkmark_color="white", fg_color=ACCENT, hover_color=ACCENT_HOVER,
        ).grid(row=1, column=1, columnspan=2, sticky="e", padx=(0, 12), pady=(0, 8))
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
                 on_structure_change=None):
        super().__init__(master, fg_color="transparent")
        self.session = session
        self.on_change = on_change
        self.on_structure_change = on_structure_change or on_change
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
            font=ctk.CTkFont(size=11), text_color="gray50",
            wraplength=400, justify="left", anchor="w",
        ).grid(row=0, column=0, sticky="w")
        _add_button(header, "+ Add Expander", self._add_clicked).grid(
            row=0, column=1, sticky="e", padx=(8, 0))

        if not design.rsps:
            ctk.CTkLabel(self.body, text="No RSPs in this design.",
                         text_color="gray50").grid(row=1, column=0, pady=24)
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
        remove_expander(self.session.design, rsp.number)
        self.on_structure_change()

    def _build_rsp_card(self, rsp, ps_by_number) -> ctk.CTkFrame:
        card = ctk.CTkFrame(self.body, corner_radius=8, fg_color=("gray97", "gray20"))
        card.columnconfigure(1, weight=1)

        zr = f"Z{min(rsp.zones)}–Z{max(rsp.zones)}" if rsp.zones else "no zones"
        ctk.CTkLabel(card, text=f"RSP-{rsp.number} / PS-{rsp.number}",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 0))
        ctk.CTkLabel(card, text=f"{rsp.model} · {zr}", font=ctk.CTkFont(size=11),
                     text_color="gray50",
                     ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 8))

        loc_var = tk.StringVar(value=rsp.location or "")

        def loc_edited(*_a, r=rsp):
            value = loc_var.get().strip() or None
            r.location = value
            ps = ps_by_number.get(r.number)
            if ps is not None:
                ps.location = value
            self.on_change()

        loc_var.trace_add("write", loc_edited)
        ctk.CTkEntry(card, textvariable=loc_var, height=30,
                     placeholder_text="RSP location",
                     ).grid(row=0, column=1, rowspan=2, sticky="ew",
                            padx=(8, 4), pady=(10, 8))

        _remove_button(card, lambda r=rsp: self._remove_clicked(r)).grid(
            row=0, column=2, rowspan=2, padx=(0, 8))
        return card
