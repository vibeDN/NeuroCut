"""Download media (video/image/audio) and fonts from the internet to a disk cache."""
from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import urllib.parse
import urllib.request

CACHE_DIR = os.environ.get(
    "NEUROCUT_CACHE",
    os.path.join(os.path.expanduser("~"), ".cache", "neurocut"),
)
MAX_BYTES = int(os.environ.get("NEUROCUT_MAX_DOWNLOAD", str(512 * 1024 * 1024)))
_UA = "neurocut/0.1"

for _sub in ("media", "font"):
    os.makedirs(os.path.join(CACHE_DIR, _sub), exist_ok=True)


class FetchError(RuntimeError):
    pass


def _download(url: str, dest: str, *, accept: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchError(f"only http(s) URLs are allowed, got {url!r}")
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": accept})
    tmp = dest + ".part"
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_BYTES:
                        raise FetchError(f"resource exceeds {MAX_BYTES} bytes: {url}")
                    f.write(chunk)
        ext = os.path.splitext(parsed.path)[1]
        if not ext:
            ext = mimetypes.guess_extension(
                resp.headers.get_content_type()) or ".bin"
        os.replace(tmp, dest + ext)
        return dest + ext
    except FetchError:
        raise
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"download failed for {url}: {e}") from e
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _key_path(kind: str, key: str) -> str:
    h = hashlib.sha256(key.encode()).hexdigest()[:24]
    d = os.path.join(CACHE_DIR, kind, h)
    for existing in (d + e for e in (".mp4", ".mov", ".webm", ".mkv", ".png",
                                     ".jpg", ".jpeg", ".webp", ".gif", ".mp3",
                                     ".wav", ".m4a", ".aac", ".ogg", ".flac",
                                     ".ttf", ".otf", ".bin")):
        if os.path.exists(existing):
            return existing
    return d  # base path, no extension yet


def fetch_media(url_or_path: str) -> str:
    """Return a local filesystem path for a media URL (downloading + caching) or
    an already-local path (validated)."""
    if "://" not in url_or_path or url_or_path.startswith("file://"):
        p = url_or_path[7:] if url_or_path.startswith("file://") else url_or_path
        p = os.path.abspath(os.path.expanduser(p))
        if not os.path.isfile(p):
            raise FetchError(f"no such file: {p}")
        return p
    cached = _key_path("media", url_or_path)
    if os.path.isfile(cached):
        return cached
    return _download(url_or_path, cached, accept="video/*,image/*,audio/*,*/*")


_GF_CSS_URL_RE = re.compile(r"url\((https://[^)\s]+)\)")


def _fetch_css(url: str) -> str:
    # An unrecognized User-Agent makes Google serve plain TTF (skia/PIL/fontconfig
    # can't read woff2).
    req = urllib.request.Request(url, headers={
        "User-Agent": "NeuroCut font fetcher", "Accept": "text/css,*/*"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def _google_font_url(family: str, weight: int, italic: bool) -> str:
    fam = family.strip().replace(" ", "+")
    # Try specific axes first, then progressively looser - a family that lacks the
    # requested weight/italic returns HTTP 400, so fall back to the bare family.
    axes = []
    if italic:
        axes.append(f":ital,wght@1,{weight}")
        axes.append(":ital@1")
    axes.append(f":wght@{weight}")
    axes.append("")
    last = None
    for ax in axes:
        css = f"https://fonts.googleapis.com/css2?family={fam}{ax}&display=swap"
        try:
            body = _fetch_css(css)
        except Exception as e:  # noqa: BLE001  (400 for a missing variant, etc.)
            last = e
            continue
        urls = _GF_CSS_URL_RE.findall(body)
        ttf = [u for u in urls if u.lower().split("?")[0].endswith((".ttf", ".otf"))]
        if ttf:
            return ttf[0]
        if urls:
            return urls[0]
    raise FetchError(
        f"Google Fonts has no usable file for {family!r} "
        f"(weight {weight}{', italic' if italic else ''}); last error: {last}. "
        f"Pass font_url= a direct .ttf/.otf instead.")


def fetch_font(*, url: str | None = None, google: str | None = None,
               weight: int = 400, italic: bool = False) -> str:
    """Return a local path to a .ttf/.otf for a direct URL or a Google font name."""
    if not url and not google:
        raise FetchError("provide either url= or google=")
    key = url or f"google:{google}:{weight}:{int(italic)}"
    cached = _key_path("font", key)
    if os.path.isfile(cached):
        return cached
    real = url or _google_font_url(google, weight, italic)
    return _download(real, cached, accept="font/ttf,font/otf,*/*")
