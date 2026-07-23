"""Small reusable Tk/CustomTkinter widgets the toolkit doesn't ship.

Two groups live here:

*Behavioral* — widgets that add an interaction CustomTkinter lacks:
- AutocompleteEntry: a CTkEntry that filters a suggestion list as you type and
  offers it in a dropdown listbox. Used by the hardware add-dialogs so a tech
  can reuse a location already typed elsewhere instead of retyping (and risking
  a typo that splits one room across two strings).
- attach_tooltip: hover help for any widget, for the controls whose meaning
  isn't obvious without the README (the ZONES filter chips, the wiring-reviewed
  checkbox).

*Presentational* — the design system's building blocks (Chip, Card, IconTile,
SectionLabel, ModeToggle, the button factories). Every screen composes these
rather than re-deriving colors, so a token change in `theme.py` lands
everywhere at once.

Both keep their popups inside the owning toplevel (place() / a transient
Toplevel) so they coexist with the modal grab the add-dialogs already hold.
"""

from __future__ import annotations

import sys
import tkinter as tk
from typing import Callable

import customtkinter as ctk

import theme

# Keys that move within the popup / dismiss it rather than re-filter.
_NAV_KEYS = {"Up", "Down", "Return", "Escape", "Tab", "Left", "Right"}


class AutocompleteEntry(ctk.CTkEntry):
    """A CTkEntry with a type-ahead dropdown over `suggestions`.

    `suggestions` may be a list or a zero-arg callable returning a list, so the
    options can be recomputed lazily (locations change as the user edits other
    cards). Free text is always allowed — the dropdown only assists, it never
    constrains the value, so `.get()` behaves exactly like a plain entry.
    """

    def __init__(self, master, suggestions: Callable[[], list[str]] | list[str],
                 *, max_visible: int = 6, **entry_kw):
        entry_kw.setdefault("border_color", theme.BORDER_STRONG)
        entry_kw.setdefault("corner_radius", theme.RADIUS["button"])
        super().__init__(master, **entry_kw)
        self._suggestions = suggestions
        self._max_visible = max_visible
        self._popup: tk.Listbox | None = None

        self.bind("<KeyRelease>", self._on_key_release)
        self.bind("<Down>", self._focus_popup)
        self.bind("<Escape>", lambda _e: self._hide())
        self.bind("<FocusOut>", self._on_focus_out)
        self.bind("<Destroy>", lambda _e: self._hide())

    # -- suggestion source --------------------------------------------------

    def _all_suggestions(self) -> list[str]:
        src = self._suggestions
        return list(src() if callable(src) else src)

    def _matches(self, typed: str) -> list[str]:
        typed = typed.strip().lower()
        if not typed:
            return []
        # Substring match, prefix hits first; drop an exact-and-only match
        # (nothing left to suggest).
        hits = [s for s in self._all_suggestions() if typed in s.lower()]
        hits.sort(key=lambda s: (not s.lower().startswith(typed), s.lower()))
        if len(hits) == 1 and hits[0].lower() == typed:
            return []
        return hits

    # -- popup lifecycle ----------------------------------------------------

    def _on_key_release(self, event):
        if event.keysym in _NAV_KEYS:
            return
        self._show(self._matches(self.get()))

    def _show(self, items: list[str]):
        if not items:
            self._hide()
            return
        top = self.winfo_toplevel()
        if self._popup is None:
            # A raw tk.Listbox: it can't take (light, dark) tuples and doesn't
            # follow CustomTkinter's appearance mode, so resolve() every color.
            self._popup = tk.Listbox(
                top, activestyle="none", highlightthickness=1,
                highlightcolor=theme.resolve(theme.ACCENT),
                relief="solid", borderwidth=1,
                bg=theme.resolve(theme.SURFACE),
                fg=theme.resolve(theme.TEXT),
                selectbackground=theme.resolve(theme.ACCENT),
                selectforeground=theme.resolve(theme.ON_ACCENT),
                font=("", 12), exportselection=False,
            )
            self._popup.bind("<Return>", lambda _e: self._accept())
            self._popup.bind("<Double-Button-1>", lambda _e: self._accept())
            self._popup.bind("<Escape>", lambda _e: (self._hide(), self.focus_set()))
        self._popup.delete(0, "end")
        for it in items:
            self._popup.insert("end", it)
        self._popup.configure(height=min(len(items), self._max_visible))

        # Place just under the entry, in toplevel-relative coordinates so the
        # dialog's grab still feeds it events.
        x = self.winfo_rootx() - top.winfo_rootx()
        y = self.winfo_rooty() - top.winfo_rooty() + self.winfo_height()
        self._popup.place(x=x, y=y, width=self.winfo_width())
        self._popup.lift()

    def _hide(self):
        if self._popup is not None:
            self._popup.destroy()
            self._popup = None

    def _on_focus_out(self, _event):
        # Don't dismiss when focus moved into our own listbox; re-check shortly.
        self.after(120, self._maybe_hide_after_focus)

    def _maybe_hide_after_focus(self):
        if self._popup is None:
            return
        focus = self.focus_get()
        if focus is not self._popup:
            self._hide()

    # -- selection ----------------------------------------------------------

    def _focus_popup(self, _event=None):
        if self._popup is None:
            self._show(self._matches(self.get()))
        if self._popup is not None and self._popup.size():
            self._popup.focus_set()
            self._popup.selection_clear(0, "end")
            self._popup.selection_set(0)
            self._popup.activate(0)
        return "break"

    def _accept(self):
        if self._popup is None or not self._popup.curselection():
            return
        value = self._popup.get(self._popup.curselection()[0])
        self.delete(0, "end")
        self.insert(0, value)
        self._hide()
        self.focus_set()
        self.icursor("end")


