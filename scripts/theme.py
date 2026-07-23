"""Design tokens for the "Refined C1" UI — the single source of colour truth.

Every colour is a ``(light, dark)`` tuple, which is exactly what CustomTkinter
widgets accept, so the appearance toggle is one ``set_mode()`` call and every
CTk widget follows automatically.

Raw-Tk surfaces (ttk.Treeview, tk.Listbox, the tooltip/toast Toplevels) can't
take a tuple and don't observe CustomTkinter's appearance mode at all. Those
call ``resolve(TOKEN)`` for the currently-active member and re-style themselves
from an ``on_mode_change`` subscription.

The values come from the design handoff's token table. Where the handoff gives
a dark value as ``rgba(...)``, it is pre-flattened here against the surface it
sits on — Tk has no alpha channel.
"""

from __future__ import annotations

import sys
from typing import Callable

import customtkinter as ctk

Token = tuple[str, str]  # (light, dark)

# --------------------------------------------------------------------------- #
# Palette                                                                       #
# --------------------------------------------------------------------------- #

APP_BG: Token = ("#f4f5f7", "#15181c")
SURFACE: Token = ("#ffffff", "#1d2126")
SURFACE_SUBTLE: Token = ("#fafbfc", "#181c21")   # table headers
SURFACE_CHIP: Token = ("#f0f2f5", "#22262c")     # chips, quiet fills
CHROME: Token = ("#ffffff", "#1a1e23")           # header + footer bars

BORDER: Token = ("#e3e6ea", "#2a3037")
BORDER_STRONG: Token = ("#d4dae1", "#3a4149")    # inputs, secondary buttons
HAIRLINE: Token = ("#f0f2f5", "#21262c")         # row separators

TEXT: Token = ("#1b2430", "#e8eaed")
TEXT_SECOND: Token = ("#5c6570", "#9aa4af")
TEXT_TERTIARY: Token = ("#8b95a1", "#6e7883")

ACCENT: Token = ("#4a7bb8", "#6ba3e0")
ACCENT_HOVER: Token = ("#3a6aa8", "#84b4e8")
ON_ACCENT: Token = ("#ffffff", "#0d1420")
ACCENT_TINT: Token = ("#eef3f9", "#1c2733")

# Semantic families. The dark tints are the handoff's rgba() overlays flattened
# against SURFACE dark (#1d2126).
SUCCESS: Token = ("#2f855a", "#3fb950")
SUCCESS_TINT: Token = ("#e9f4ee", "#21332b")     # rgba(63,185,80,.12)
WARNING: Token = ("#c05621", "#d29922")
WARNING_TINT: Token = ("#faeee6", "#2f2d26")     # rgba(210,153,34,.10)
WARNING_ROW: Token = ("#fdf6ec", "#2a2926")      # rgba(210,153,34,.07)
ERROR: Token = ("#c0392b", "#e5534b")
ERROR_TINT: Token = ("#fdf1f1", "#35272a")       # rgba(229,83,75,.12)
ERROR_BORDER: Token = ("#f3d3d3", "#5a3230")

# Riser-warning banner. Flattened against APP_BG dark (#15181c) — the banner
# sits on the tab content surface, not on a card.
BANNER_BG: Token = ("#fdf3e7", "#24221c")        # rgba(210,153,34,.08)
BANNER_BORDER: Token = ("#e8b87c", "#614c1e")    # rgba(210,153,34,.35)
BANNER_TEXT: Token = ("#7a4a1f", "#e0c68a")

ROW_HOVER: Token = ("#f4f8fc", "#22262c")
# The topology tree's root node: a solid chip in both modes.
NODE_ROOT_BG: Token = ("#3d4854", "#384656")
NODE_ROOT_TEXT: Token = ("#ffffff", "#e8eaed")

# Hover fill for ghost / secondary buttons ("darken the bg one step").
HOVER_SUBTLE: Token = ("#f0f2f5", "#262b32")
HOVER_DANGER: Token = ("#fdf1f1", "#35272a")

# Text sitting on a solid warning/error fill (the tab bar's error count pill).
ON_WARNING: Token = ("#ffffff", "#15181c")

# Overlays. The tooltip is deliberately inverted against the page in both
# modes so it reads as floating above it; the scrim dims the editor behind the
# pre-generate sheet. Tk has no alpha, so the scrim is an opaque near-black
# rather than the spec's translucent wash.
TOOLTIP_BG: Token = ("#1c1c1e", "#2f353d")
TOOLTIP_TEXT: Token = ("#ffffff", "#e8eaed")
SCRIM: Token = ("#3d4854", "#0d1013")

