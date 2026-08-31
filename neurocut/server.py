"""NeuroCut - an MLT-backed video editor exposed to an AI over MCP.

Same philosophy as a good painting tool: a stateful project the model mutates
with terse calls, then *looks* at cheap downscaled preview frames (never the
whole video) and iterates; `render` writes the real file to disk for free.
"""
from __future__ import annotations

import json
import os
import threading

import mcp.types as _mt
from fastmcp import FastMCP
from fastmcp.tools import ToolResult
from fastmcp.utilities.types import Image

from . import mltxml, render
from .fonts import ensure_font
from .model import (Project, ProjectError, new_clip, new_filter, new_media,
                    new_track, new_transition)
from .net import fetch_media
from .probe import probe

EXPORT_DIR = os.environ.get(
    "NEUROCUT_EXPORT",
    os.path.join(os.path.expanduser("~"), "neurocut-output"))
os.makedirs(EXPORT_DIR, exist_ok=True)

_PROJECTS: dict[str, Project] = {}
_ORDER: list[str] = []
_LOCK = threading.RLock()

INSTRUCTIONS = """
NeuroCut is a multi-track video editor. Time is in SECONDS (floats). Track index
0 is the bottom video layer; higher video tracks composite on top. Colors:
'#rrggbb' / '#rrggbbaa'.

EFFICIENT WORKFLOW (this keeps token cost low):
  1. new_project once. add_media for every source (URL or local path).
  2. Lay down clips with add_clip / add_text / add_image. Prefer `batch` to send
     many edits in a single call.
  3. Check with storyboard(count=9) - ONE image showing frames across the whole
     timeline - or preview(at=SECONDS) for one moment. Both are downscaled; use
     detail='full' only for a final check.
  4. Fix what's wrong; undo() if an edit goes bad.
  5. render() writes the final video to disk (costs no image tokens).

Never expect to receive video - you only ever get still preview frames.
""".strip()

mcp = FastMCP("neurocut", instructions=INSTRUCTIONS)


class _TokenGate:
    def __init__(self, app, token: str):
        self.app, self.token = app, token

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not scope.get("path", "").startswith(
                "/.well-known"):
            h = dict(scope.get("headers") or [])
            supplied = h.get(b"x-neurocut-token", b"").decode()
            auth = h.get(b"authorization", b"").decode()
            if auth[:7].lower() == "bearer ":
                auth = auth[7:]
            if self.token not in (supplied, auth):
                body = b'{"error":"missing or invalid token"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length",
                                         str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


# --------------------------------------------------------------------------- #
def _result(*parts) -> ToolResult:
    content = []
    for p in parts:
        if isinstance(p, Image):
            content.append(p.to_image_content())
        else:
            content.append(_mt.TextContent(type="text", text=str(p)))
    return ToolResult(content=content)


def _p(project_id: str | None) -> Project:
    with _LOCK:
        if project_id:
            if project_id not in _PROJECTS:
                raise ProjectError(f"no project {project_id!r}")
            return _PROJECTS[project_id]
        if not _ORDER:
            raise ProjectError("no project yet - call new_project")
        return _PROJECTS[_ORDER[-1]]


def _ok(pr: Project, msg: str) -> str:
    return (f"{msg} | project {pr.id} | {len(pr.tracks)} tracks | "
            f"dur {pr.duration():.2f}s | {pr.ops} ops | undo {len(pr._undo)}")


def _xml(pr: Project) -> str:
    return mltxml.to_xml(pr)


# --------------------------------------------------------------------------- #
# project
# --------------------------------------------------------------------------- #
@mcp.tool
def new_project(width: int = 1920, height: int = 1080, fps: float = 30.0,
                background: str = "#000000") -> str:
    """Create a project. Returns project_id + summary (JSON)."""
    pr = Project(width, height, fps, background)
    with _LOCK:
        _PROJECTS[pr.id] = pr
        _ORDER.append(pr.id)
    return json.dumps(pr.summary())


@mcp.tool
def project_info(project_id: str | None = None) -> str:
    """Full project state: tracks, clips (with start/duration/end), media (JSON)."""
    return json.dumps(_p(project_id).summary())


@mcp.tool
def list_projects() -> str:
    """List open projects (JSON)."""
    with _LOCK:
        return json.dumps([_PROJECTS[i].summary() for i in _ORDER][::-1])


