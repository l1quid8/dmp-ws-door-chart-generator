"""Small reusable Tk/CustomTkinter widgets the toolkit doesn't ship.

- AutocompleteEntry: a CTkEntry that filters a suggestion list as you type and
  offers it in a dropdown listbox. Used by the hardware add-dialogs so a tech
  can reuse a location already typed elsewhere instead of retyping (and risking
  a typo that splits one room across two strings).
- attach_tooltip: hover help for any widget, for the controls whose meaning
  isn't obvious without the README (the ZONES filter chips, the wiring-reviewed
  checkbox).

Both keep their popups inside the owning toplevel (place() / a transient
Toplevel) so they coexist with the modal grab the add-dialogs already hold.
"""

from __future__ import annotations

import tkinter as tk
from typing import Callable

import customtkinter as ctk

ACCENT = "#4a7bb8"

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
            self._popup = tk.Listbox(
                top, activestyle="none", highlightthickness=1,
                highlightcolor=ACCENT, relief="solid", borderwidth=1,
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
            tip, text=self.text, justify="left", bg="#1c1c1e", fg="white",
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
