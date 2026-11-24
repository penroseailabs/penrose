"""Draws the README's pictures: every scene in scenes.html, light and dark, at 2x, into docs/readme/*.webp.
Two more are kept as PNG, in light only, for uploading to GitHub by hand: social-preview.png (the repository's
Settings, Social preview) and avatar.png (the account's profile picture).

    python docs/readme/src/render.py            # all scenes
    python docs/readme/src/render.py hero sees  # some

Needs Chrome (or Edge), Pillow, and frontend/node_modules (npm ci in frontend/) for the fonts and tokens.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
OUT = HERE.parent
SCENES = {  # name: window size in CSS pixels (the picture is cropped to what is drawn)
    "hero": (1200, 620),
    "day": (1200, 560),
    "sees": (1200, 760),
    "ask": (1200, 720),
    "call": (1200, 700),
    "features": (1200, 700),
    "how": (1200, 520),
    "pocket": (1200, 700),
    "button-download": (400, 90),
    "button-how": (400, 90),
    "button-agent": (400, 90),
    "social": (1280, 640),
    "avatar": (500, 500),
}
UPLOADS = {"social": "social-preview.png", "avatar": "avatar.png"}  # PNG, light only, at the size GitHub asks for
BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "chromium",
]


def browser() -> str:
    for b in BROWSERS:
        if Path(b).exists() or "/" not in b and "\\" not in b:
            return b
    raise SystemExit("Chrome or Edge not found")


def render(name: str, theme: str, chrome: str, profile: str) -> Path:
    w, h = SCENES[name]
    out = OUT / f"{name}-{theme}.png"
    webp = out.with_suffix(".webp")
    url = (HERE / "scenes.html").as_uri() + f"?scene={name}&theme={theme}"
    subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--allow-file-access-from-files",
         f"--user-data-dir={profile}", f"--force-device-scale-factor={1 if name in UPLOADS else 2}",
         "--default-background-color=00000000",
         "--virtual-time-budget=8000", f"--window-size={w},{h}", f"--screenshot={out}", url],
        check=True, capture_output=True,
    )
    if name in UPLOADS:
        final = OUT / UPLOADS[name]
        Image.open(out).convert("RGB").save(final, "PNG", optimize=True)
        out.unlink()
        return final
    image = Image.open(out).convert("RGBA")
    box = image.getchannel("A").point(lambda a: 255 if a > 3 else 0).getbbox()
    if box:
        image = image.crop(box)
    image.save(webp, "WEBP", quality=90, method=6)
    out.unlink()
    return webp


def main(names: list[str]) -> None:
    chrome = browser()
    with tempfile.TemporaryDirectory() as profile:
        for name in names or list(SCENES):
            for theme in ("light",) if name in UPLOADS else ("light", "dark"):
                out = render(name, theme, chrome, profile)
                print(f"{out.relative_to(OUT.parent.parent)}  {os.path.getsize(out) // 1024} KB")


if __name__ == "__main__":
    main(sys.argv[1:])