@mcp.tool
def delete_project(project_id: str) -> str:
    """Discard a project."""
    with _LOCK:
        _PROJECTS.pop(project_id, None)
        if project_id in _ORDER:
            _ORDER.remove(project_id)
    return f"deleted {project_id}"


# --------------------------------------------------------------------------- #
# media
# --------------------------------------------------------------------------- #
@mcp.tool
def add_media(url: str, name: str | None = None,
              project_id: str | None = None) -> str:
    """Register a source file (http(s) URL or local path). Downloads + probes it.
    Returns media_id and probe info (duration, resolution, has_audio, is_image)."""
    pr = _p(project_id)
    path = fetch_media(url)
    info = probe(path)
    with pr.lock:
        m = new_media(path, name or os.path.basename(url) or path, info)
        pr.media[m.id] = m
    return json.dumps({"media_id": m.id, **info})


# --------------------------------------------------------------------------- #
# tracks
# --------------------------------------------------------------------------- #
@mcp.tool
def add_track(kind: str = "video", name: str | None = None,
              project_id: str | None = None) -> str:
    """Add a 'video' or 'audio' track. Video tracks composite in order (higher =
    on top). Returns the new track index."""
    if kind not in ("video", "audio"):
        raise ProjectError("kind must be 'video' or 'audio'")
    pr = _p(project_id)
    with pr.lock:
        pr.checkpoint("add_track")
        n = sum(1 for t in pr.tracks if t.kind == kind) + 1
        pr.tracks.append(new_track(kind, name or f"{kind[0].upper()}{n}"))
        idx = len(pr.tracks) - 1
    return _ok(pr, f"added {kind} track at index {idx}")


@mcp.tool
def update_track(index: int, name: str | None = None, muted: bool | None = None,
                 hidden: bool | None = None, volume: float | None = None,
                 opacity: float | None = None,
                 project_id: str | None = None) -> str:
    """Rename / mute / hide a track, or set its volume (linear) or opacity (0..1)."""
    pr = _p(project_id)
    with pr.lock:
        tr = pr.track(index)
        pr.checkpoint("update_track")
        if name is not None:
            tr.name = name
        if muted is not None:
            tr.muted = bool(muted)
        if hidden is not None:
            tr.hidden = bool(hidden)
        if volume is not None:
            tr.volume = float(volume)
        if opacity is not None:
            tr.opacity = max(0.0, min(1.0, float(opacity)))
    return _ok(pr, f"updated track {index}")


@mcp.tool
def remove_track(index: int, project_id: str | None = None) -> str:
    """Delete a track and its clips."""
    pr = _p(project_id)
    with pr.lock:
        pr.track(index)
        if len(pr.tracks) <= 1:
            raise ProjectError("cannot remove the last track")
        pr.checkpoint("remove_track")
        pr.tracks.pop(index)
    return _ok(pr, f"removed track {index}")


# --------------------------------------------------------------------------- #
# clips
# --------------------------------------------------------------------------- #
def _place(pr: Project, tr, clip, start):
    clip.start = pr.append_position(tr) if start is None else max(0.0, float(start))
    tr.clips.append(clip)


@mcp.tool
def add_clip(media_id: str, track: int, start: float | None = None,
             in_point: float = 0.0, out_point: float | None = None,
             project_id: str | None = None) -> str:
    """Place a media clip on a track. start = timeline position (s, default: append).
    in_point/out_point = trim within the source (s). Returns clip_id."""
    pr = _p(project_id)
    with pr.lock:
        tr = pr.track(track)
        m = pr.media_or_die(media_id)
        dur = m.info.get("duration") or 0.0
        op = dur if out_point is None else float(out_point)
        if m.info.get("is_image"):
            op = op or 4.0
        kind = "audio" if tr.kind == "audio" else (
            "image" if m.info.get("is_image") else "video")
        pr.checkpoint("add_clip")
        c = new_clip(kind=kind, media_id=media_id, start=0.0,
                     in_point=max(0.0, float(in_point)), out_point=max(0.1, op))
        _place(pr, tr, c, start)
    return _ok(pr, f"added clip {c.id} ({kind}, {c.duration:.2f}s @ {c.start:.2f}s)")


