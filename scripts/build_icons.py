"""
Convert logos/icons/app-icon-dmg.png into every derived icon:
app-icon.icns (macOS bundle), app-icon.ico (Windows .exe), and toolbar-icon.ico
(the in-window title-bar icon app.py sets on Windows).

Run before pyinstaller. Idempotent — re-running just regenerates from the source PNG.

The source must carry an alpha channel with the rounded-corner mask already
applied. It is used as-is: nothing here masks corners, so a flattened (fully
opaque) source silently yields square black icons.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

PROJECT_ROOT = Path(__file__).parent.parent
SRC = PROJECT_ROOT / "logos" / "icons" / "app-icon-dmg.png"
ICNS = PROJECT_ROOT / "logos" / "icons" / "app-icon.icns"
ICO = PROJECT_ROOT / "logos" / "icons" / "app-icon.ico"
TOOLBAR_ICO = PROJECT_ROOT / "logos" / "icons" / "toolbar-icon.ico"


def _square_padded(img: Image.Image) -> Image.Image:
    """Pad a non-square image to a square with transparent background."""
    if img.width == img.height:
        return img
    side = max(img.width, img.height)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(img, ((side - img.width) // 2, (side - img.height) // 2))
    return canvas


def build_ico(img: Image.Image, out: Path, max_size: int = 256) -> None:
    sizes = [(n, n) for n in (16, 24, 32, 48, 64, 128, 256) if n <= max_size]
    img.save(out, format="ICO", sizes=sizes)
    print(f"✓ Wrote {out}")


def build_icns(img: Image.Image, out: Path) -> None:
    if sys.platform != "darwin":
        print(f"  Skipping {out.name} — iconutil only available on macOS")
        return
    iconset_sizes = [
        (16, "16x16"),
        (32, "16x16@2x"),
        (32, "32x32"),
        (64, "32x32@2x"),
        (128, "128x128"),
        (256, "128x128@2x"),
        (256, "256x256"),
        (512, "256x256@2x"),
        (512, "512x512"),
        (1024, "512x512@2x"),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "app-icon.iconset"
        iconset.mkdir()
        for size, label in iconset_sizes:
            resized = img.resize((size, size), Image.LANCZOS)
            resized.save(iconset / f"icon_{label}.png", format="PNG")
        subprocess.run(
            ["iconutil", "--convert", "icns", str(iconset), "-o", str(out)],
            check=True,
        )
    print(f"✓ Wrote {out}")


def main() -> None:
    if not SRC.exists():
        sys.exit(f"Source icon not found: {SRC}")
    img = Image.open(SRC).convert("RGBA")
    print(f"Loaded {SRC} ({img.width}x{img.height})")
    if img.getchannel("A").getextrema()[0] == 255:
        print("  WARNING: source is fully opaque — its rounded corners are not")
        print("           masked, so the generated icons will be square blocks.")
        print("           Re-export the source PNG with transparency.")
    img = _square_padded(img)
    if img.width != Image.open(SRC).width:
        print(f"  Padded to {img.width}x{img.height}")
    build_ico(img, ICO)
    # The title-bar icon renders at ~16-32px, so it needs no size above 128.
    build_ico(img, TOOLBAR_ICO, max_size=128)
    build_icns(img, ICNS)


if __name__ == "__main__":
    main()
