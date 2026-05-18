"""
Convert logos/icon_dmp_ws_gen.png into logos/icons/exe-icon.ico.

This is the icon embedded into the Windows .exe by PyInstaller — shown in
File Explorer, the taskbar, and Alt-Tab. The in-window title-bar icon is
set separately at runtime to logos/icons/app-icon.ico (built by build_icons.py).
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).parent.parent
SRC = PROJECT_ROOT / "logos" / "icon_dmp_ws_gen.png"
OUT = PROJECT_ROOT / "logos" / "icons" / "exe-icon.ico"


def main() -> None:
    img = Image.open(SRC).convert("RGBA")
    print(f"Loaded {SRC.name} ({img.width}x{img.height})")

    if img.width != img.height:
        side = max(img.width, img.height)
        canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
        img = canvas
        print(f"Padded to {img.width}x{img.height}")

    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(OUT, format="ICO", sizes=sizes)
    print(f"Wrote {OUT}")


if __name__ == "__main__":
    main()
