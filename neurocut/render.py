"""Drive `melt` to extract preview frames and render final video."""
from __future__ import annotations

import io
import math
import os
import subprocess
import tempfile

from PIL import Image

from .probe import MELT

PREVIEW_CAPS = {"thumb": 320, "preview": 640, "full": 1024, "max": 1600}

PRESETS = {
    "mp4": ["vcodec=libx264", "acodec=aac", "crf=20", "preset=medium",
            "movflags=+faststart", "pix_fmt=yuv420p"],
    "mp4_hq": ["vcodec=libx264", "acodec=aac", "crf=16", "preset=slow",
               "movflags=+faststart", "pix_fmt=yuv420p"],
    "webm": ["vcodec=libvpx-vp9", "acodec=libopus", "crf=32", "b:v=0"],
    "prores": ["vcodec=prores_ks", "profile=3", "acodec=pcm_s16le", "vendor=apl0"],
    "gif": ["an=1"],
}


class RenderError(RuntimeError):
    pass


def _run(cmd: list[str], timeout: int) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RenderError(f"melt timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise RenderError("melt not found on PATH") from e
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-6:]
        raise RenderError("melt failed:\n" + "\n".join(tail))
    return r.stderr + r.stdout


def _write_xml(xml: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".mlt", prefix="neurocut_")
    with os.fdopen(fd, "w") as f:
        f.write(xml)
    return path


def extract_frame(xml: str, t: float, fps: float) -> Image.Image:
    frame = max(0, round(t * fps))
    xml_path = _write_xml(xml)
    out = os.path.join(tempfile.mkdtemp(prefix="neurocut_"), "f.png")
    try:
        _run([MELT, "-quiet", xml_path, f"in={frame}", f"out={frame}",
              "-consumer", f"avformat:{out}", "terminate_on_pause=1"], timeout=180)
        if not os.path.exists(out):
            raise RenderError("melt produced no frame (timeline empty at that time?)")
        return Image.open(out).convert("RGB")
    finally:
        _cleanup(xml_path, out)


def frames_grid(xml: str, times: list[float], fps: float, cols: int,
                cell: int = 320) -> Image.Image:
    imgs = [extract_frame(xml, t, fps) for t in times]
    rows = math.ceil(len(imgs) / cols)
    w0, h0 = imgs[0].size
    cw = cell
    ch = max(1, round(cell * h0 / w0))
    sheet = Image.new("RGB", (cw * cols, ch * rows), (18, 18, 20))
    for i, im in enumerate(imgs):
        im = im.resize((cw, ch))
        sheet.paste(im, ((i % cols) * cw, (i // cols) * ch))
    return sheet


def encode(img: Image.Image, detail: str = "preview", fmt: str = "webp",
           quality: int = 80) -> tuple[bytes, str, dict]:
    cap = PREVIEW_CAPS.get(detail)
    if cap is None:
        raise RenderError(f"detail must be one of {list(PREVIEW_CAPS)}")
    w, h = img.size
    scale = min(1.0, cap / max(w, h))
    if scale < 1.0:
        img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))))
    buf = io.BytesIO()
    if fmt == "png":
        img.save(buf, "PNG", optimize=True)
        real = "png"
    elif fmt in ("jpg", "jpeg"):
        img.save(buf, "JPEG", quality=quality)
        real = "jpeg"
    else:
        img.save(buf, "WEBP", quality=quality, method=4)
        real = "webp"
    data = buf.getvalue()
    ow, oh = img.size
    return data, real, {"preview_px": [ow, oh], "bytes": len(data),
                        "approx_image_tokens": (ow * oh + 749) // 750}


def render_video(xml: str, out_path: str, preset: str = "mp4",
                 scale: float = 1.0, fps: float | None = None,
                 t_in: float | None = None, t_out: float | None = None,
                 project_fps: float = 30.0, timeout: int = 3600) -> dict:
    if preset not in PRESETS:
        raise RenderError(f"preset must be one of {list(PRESETS)}")
    xml_path = _write_xml(xml)
    args = [MELT, "-quiet", xml_path]
    if t_in is not None:
        args.append(f"in={round(t_in * project_fps)}")
    if t_out is not None:
        args.append(f"out={round(t_out * project_fps)}")
    consumer = [f"avformat:{out_path}", *PRESETS[preset]]
    if scale != 1.0:
        consumer.append(f"resize=1")
    args += ["-consumer", *consumer]
    log = _run(args, timeout=timeout)
    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RenderError("render produced no output")
    os.remove(xml_path)
    return {"path": out_path, "bytes": os.path.getsize(out_path)}


def _cleanup(*paths: str) -> None:
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
            d = os.path.dirname(p)
            if d.startswith(tempfile.gettempdir()) and os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
