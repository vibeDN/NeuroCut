"""ffprobe wrapper: inspect a media file."""
from __future__ import annotations

import json
import shutil
import subprocess

FFPROBE = shutil.which("ffprobe") or "ffprobe"
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
MELT = shutil.which("melt") or "melt"


class ProbeError(RuntimeError):
    pass


def _frac(s: str, default: float = 0.0) -> float:
    try:
        if "/" in str(s):
            a, b = str(s).split("/")
            return float(a) / float(b) if float(b) else default
        return float(s)
    except (ValueError, ZeroDivisionError):
        return default


def probe(path: str) -> dict:
    try:
        out = subprocess.run(
            [FFPROBE, "-v", "error", "-show_format", "-show_streams",
             "-print_format", "json", path],
            capture_output=True, text=True, timeout=60, check=True).stdout
    except subprocess.CalledProcessError as e:
        raise ProbeError(f"ffprobe failed: {e.stderr.strip()[:300]}") from e
    except FileNotFoundError as e:
        raise ProbeError("ffprobe not found on PATH") from e
    data = json.loads(out)
    fmt = data.get("format", {})
    v = a = None
    for s in data.get("streams", []):
        if s.get("codec_type") == "video" and v is None:
            v = s
        elif s.get("codec_type") == "audio" and a is None:
            a = s
    duration = _frac(fmt.get("duration"), 0.0)
    info = {
        "path": path,
        "duration": round(duration, 3),
        "container": fmt.get("format_name", ""),
        "has_video": v is not None,
        "has_audio": a is not None,
    }
    if v:
        info.update(
            width=int(v.get("width", 0)),
            height=int(v.get("height", 0)),
            fps=round(_frac(v.get("avg_frame_rate") or v.get("r_frame_rate"), 0), 4),
            video_codec=v.get("codec_name", ""),
            is_image=(v.get("avg_frame_rate") in ("0/0", None)
                      and _frac(v.get("r_frame_rate")) == 0) or duration == 0,
        )
    if a:
        info.update(
            audio_codec=a.get("codec_name", ""),
            sample_rate=int(a.get("sample_rate", 0) or 0),
            channels=int(a.get("channels", 0) or 0),
        )
    if not v and a:
        info["is_image"] = False
    return info