# ---------------------------------------------------------------------------
# Tooltips
# ---------------------------------------------------------------------------

def _bind_recursive(widget, seq, func):
    """Bind an event on `widget`, falling back to its children.

    Composite CustomTkinter widgets (CTkSegmentedButton, CTkCheckBox) raise
    NotImplementedError on .bind(); their interactive surface lives on inner
    tk widgets, so we recurse until a real binder accepts it.
    """
    try:
        widget.bind(seq, func, add="+")
    except (NotImplementedError, tk.TclError):
        for child in widget.winfo_children():
            _bind_recursive(child, seq, func)


class _Tooltip:
    """Hover help bound to a single widget. Created via attach_tooltip()."""

    def __init__(self, widget, text: str, delay: int = 500):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after_id: str | None = None
        self._tip: tk.Toplevel | None = None
        _bind_recursive(widget, "<Enter>", self._schedule)
        _bind_recursive(widget, "<Leave>", self._hide)
        _bind_recursive(widget, "<ButtonPress>", self._hide)
        _bind_recursive(widget, "<Destroy>", self._hide)

    def _schedule(self, _event=None):
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self):
        if self._tip is not None or not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        tip = tk.Toplevel(self.widget)
        tip.overrideredirect(True)
        tip.attributes("-alpha", 0.96)
        tk.Label(
            tip, text=self.text, justify="left",
            bg=theme.resolve(theme.TOOLTIP_BG),
            fg=theme.resolve(theme.TOOLTIP_TEXT),
            font=("Helvetica", 11), padx=10, pady=6, wraplength=320,
        ).pack()
        tip.geometry(f"+{x}+{y}")
        self._tip = tip

    def _hide(self, _event=None):
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None


def attach_tooltip(widget, text: str, delay: int = 500) -> _Tooltip:
    """Attach hover help to `widget`. Returns the tooltip (usually ignorable)."""
    return _Tooltip(widget, text, delay)


# ---------------------------------------------------------------------------
# Design-system primitives
# ---------------------------------------------------------------------------

