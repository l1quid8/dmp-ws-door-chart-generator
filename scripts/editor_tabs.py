"""SPLITTERS / KEYPADS / POWER tabs of the project editor.

Form-style editors over the corresponding DMPDesign lists. The SPLITTERS tab
absorbs the two pre-generation modal dialogs: unresolved source-data
conflicts render as banner cards that can be resolved (or deferred — they
block finalize, not editing), and the splitter wiring editor that used to be
a one-shot confirmation is now permanently editable with a 'reviewed'
checkbox feeding the topology.unconfirmed validation rule.
"""

from __future__ import annotations

import tkinter as tk

import customtkinter as ctk

from session import Session

ACCENT = "#4a7bb8"
ACCENT_HOVER = "#3a6aa8"
BANNER_BG = "#fdf3e7"


class SplittersTab(ctk.CTkFrame):
    def __init__(self, master, session: Session, on_change):
        super().__init__(master, fg_color="transparent")
        self.session = session
        self.on_change = on_change
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.body = ctk.CTkScrollableFrame(self, fg_color="transparent", height=330)
        self.body.grid(row=0, column=0, sticky="nsew")
        self.body.columnconfigure(0, weight=1)
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
        self._build_reviewed_row().grid(row=row, column=0, sticky="ew", pady=(0, 8))
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

    # ---- conflict banners (was _show_conflict_dialog) ----

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

    # ---- wiring reviewed checkbox ----

    def _build_reviewed_row(self) -> ctk.CTkFrame:
        frame = ctk.CTkFrame(self.body, fg_color="transparent")
        source = self.session.design.topology_source or "auto-derived"
        if source == "riser":
            note = "Wiring below was read from the riser diagram."
        else:
            note = ("Riser extraction was incomplete — the wiring below is a "
                    "best-guess convention. Check it against the riser diagram.")
        ctk.CTkLabel(frame, text=note, font=ctk.CTkFont(size=11),
                     text_color="gray50", anchor="w", wraplength=480,
                     justify="left").grid(row=0, column=0, sticky="w")

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

    # ---- splitter cards (was _show_topology_dialog) ----

    def _output_choices(self, splitter) -> list[str]:
        design = self.session.design
        rsp_names = [f"RSP-{r.number}" for r in design.rsps]
        kp_names = [f"KEYPAD #{k.number}" for k in design.keypads if k.number != 1]
        others = [f"To {o.id}" for o in design.splitters if o.id != splitter.id]
        return ["Spare"] + rsp_names + kp_names + others

    def _build_splitter_card(self, splitter) -> ctk.CTkFrame:
        card = ctk.CTkFrame(self.body, corner_radius=8, fg_color=("gray97", "gray20"))
        card.columnconfigure(1, weight=1)

        ctk.CTkLabel(card, text=splitter.id,
                     font=ctk.CTkFont(size=12, weight="bold"),
                     ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 0))

        loc_var = tk.StringVar(value=splitter.location or "")

        def loc_edited(*_a):
            splitter.location = loc_var.get().strip() or None
            self.on_change()

        loc_var.trace_add("write", loc_edited)
        ctk.CTkEntry(card, textvariable=loc_var, height=30,
                     placeholder_text="Splitter location",
                     ).grid(row=0, column=1, sticky="ew", padx=(8, 12), pady=(10, 0))

        first_input = next((v for v in splitter.inputs.values() if v), "")
        ctk.CTkLabel(card, text=f"input: {first_input or '—'}",
                     font=ctk.CTkFont(size=11), text_color="gray50",
                     ).grid(row=1, column=0, columnspan=2, sticky="w",
                            padx=12, pady=(0, 2))

        choices = self._output_choices(splitter)
        outs = list(splitter.outputs or [])
        for i in range(3):
            current = outs[i] if i < len(outs) else "Spare"
            row = ctk.CTkFrame(card, fg_color="transparent")
            row.grid(row=2 + i, column=0, columnspan=2, sticky="ew",
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

    def _set_output(self, splitter, index: int, value: str):
        outs = list(splitter.outputs or [])
        while len(outs) <= index:
            outs.append("Spare")
        outs[index] = value
        splitter.outputs = outs
        self.on_change()


class KeypadsTab(ctk.CTkFrame):
    def __init__(self, master, session: Session, on_change):
        super().__init__(master, fg_color="transparent")
        self.session = session
        self.on_change = on_change
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        body = ctk.CTkScrollableFrame(self, fg_color="transparent", height=330)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)

        keypads = self.session.design.keypads
        if not keypads:
            ctk.CTkLabel(body, text="No keypads in this design.",
                         text_color="gray50").grid(row=0, column=0, pady=24)
            return
        for i, kp in enumerate(keypads):
            card = ctk.CTkFrame(body, corner_radius=8, fg_color=("gray97", "gray20"))
            card.grid(row=i, column=0, sticky="ew", pady=4)
            card.columnconfigure(1, weight=1)

            ctk.CTkLabel(card, text=f"KEYPAD #{kp.number}",
                         font=ctk.CTkFont(size=12, weight="bold"),
                         ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 0))

            loc_var = tk.StringVar(value=kp.location or "")

            def make_loc_cb(k, var):
                def cb(*_a):
                    k.location = var.get().strip() or None
                    self.on_change()
                return cb

            loc_var.trace_add("write", make_loc_cb(kp, loc_var))
            ctk.CTkEntry(card, textvariable=loc_var, height=30,
                         placeholder_text="Keypad location",
                         ).grid(row=0, column=1, sticky="ew", padx=(8, 12), pady=(10, 0))

            ctk.CTkLabel(card, text=f"source: {kp.source or '—'}",
                         font=ctk.CTkFont(size=11), text_color="gray50",
                         ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 8))

            glob_var = tk.BooleanVar(value=kp.global_keypad)

            def make_glob_cb(k, var):
                def cb():
                    k.global_keypad = var.get()
                    self.on_change()
                return cb

            ctk.CTkCheckBox(
                card, text="Global keypad", variable=glob_var,
                command=make_glob_cb(kp, glob_var), font=ctk.CTkFont(size=11),
                checkmark_color="white", fg_color=ACCENT, hover_color=ACCENT_HOVER,
            ).grid(row=1, column=1, sticky="e", padx=(0, 12), pady=(0, 8))