@mcp.tool
def add_color(track: int, color: str = "#000000", duration: float = 3.0,
              start: float | None = None, project_id: str | None = None) -> str:
    """Add a solid color clip (backgrounds, flashes, mattes). Returns clip_id."""
    pr = _p(project_id)
    with pr.lock:
        tr = pr.track(track)
        pr.checkpoint("add_color")
        c = new_clip(kind="color", media_id=None, start=0.0, in_point=0.0,
                     out_point=max(0.1, float(duration)), props={"color": color})
        _place(pr, tr, c, start)
    return _ok(pr, f"added color clip {c.id}")


@mcp.tool
def add_text(text: str, track: int, duration: float = 3.0,
             start: float | None = None, size: int | None = None,
             color: str = "#ffffffff", position: str = "bottom",
             font_google: str | None = None, font_url: str | None = None,
             weight: int = 400, italic: bool = False, bg: str = "#00000000",
             outline: int = 2, outline_color: str = "#cc000000",
             project_id: str | None = None) -> str:
    """Add a text/caption clip on a video track. position: top|middle|bottom or
    'H V' halign/valign. font_google='Bebas Neue' pulls a webfont. Returns clip_id."""
    pr = _p(project_id)
    family = "Sans"
    if font_google or font_url:
        family = ensure_font(google=font_google, url=font_url, weight=weight,
                             italic=italic)
    halign, valign = "center", "bottom"
    if position in ("top", "middle", "center", "bottom"):
        valign = "middle" if position in ("middle", "center") else position
    elif " " in position:
        halign, valign = position.split()[:2]
    with pr.lock:
        tr = pr.track(track)
        if tr.kind != "video":
            raise ProjectError("text goes on a video track")
        pr.checkpoint("add_text")
        c = new_clip(kind="text", media_id=None, start=0.0, in_point=0.0,
                     out_point=max(0.1, float(duration)),
                     props={"text": text, "size": size or max(28, pr.h // 12),
                            "color": color, "bg": bg, "family": family,
                            "weight": weight, "italic": italic,
                            "outline": outline, "outline_color": outline_color,
                            "halign": halign, "valign": valign})
        _place(pr, tr, c, start)
    return _ok(pr, f"added text clip {c.id} (font {family!r})")


@mcp.tool
def move_clip(clip_id: str, start: float | None = None, track: int | None = None,
              project_id: str | None = None) -> str:
    """Move a clip to a new timeline position and/or a different track."""
    pr = _p(project_id)
    with pr.lock:
        src_tr, c = pr.find_clip(clip_id)
        pr.checkpoint("move_clip")
        if start is not None:
            c.start = max(0.0, float(start))
        if track is not None and pr.track(track) is not src_tr:
            src_tr.clips.remove(c)
            pr.track(track).clips.append(c)
    return _ok(pr, f"moved {clip_id}")


@mcp.tool
def trim_clip(clip_id: str, in_point: float | None = None,
              out_point: float | None = None,
              project_id: str | None = None) -> str:
    """Change a clip's source in/out trim points (seconds)."""
    pr = _p(project_id)
    with pr.lock:
        _, c = pr.find_clip(clip_id)
        pr.checkpoint("trim_clip")
        if in_point is not None:
            c.in_point = max(0.0, float(in_point))
        if out_point is not None:
            c.out_point = max(c.in_point + 0.05, float(out_point))
    return _ok(pr, f"trimmed {clip_id} -> {c.duration:.2f}s")


@mcp.tool
def split_clip(clip_id: str, at: float, project_id: str | None = None) -> str:
    """Split a clip at timeline position `at` (seconds). Returns both clip ids."""
    pr = _p(project_id)
    with pr.lock:
        tr, c = pr.find_clip(clip_id)
        if not (c.start < at < c.end):
            raise ProjectError(f"{at}s is not inside clip {clip_id} "
                               f"({c.start:.2f}..{c.end:.2f})")
        pr.checkpoint("split_clip")
        off = (at - c.start) * c.speed
        b = new_clip(kind=c.kind, media_id=c.media_id, start=at,
                     in_point=c.in_point + off, out_point=c.out_point,
                     speed=c.speed, gain=c.gain, props=dict(c.props),
                     filters=list(c.filters))
        c.out_point = c.in_point + off
        c.fade_out = 0.0
        b.fade_in = 0.0
        tr.clips.append(b)
    return _ok(pr, f"split -> {c.id} + {b.id}")


@mcp.tool
def remove_clip(clip_id: str, ripple: bool = False,
                project_id: str | None = None) -> str:
    """Delete a clip. ripple=True also shifts later clips on that track left."""
    pr = _p(project_id)
    with pr.lock:
        tr, c = pr.find_clip(clip_id)
        pr.checkpoint("remove_clip")
        gap = c.duration
        tr.clips.remove(c)
        pr.transitions = [t for t in pr.transitions
                          if clip_id not in (t.a, t.b)]
        if ripple:
            for other in tr.clips:
                if other.start >= c.start:
                    other.start = max(0.0, other.start - gap)
    return _ok(pr, f"removed {clip_id}")


@mcp.tool
def set_clip_speed(clip_id: str, speed: float, project_id: str | None = None) -> str:
    """Change playback speed (2.0 = 2x faster, 0.5 = slow-mo). Video/audio only."""
    pr = _p(project_id)
    with pr.lock:
        _, c = pr.find_clip(clip_id)
        if c.kind not in ("video", "audio"):
            raise ProjectError("speed only applies to video/audio clips")
        pr.checkpoint("set_clip_speed")
        c.speed = max(0.1, min(20.0, float(speed)))
    return _ok(pr, f"{clip_id} speed {c.speed}x -> {c.duration:.2f}s")


@mcp.tool
def set_clip_fade(clip_id: str, fade_in: float | None = None,
                  fade_out: float | None = None,
                  project_id: str | None = None) -> str:
    """Set fade-in / fade-out duration (seconds) on a clip. Fades alpha, so on an
    upper track it dissolves into the track below."""
    pr = _p(project_id)
    with pr.lock:
        _, c = pr.find_clip(clip_id)
        pr.checkpoint("set_clip_fade")
        if fade_in is not None:
            c.fade_in = max(0.0, float(fade_in))
        if fade_out is not None:
            c.fade_out = max(0.0, float(fade_out))
    return _ok(pr, f"{clip_id} fades {c.fade_in}/{c.fade_out}s")


@mcp.tool
def set_clip_gain(clip_id: str, gain_db: float | None = None,
                  gain: float | None = None, project_id: str | None = None) -> str:
    """Set a clip's audio level, as dB (gain_db) or linear multiplier (gain)."""
    pr = _p(project_id)
    with pr.lock:
        _, c = pr.find_clip(clip_id)
        pr.checkpoint("set_clip_gain")
        if gain_db is not None:
            c.gain = 10 ** (float(gain_db) / 20.0)
        elif gain is not None:
            c.gain = max(0.0, float(gain))
    return _ok(pr, f"{clip_id} gain x{c.gain:.3f}")


# --------------------------------------------------------------------------- #
# effects
# --------------------------------------------------------------------------- #
def _target(pr: Project, target: str):
    if target.startswith("track") and target[5:].isdigit():
        return pr.track(int(target[5:]))
    return pr.find_clip(target)[1]


@mcp.tool
def add_filter(target: str, service: str, params: dict | None = None,
               project_id: str | None = None) -> str:
    """Attach an MLT filter to a clip (by id) or a track ('track0', 'track1', ...).

    Useful services: brightness, saturation {level}, gamma, sepia, grayscale,
    oldfilm, gblur {radius}, sharpen, vignette, crop {top,bottom,left,right},
    dust, spot_remover, avfilter.hue {h,s}, avfilter.eq {contrast,brightness,
    saturation,gamma}, avfilter.unsharp, avfilter.vignette, avfilter.gblur.
    """
    pr = _p(project_id)
    with pr.lock:
        obj = _target(pr, target)
        pr.checkpoint("add_filter")
        obj.filters.append(new_filter(service, params or {}))
    return _ok(pr, f"added filter {service} on {target}")


@mcp.tool
def add_transform(target: str, x: float = 0, y: float = 0,
                  width: float | None = None, height: float | None = None,
                  opacity: float = 1.0, rotate: float = 0.0,
                  project_id: str | None = None) -> str:
    """Position/scale/rotate a clip or track for picture-in-picture and overlays.
    x,y,width,height are in project pixels (default: full frame)."""
    pr = _p(project_id)
    w = pr.w if width is None else width
    h = pr.h if height is None else height
    with pr.lock:
        obj = _target(pr, target)
        pr.checkpoint("add_transform")
        obj.filters.append(new_filter("qtblend", {
            "rect": f"{int(x)} {int(y)} {int(w)} {int(h)} {max(0.0, min(1.0, opacity))}",
            "rotation": rotate, "compositing": 0}))
    return _ok(pr, f"transform on {target}")


@mcp.tool
def clear_filters(target: str, project_id: str | None = None) -> str:
    """Remove all filters/transforms from a clip or track."""
    pr = _p(project_id)
    with pr.lock:
        obj = _target(pr, target)
        pr.checkpoint("clear_filters")
        obj.filters.clear()
    return _ok(pr, f"cleared filters on {target}")


@mcp.tool
def crossfade(clip_a: str, clip_b: str, duration: float = 1.0,
              project_id: str | None = None) -> str:
    """Dissolve from clip_a into clip_b. clip_b is lifted to the video track above
    clip_a's, overlapped by `duration`, and given a matching fade-in."""
    pr = _p(project_id)
    with pr.lock:
        ta, ca = pr.find_clip(clip_a)
        tb, cb = pr.find_clip(clip_b)
        if ta.kind != "video":
            raise ProjectError("crossfade needs video clips")
        ia = pr.tracks.index(ta)
        upper = next((i for i in range(ia + 1, len(pr.tracks))
                      if pr.tracks[i].kind == "video"), None)
        pr.checkpoint("crossfade")
        if upper is None:
            pr.tracks.append(new_track("video", f"V{sum(1 for t in pr.tracks if t.kind=='video')+1}"))
            upper = len(pr.tracks) - 1
        up = pr.tracks[upper]
        if cb in tb.clips and tb is not up:
            tb.clips.remove(cb)
            up.clips.append(cb)
        cb.start = max(0.0, ca.end - float(duration))
        cb.fade_in = float(duration)
        ca.fade_out = max(ca.fade_out, 0.0)
        pr.transitions.append(new_transition(ca.id, cb.id, "dissolve", duration))
    return _ok(pr, f"crossfade {clip_a}->{clip_b} ({duration}s)")


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #
@mcp.tool(output_schema=None)
def preview(at: float = 0.0, detail: str = "preview",
            image_format: str = "webp", project_id: str | None = None) -> ToolResult:
    """Render ONE frame at `at` seconds and return it as an image.
    detail: thumb (~320px) / preview (~640px, default) / full (~1024px) / max.
    The reply reports approx_image_tokens."""
    pr = _p(project_id)
    with pr.lock:
        xml = _xml(pr)
        img = render.extract_frame(xml, at, pr.fps)
    data, fmt, info = render.encode(img, detail, image_format)
    info["at"] = at
    return _result(json.dumps(info), Image(data=data, format=fmt))


@mcp.tool(output_schema=None)
def storyboard(count: int = 9, detail: str = "preview", cols: int = 3,
               image_format: str = "webp",
               project_id: str | None = None) -> ToolResult:
    """Render `count` frames evenly across the whole timeline as ONE contact-sheet
    image - the cheap way to review an entire edit."""
    pr = _p(project_id)
    dur = pr.duration()
    if dur <= 0:
        raise ProjectError("timeline is empty")
    count = max(2, min(25, count))
    times = [dur * (i + 0.5) / count for i in range(count)]
    with pr.lock:
        xml = _xml(pr)
        sheet = render.frames_grid(xml, times, pr.fps, cols)
    data, fmt, info = render.encode(sheet, detail, image_format)
    info["times"] = [round(t, 2) for t in times]
    return _result(json.dumps(info), Image(data=data, format=fmt))


@mcp.tool
def render_video(filename: str | None = None, preset: str = "mp4",
                 scale: float = 1.0, start: float | None = None,
                 end: float | None = None, project_id: str | None = None) -> str:
    """Render the project to a video file on disk (no image tokens).
    preset: mp4 | mp4_hq | webm | prores | gif. Returns the absolute path."""
    pr = _p(project_id)
    ext = "gif" if preset == "gif" else ("mov" if preset == "prores"
                                         else ("webm" if preset == "webm" else "mp4"))
    name = filename or f"{pr.id}.{ext}"
    if not os.path.isabs(name):
        name = os.path.join(EXPORT_DIR, name)
    with pr.lock:
        xml = _xml(pr)
    info = render.render_video(xml, name, preset=preset, scale=scale,
                               t_in=start, t_out=end, project_fps=pr.fps)
    return f"rendered {info['bytes']} bytes -> {info['path']}"


@mcp.tool
def save_mlt(filename: str | None = None, project_id: str | None = None) -> str:
    """Write the project as an .mlt file (opens in Shotcut / Kdenlive / melt)."""
    pr = _p(project_id)
    name = filename or f"{pr.id}.mlt"
    if not os.path.isabs(name):
        name = os.path.join(EXPORT_DIR, name)
    with pr.lock:
        xml = _xml(pr)
    with open(name, "w") as f:
        f.write(xml)
    return f"wrote {name}"


@mcp.tool
def undo(steps: int = 1, project_id: str | None = None) -> str:
    """Undo the last N edits."""
    pr = _p(project_id)
    with pr.lock:
        msgs = [pr.undo() for _ in range(max(1, steps))]
    return _ok(pr, "; ".join(msgs))


@mcp.tool
def redo(steps: int = 1, project_id: str | None = None) -> str:
    """Redo undone edits."""
    pr = _p(project_id)
    with pr.lock:
        msgs = [pr.redo() for _ in range(max(1, steps))]
    return _ok(pr, "; ".join(msgs))


_OPS = {
    "add_clip": add_clip, "add_color": add_color, "add_text": add_text,
    "move_clip": move_clip, "trim_clip": trim_clip, "split_clip": split_clip,
    "remove_clip": remove_clip, "set_clip_speed": set_clip_speed,
    "set_clip_fade": set_clip_fade, "set_clip_gain": set_clip_gain,
    "add_filter": add_filter, "add_transform": add_transform,
    "clear_filters": clear_filters, "crossfade": crossfade,
    "add_track": add_track, "update_track": update_track,
}


@mcp.tool(output_schema=None)
def batch(ops: list[dict], preview_after: bool = False,
          project_id: str | None = None) -> ToolResult:
    """Run many edits in ONE call (one undo step). Each item: {"op": <name>, ...}.
    op is any of: add_clip, add_color, add_text, move_clip, trim_clip, split_clip,
    remove_clip, set_clip_speed, set_clip_fade, set_clip_gain, add_filter,
    add_transform, clear_filters, crossfade, add_track, update_track.
    If preview_after=True, also returns a storyboard."""
    pr = _p(project_id)
    results = []
    with pr.lock:
        pr.checkpoint(f"batch[{len(ops)}]")
        depth = len(pr._undo)
        for i, item in enumerate(ops):
            item = dict(item)
            name = item.pop("op", None)
            fn = _OPS.get(name)
            if not fn:
                raise ProjectError(f"op #{i}: unknown op {name!r}")
            item["project_id"] = pr.id
            try:
                line = fn(**item)
            except TypeError as e:
                raise ProjectError(f"op #{i} ({name}): {e}") from e
            results.append({"i": i, "op": name, "ok": str(line)[:120]})
        # collapse the per-op checkpoints back to one
        del pr._undo[depth:]
        out = [_ok(pr, f"batch of {len(ops)} ops"), json.dumps(results)]
        if preview_after and pr.duration() > 0:
            dur = pr.duration()
            times = [dur * (i + 0.5) / 9 for i in range(9)]
            sheet = render.frames_grid(_xml(pr), times, pr.fps, 3)
            data, fmt, _ = render.encode(sheet, "preview")
            out.append(Image(data=data, format=fmt))
    return _result(*out)


def main() -> None:
    host = os.environ.get("NEUROCUT_HOST", "127.0.0.1")
    port = int(os.environ.get("NEUROCUT_PORT", "8766"))
    transport = os.environ.get("NEUROCUT_TRANSPORT", "http")
    token = os.environ.get("NEUROCUT_TOKEN")
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    middleware = None
    if token:
        from starlette.middleware import Middleware
        middleware = [Middleware(_TokenGate, token=token)]
    mcp.run(transport="http", host=host, port=port, middleware=middleware)


if __name__ == "__main__":
    main()