def shortcut_text(key: str) -> str:
    """"⌘E" on macOS, "Ctrl+E" elsewhere."""
    return f"⌘{key}" if sys.platform == "darwin" else f"Ctrl+{key}"


def add_hover(widget, **on_enter):
    """Reconfigure `widget` on hover and restore the prior values on leave.

    Pass the hovered state as kwargs (``border_color=...``, ``fg_color=...``);
    the pre-hover values are captured lazily on first enter, so a widget that
    is re-themed between hovers still restores correctly.
    """
    def enter(_e=None):
        widget._hover_restore = {k: widget.cget(k) for k in on_enter}
        widget.configure(**on_enter)

    def leave(_e=None):
        restore = getattr(widget, "_hover_restore", None)
        if restore:
            widget.configure(**restore)
            widget._hover_restore = None

    _bind_recursive(widget, "<Enter>", enter)
    _bind_recursive(widget, "<Leave>", leave)


def bind_click(widget, command: Callable[[], None]):
    """Make a non-button widget (and its children) clickable, with a hand cursor."""
    cursor = "pointinghand" if sys.platform == "darwin" else "hand2"

    def set_cursor(w):
        try:
            w.configure(cursor=cursor)
        except (tk.TclError, ValueError):
            pass
        for child in w.winfo_children():
            set_cursor(child)

    set_cursor(widget)
    _bind_recursive(widget, "<Button-1>", lambda _e: command())


def flash(widget, tint, *, duration_ms: int = 250):
    """Pulse `widget`'s background to `tint`, then restore it.

    Used by the topology panel to point at the card a clicked node belongs to.
    """
    try:
        original = widget.cget("fg_color")
    except (tk.TclError, ValueError):
        return
    widget.configure(fg_color=tint)

    def restore():
        if widget.winfo_exists():
            widget.configure(fg_color=original)

    widget.after(duration_ms, restore)


class Card(ctk.CTkFrame):
    """Surface + 1px border + radius 10 — the app's default container."""

    def __init__(self, master, *, hoverable: bool = False, **kw):
        kw.setdefault("fg_color", theme.SURFACE)
        kw.setdefault("border_width", 1)
        kw.setdefault("border_color", theme.BORDER)
        kw.setdefault("corner_radius", theme.RADIUS["card"])
        super().__init__(master, **kw)
        if hoverable:
            add_hover(self, border_color=theme.BORDER_STRONG)


class SectionLabel(ctk.CTkLabel):
    """11px/700 uppercase tertiary with the spec's +.06em tracking."""

    def __init__(self, master, text: str, **kw):
        kw.setdefault("text_color", theme.TEXT_TERTIARY)
        kw.setdefault("font", theme.ui_font(theme.SIZE["label"], "bold"))
        kw.setdefault("anchor", "w")
        super().__init__(master, text=theme.tracked(text), **kw)