class PowerTab(ctk.CTkFrame):
    """RSP / power-supply locations. An RSP and its power supply share a room,
    so one entry writes both (same contract as apply_location_conflict)."""

    def __init__(self, master, session: Session, on_change):
        super().__init__(master, fg_color="transparent")
        self.session = session
        self.on_change = on_change
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        body = ctk.CTkScrollableFrame(self, fg_color="transparent", height=330)
        body.grid(row=0, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)

        design = self.session.design
        ps_by_number = {ps.number: ps for ps in design.power_supplies}
        if not design.rsps:
            ctk.CTkLabel(body, text="No RSPs in this design.",
                         text_color="gray50").grid(row=0, column=0, pady=24)
            return

        ctk.CTkLabel(
            body,
            text="One location per RSP — the expander, its power supply, and the "
                 "66 block live in the same room. Edits propagate to every sheet.",
            font=ctk.CTkFont(size=11), text_color="gray50",
            wraplength=480, justify="left", anchor="w",
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))

        for i, rsp in enumerate(design.rsps):
            card = ctk.CTkFrame(body, corner_radius=8, fg_color=("gray97", "gray20"))
            card.grid(row=i + 1, column=0, sticky="ew", pady=4)
            card.columnconfigure(1, weight=1)

            zr = f"Z{min(rsp.zones)}–Z{max(rsp.zones)}" if rsp.zones else "no zones"
            ctk.CTkLabel(card, text=f"RSP-{rsp.number} / PS-{rsp.number}",
                         font=ctk.CTkFont(size=12, weight="bold"),
                         ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 0))
            ctk.CTkLabel(card, text=zr, font=ctk.CTkFont(size=11),
                         text_color="gray50",
                         ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 8))

            loc_var = tk.StringVar(value=rsp.location or "")

            def make_cb(r, var):
                def cb(*_a):
                    value = var.get().strip() or None
                    r.location = value
                    ps = ps_by_number.get(r.number)
                    if ps is not None:
                        ps.location = value
                    self.on_change()
                return cb

            loc_var.trace_add("write", make_cb(rsp, loc_var))
            ctk.CTkEntry(card, textvariable=loc_var, height=30,
                         placeholder_text="RSP location",
                         ).grid(row=0, column=1, rowspan=2, sticky="ew",
                                padx=(8, 12), pady=(10, 8))