# --------------------------------------------------------------------------- #
# Scales                                                                        #
# --------------------------------------------------------------------------- #

# Tk font sizes are integers, so the handoff's half-point sizes are rounded to
# the nearest point. The *relationships* are what matter and they survive.
SIZE = {
    "drop_title": 15,   # 15/600 drop-zone title
    "title": 14,        # 13.5/700 project name
    "body": 13,         # body text, grid cells
    "control": 12,      # 12.5/600-700 buttons and tabs
    "chip": 12,         # 11.5 chips
    "meta": 11,         # 11 secondary meta lines
    "label": 11,        # 10.5/700 uppercase section + table headers
    "badge": 10,        # 9.5/700 count badges
}

RADIUS = {
    "card": 10,
    "button": 7,
    "control": 6,
    "chip": 5,
    "pill": 20,
    "tag": 4,
}

HEIGHT = {
    "header": 52,
    "tabbar": 34,
    "row": 31,
    "footer": 52,
    "button": 34,
    "button_sm": 28,
    "button_md": 30,
    "control": 26,
    "input": 32,
    "statusbar": 26,
}

# Spacing scale: 4 / 8 / 12 / 16.
PAD = {"xs": 4, "sm": 8, "md": 12, "lg": 16}

# UI font: the platform system face. Passing family=None lets CustomTkinter pick
# its own system default, which is already correct on every platform we ship.
if sys.platform == "darwin":
    MONO_FAMILY = "Menlo"       # SF Mono isn't reliably exposed to Tk by name
elif sys.platform == "win32":
    MONO_FAMILY = "Consolas"
else:
    MONO_FAMILY = "DejaVu Sans Mono"


def ui_font(size: int = SIZE["body"], weight: str = "normal") -> ctk.CTkFont:
    """A system-UI font at `size`. `weight` is "normal" or "bold"."""
    return ctk.CTkFont(size=size, weight=weight)


def mono_font(size: int = SIZE["body"], weight: str = "normal") -> ctk.CTkFont:
    """A monospace font — zone ids, splitter ids, the topology tree."""
    return ctk.CTkFont(family=MONO_FAMILY, size=size, weight=weight)


def tracked(text: str) -> str:
    """Approximate the spec's +.06em letter-spacing on uppercase section labels.

    Tk fonts have no tracking attribute, so we interleave thin spaces. Applied
    only to short all-caps labels, where the spec asks for it and where the
    hack stays invisible.
    """
    return " ".join(text.upper())


# --------------------------------------------------------------------------- #
# Appearance mode                                                               #
# --------------------------------------------------------------------------- #

_MODE: str = "light"
_SUBSCRIBERS: list[Callable[[str], None]] = []


def current_mode() -> str:
    """"light" or "dark" — never "system"; set_mode() resolves that up front."""
    return _MODE


def resolve(token: Token | str) -> str:
    """The active member of a token, for widgets that can't take a tuple."""
    if isinstance(token, str):
        return token
    return token[1] if _MODE == "dark" else token[0]


def set_mode(mode: str) -> None:
    """Switch the whole app between the two token sets.

    Accepts "light", "dark", or "system" (resolved immediately, because
    resolve() must always return a concrete colour).
    """
    global _MODE
    if mode == "system":
        ctk.set_appearance_mode("system")
        resolved = ctk.get_appearance_mode().lower()
        _MODE = "dark" if resolved == "dark" else "light"
    else:
        _MODE = "dark" if mode == "dark" else "light"
        ctk.set_appearance_mode(_MODE)
    for callback in list(_SUBSCRIBERS):
        try:
            callback(_MODE)
        except Exception:
            # A dead widget's restyle must never take the toggle down with it.
            pass


def on_mode_change(callback: Callable[[str], None]) -> Callable[[str], None]:
    """Subscribe to mode switches. Returns the callback, so it can be stashed
    for a matching off_mode_change() on <Destroy>."""
    _SUBSCRIBERS.append(callback)
    return callback


def off_mode_change(callback: Callable[[str], None]) -> None:
    """Unsubscribe. Safe to call for a callback that isn't registered."""
    while callback in _SUBSCRIBERS:
        _SUBSCRIBERS.remove(callback)


def bind_mode_change(widget, callback: Callable[[str], None]) -> None:
    """Subscribe `callback` and auto-unsubscribe when `widget` is destroyed.

    The registry holds strong references, so an un-unsubscribed callback keeps
    a torn-down tab tree alive and restyles dead widgets on the next toggle.
    """
    on_mode_change(callback)
    widget.bind("<Destroy>", lambda _e: off_mode_change(callback), add="+")