class IconTile(ctk.CTkFrame):
    """A rounded tinted square holding one glyph — drop zone, avatars, toast ✓."""

    def __init__(self, master, glyph: str, *, size: int = 44,
                 tint=theme.ACCENT_TINT, fg=theme.ACCENT,
                 font_size: int | None = None, **kw):
        kw.setdefault("fg_color", tint)
        kw.setdefault("corner_radius", theme.RADIUS["card"] if size >= 40 else 8)
        super().__init__(master, width=size, height=size, **kw)
        self.grid_propagate(False)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        ctk.CTkLabel(
            self, text=glyph, text_color=fg,
            font=theme.ui_font(font_size or max(12, size // 2), "bold"),
        ).grid(row=0, column=0)


# Chip variants: (background, text colour, border colour, border width).
# "dashed" is the spec's dashed-border treatment for spare/unset values — Tk
# frames have no dash pattern, so it degrades to a muted fill + soft border,
# which still reads as "not wired up" next to the solid variants.
_CHIP_VARIANTS = {
    "neutral": (theme.SURFACE_CHIP, theme.TEXT_SECOND, theme.BORDER, 1),
    "outline": (theme.SURFACE_SUBTLE, theme.TEXT_SECOND, theme.BORDER_STRONG, 1),
    "accent": (theme.ACCENT, theme.ON_ACCENT, theme.ACCENT, 0),
    "accent_tint": (theme.ACCENT_TINT, theme.ACCENT, theme.ACCENT, 1),
    "warning": (theme.WARNING_TINT, theme.WARNING, theme.WARNING, 1),
    "error": (theme.ERROR_TINT, theme.ERROR, theme.ERROR, 1),
    "error_solid": (theme.WARNING, theme.ON_WARNING, theme.WARNING, 0),
    "success": (theme.SUCCESS_TINT, theme.SUCCESS, theme.SUCCESS_TINT, 0),
    "dashed": (theme.SURFACE_CHIP, theme.TEXT_TERTIARY, theme.BORDER_STRONG, 1),
}


class Chip(ctk.CTkFrame):
    """A pill/tag: filter chips, tab badges, footer issue chips, IN/OUT values.

    `variant` selects the colour family (see _CHIP_VARIANTS). Pass `on_click`
    to make it interactive — it then gets a hand cursor and an accent hover.
    """

    def __init__(self, master, text: str, *, variant: str = "neutral",
                 size: int | None = None, bold: bool = True,
                 pill: bool = True, on_click: Callable[[], None] | None = None,
                 padx: int = 10, pady: int = 3, mono: bool = False, **kw):
        bg, fg, border, border_w = _CHIP_VARIANTS.get(
            variant, _CHIP_VARIANTS["neutral"])
        kw.setdefault("fg_color", bg)
        kw.setdefault("border_color", border)
        kw.setdefault("border_width", border_w)
        kw.setdefault("corner_radius",
                      theme.RADIUS["pill"] if pill else theme.RADIUS["chip"])
        super().__init__(master, **kw)
        self._variant = variant

        font_size = size or theme.SIZE["chip"]
        font = (theme.mono_font if mono else theme.ui_font)(
            font_size, "bold" if bold else "normal")
        self._label = ctk.CTkLabel(self, text=text, text_color=fg, font=font)
        self._label.grid(row=0, column=0, padx=padx, pady=pady)

        if on_click is not None:
            bind_click(self, on_click)
            if variant != "accent":
                add_hover(self, border_color=theme.ACCENT)

    def set_text(self, text: str):
        self._label.configure(text=text)

    def set_variant(self, variant: str):
        """Re-colour in place — filter chips flip between active and inactive."""
        bg, fg, border, border_w = _CHIP_VARIANTS.get(
            variant, _CHIP_VARIANTS["neutral"])
        self._variant = variant
        self.configure(fg_color=bg, border_color=border, border_width=border_w)
        self._label.configure(text_color=fg)


class ShortcutChip(ctk.CTkLabel):
    """The "⌘E" hint rendered inside a primary button."""

    def __init__(self, master, key: str, **kw):
        kw.setdefault("text_color", theme.ON_ACCENT)
        kw.setdefault("font", theme.ui_font(theme.SIZE["badge"], "bold"))
        super().__init__(master, text=shortcut_text(key), **kw)


class ModeToggle(ctk.CTkFrame):
    """Two-segment Light/Dark pill. Calls `on_change(mode)` after switching."""

    def __init__(self, master, on_change: Callable[[str], None] | None = None, **kw):
        kw.setdefault("fg_color", "transparent")
        kw.setdefault("border_width", 1)
        kw.setdefault("border_color", theme.BORDER_STRONG)
        kw.setdefault("corner_radius", theme.RADIUS["pill"])
        super().__init__(master, **kw)
        self._on_change = on_change
        self._buttons: dict[str, ctk.CTkButton] = {}

        for col, (mode, label) in enumerate((("light", "Light"), ("dark", "Dark"))):
            btn = ctk.CTkButton(
                self, text=label, width=46, height=20,
                corner_radius=theme.RADIUS["pill"], border_width=0,
                font=theme.ui_font(theme.SIZE["label"], "bold"),
                command=lambda m=mode: self._select(m),
            )
            btn.grid(row=0, column=col, padx=2, pady=2)
            self._buttons[mode] = btn
        self._paint()

    def _select(self, mode: str):
        if mode == theme.current_mode():
            return
        theme.set_mode(mode)
        self._paint()
        if self._on_change is not None:
            self._on_change(mode)

    def _paint(self):
        active = theme.current_mode()
        for mode, btn in self._buttons.items():
            if mode == active:
                btn.configure(fg_color=theme.ACCENT, text_color=theme.ON_ACCENT,
                              hover_color=theme.ACCENT)
            else:
                btn.configure(fg_color="transparent",
                              text_color=theme.TEXT_SECOND,
                              hover_color=theme.HOVER_SUBTLE)


# -- button factories --------------------------------------------------------
#
# Three recipes cover every button in the app. Height defaults follow the
# spec's scale: 34 primary/secondary in the footer, 30 in toolbars, 28 inline.

def primary_button(master, text: str, command=None, *,
                   height: int = theme.HEIGHT["button"], **kw) -> ctk.CTkButton:
    kw.setdefault("fg_color", theme.ACCENT)
    kw.setdefault("hover_color", theme.ACCENT_HOVER)
    kw.setdefault("text_color", theme.ON_ACCENT)
    kw.setdefault("corner_radius", theme.RADIUS["button"])
    kw.setdefault("font", theme.ui_font(theme.SIZE["control"], "bold"))
    return ctk.CTkButton(master, text=text, command=command, height=height, **kw)


def secondary_button(master, text: str, command=None, *,
                     height: int = theme.HEIGHT["button"], **kw) -> ctk.CTkButton:
    kw.setdefault("fg_color", "transparent")
    kw.setdefault("border_width", 1)
    kw.setdefault("border_color", theme.BORDER_STRONG)
    kw.setdefault("text_color", theme.TEXT)
    kw.setdefault("hover_color", theme.HOVER_SUBTLE)
    kw.setdefault("corner_radius", theme.RADIUS["button"])
    kw.setdefault("font", theme.ui_font(theme.SIZE["control"], "bold"))
    return ctk.CTkButton(master, text=text, command=command, height=height, **kw)


def accent_outline_button(master, text: str, command=None, *,
                          height: int = theme.HEIGHT["button_md"],
                          **kw) -> ctk.CTkButton:
    """Secondary weight, accent voice — "+ Add Expander", "+ Add Splitter"."""
    kw.setdefault("border_color", theme.ACCENT)
    kw.setdefault("text_color", theme.ACCENT)
    return secondary_button(master, text, command, height=height, **kw)


def ghost_button(master, text: str, command=None, *,
                 height: int = theme.HEIGHT["button_md"],
                 danger: bool = False, **kw) -> ctk.CTkButton:
    kw.setdefault("fg_color", "transparent")
    kw.setdefault("border_width", 0)
    kw.setdefault("text_color", theme.TEXT_SECOND)
    kw.setdefault("hover_color", theme.HOVER_DANGER if danger else theme.HOVER_SUBTLE)
    kw.setdefault("corner_radius", theme.RADIUS["button"])
    kw.setdefault("font", theme.ui_font(theme.SIZE["control"]))
    return ctk.CTkButton(master, text=text, command=command, height=height, **kw)


def remove_button(master, command=None, *, size: int = 26) -> ctk.CTkButton:
    """The ✕ that removes a card's hardware. Red-tinted on hover."""
    return ghost_button(master, "✕", command, height=size, width=size,
                        danger=True, text_color=theme.TEXT_TERTIARY)
