"""In-memory editing project: media, tracks, clips, filters, transitions, undo."""
from __future__ import annotations

import copy
import threading
import time
import uuid
from dataclasses import dataclass, field

MAX_HISTORY = 40
MAX_DIM = 7680


def _uid(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


@dataclass
class Media:
    id: str
    path: str
    origin: str
    info: dict


@dataclass
class Filter:
    id: str
    service: str
    params: dict = field(default_factory=dict)


@dataclass
class Clip:
    id: str
    kind: str                 # video | audio | image | text | color
    start: float              # position on its track, seconds
    in_point: float           # trim in within source, seconds
    out_point: float          # trim out within source, seconds
    media_id: str | None = None
    speed: float = 1.0
    gain: float = 1.0         # linear audio gain
    fade_in: float = 0.0
    fade_out: float = 0.0
    props: dict = field(default_factory=dict)   # text/color params
    filters: list[Filter] = field(default_factory=list)

    @property
    def src_span(self) -> float:
        return max(0.0, self.out_point - self.in_point)

    @property
    def duration(self) -> float:
        return self.src_span / max(0.01, self.speed)

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class Track:
    id: str
    kind: str                 # video | audio
    name: str
    clips: list[Clip] = field(default_factory=list)
    muted: bool = False
    hidden: bool = False
    volume: float = 1.0
    opacity: float = 1.0
    filters: list[Filter] = field(default_factory=list)

    def sorted_clips(self) -> list[Clip]:
        return sorted(self.clips, key=lambda c: c.start)


@dataclass
class Transition:
    id: str
    a: str                    # clip id
    b: str                    # clip id
    kind: str = "dissolve"    # dissolve | fade | wipe_left | wipe_right | wipe_up | wipe_down
    duration: float = 1.0


class ProjectError(ValueError):
    pass


class Project:
    def __init__(self, width=1920, height=1080, fps=30, background="#000000"):
        if not (16 <= width <= MAX_DIM and 16 <= height <= MAX_DIM):
            raise ProjectError(f"resolution out of range (16..{MAX_DIM})")
        self.id = uuid.uuid4().hex[:8]
        self.w, self.h = int(width), int(height)
        self.fps = float(fps)
        self.background = background
        self.media: dict[str, Media] = {}
        self.tracks: list[Track] = [
            Track(_uid("t"), "video", "V1"),
            Track(_uid("t"), "audio", "A1"),
        ]
        self.transitions: list[Transition] = []
        self.created = time.time()
        self.ops = 0
        self.lock = threading.RLock()
        self._undo: list[tuple[str, dict]] = []
        self._redo: list[tuple[str, dict]] = []

    # ---- lookup ---------------------------------------------------------
    def track(self, index: int) -> Track:
        if not (0 <= index < len(self.tracks)):
            raise ProjectError(f"track {index} out of range (0..{len(self.tracks) - 1})")
        return self.tracks[index]

    def find_clip(self, clip_id: str) -> tuple[Track, Clip]:
        for tr in self.tracks:
            for c in tr.clips:
                if c.id == clip_id:
                    return tr, c
        raise ProjectError(f"no clip {clip_id!r}")

    def media_or_die(self, media_id: str) -> Media:
        if media_id not in self.media:
            raise ProjectError(f"no media {media_id!r}; add_media first")
        return self.media[media_id]

    # ---- history -------------------------------------------------------
    def _state(self) -> dict:
        return {
            "w": self.w, "h": self.h, "fps": self.fps, "background": self.background,
            "media": copy.deepcopy(self.media),
            "tracks": copy.deepcopy(self.tracks),
            "transitions": copy.deepcopy(self.transitions),
        }

    def _restore(self, s: dict) -> None:
        self.w, self.h, self.fps = s["w"], s["h"], s["fps"]
        self.background = s["background"]
        self.media = s["media"]
        self.tracks = s["tracks"]
        self.transitions = s["transitions"]

    def checkpoint(self, op: str) -> None:
        self._undo.append((op, self._state()))
        if len(self._undo) > MAX_HISTORY:
            self._undo.pop(0)
        self._redo.clear()
        self.ops += 1

    def undo(self) -> str:
        if not self._undo:
            return "nothing to undo"
        op, s = self._undo.pop()
        self._redo.append((op, self._state()))
        self._restore(s)
        return f"undid '{op}'"

    def redo(self) -> str:
        if not self._redo:
            return "nothing to redo"
        op, s = self._redo.pop()
        self._undo.append((op, self._state()))
        self._restore(s)
        return f"redid '{op}'"

    # ---- helpers ------------------------------------------------------
    def append_position(self, tr: Track) -> float:
        return max((c.end for c in tr.clips), default=0.0)

    def duration(self) -> float:
        return max((c.end for tr in self.tracks for c in tr.clips), default=0.0)

    def summary(self) -> dict:
        return {
            "project_id": self.id,
            "resolution": [self.w, self.h],
            "fps": self.fps,
            "duration": round(self.duration(), 3),
            "background": self.background,
            "media": [
                {"id": m.id, "origin": m.origin,
                 "duration": m.info.get("duration"),
                 "res": [m.info.get("width"), m.info.get("height")],
                 "has_audio": m.info.get("has_audio"),
                 "is_image": m.info.get("is_image", False)}
                for m in self.media.values()
            ],
            "tracks": [
                {"index": i, "kind": tr.kind, "name": tr.name,
                 "muted": tr.muted, "hidden": tr.hidden,
                 "volume": tr.volume, "opacity": tr.opacity,
                 "clips": [
                     {"id": c.id, "kind": c.kind, "media_id": c.media_id,
                      "start": round(c.start, 3), "duration": round(c.duration, 3),
                      "end": round(c.end, 3),
                      "in": round(c.in_point, 3), "out": round(c.out_point, 3),
                      "speed": c.speed, "gain": round(c.gain, 3),
                      "fade_in": c.fade_in, "fade_out": c.fade_out,
                      "filters": [f.service for f in c.filters],
                      "props": c.props}
                     for c in tr.sorted_clips()
                 ]}
                for i, tr in enumerate(self.tracks)
            ],
            "transitions": [
                {"id": t.id, "a": t.a, "b": t.b, "kind": t.kind,
                 "duration": t.duration}
                for t in self.transitions
            ],
            "ops": self.ops, "undo_depth": len(self._undo),
            "redo_depth": len(self._redo),
        }


def new_filter(service: str, params: dict | None = None) -> Filter:
    return Filter(_uid("f"), service, dict(params or {}))


def new_clip(**kw) -> Clip:
    kw.setdefault("id", _uid("c"))
    return Clip(**kw)


def new_track(kind: str, name: str) -> Track:
    return Track(_uid("t"), kind, name)


def new_transition(a: str, b: str, kind: str, duration: float) -> Transition:
    return Transition(_uid("x"), a, b, kind, duration)


def new_media(path: str, origin: str, info: dict) -> Media:
    return Media(_uid("m"), path, origin, info)
