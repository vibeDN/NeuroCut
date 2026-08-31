"""Register a downloaded font with fontconfig so MLT's dynamictext can use it."""
from __future__ import annotations

import os
import shutil
import subprocess

from PIL import ImageFont

from .net import fetch_font

_FONT_DIR = os.path.join(os.path.expanduser("~"), ".local", "share", "fonts",
                         "neurocut")
os.makedirs(_FONT_DIR, exist_ok=True)
_cache: dict[str, str] = {}


def ensure_font(*, google: str | None = None, url: str | None = None,
                weight: int = 400, italic: bool = False) -> str:
    """Download + install the font, return the fontconfig family name to pass to
    dynamictext's `family` property."""
    key = url or f"{google}:{weight}:{italic}"
    if key in _cache:
        return _cache[key]
    src = fetch_font(url=url, google=google, weight=weight, italic=italic)
    dst = os.path.join(_FONT_DIR, os.path.basename(src))
    if not os.path.exists(dst):
        shutil.copy(src, dst)
        subprocess.run(["fc-cache", "-f", _FONT_DIR], capture_output=True,
                       timeout=60)
    try:
        family = ImageFont.truetype(dst, 20).getname()[0]
    except Exception:  # noqa: BLE001
        family = google or "Sans"
    _cache[key] = family
    return family
