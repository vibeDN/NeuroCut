"""Serialize a Project into an MLT XML document (openable in Shotcut, renderable
by `melt`)."""
from __future__ import annotations

import xml.etree.ElementTree as ET

from .model import Clip, Project, Track


def _tc(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _prop(parent: ET.Element, name: str, value) -> None:
    el = ET.SubElement(parent, "property", name=name)
    el.text = str(value)


class _Builder:
    def __init__(self, project: Project):
        self.p = project
        self.fps = project.fps
        self.root = ET.Element("mlt", title=f"NeuroCut-{project.id}",
                               version="7.0.0")
        self._pid = 0
        self._fid = 0
        self._producers: dict[str, ET.Element] = {}
        self.total = max(project.duration(), 0.04)

    def f(self, seconds: float) -> int:
        return max(0, round(float(seconds) * self.fps))

    def _new_pid(self) -> str:
        self._pid += 1
        return f"producer{self._pid}"

    # ---- producers -----------------------------------------------------
    def _filter(self, parent: ET.Element, service: str, props: dict) -> None:
        self._fid += 1
        fel = ET.SubElement(parent, "filter", id=f"filter{self._fid}")
        _prop(fel, "mlt_service", service)
        for k, v in props.items():
            _prop(fel, k, v)

    def _clip_producer(self, clip: Clip) -> str:
        pid = self._new_pid()
        pel = ET.Element("producer", id=pid)
        span = clip.src_span
        if clip.kind == "text":
            _prop(pel, "mlt_service", "color")
            _prop(pel, "resource", "#00000000")
            _prop(pel, "length", _tc(clip.duration + 1))
            _prop(pel, "out", self.f(clip.duration) )
        elif clip.kind == "color":
            _prop(pel, "mlt_service", "color")
            _prop(pel, "resource", clip.props.get("color", "#000000"))
            _prop(pel, "length", _tc(clip.duration + 1))
            _prop(pel, "out", self.f(clip.duration))
        else:
            media = self.p.media[clip.media_id]
            if abs(clip.speed - 1.0) > 1e-3 and clip.kind in ("video", "audio"):
                _prop(pel, "mlt_service", "timewarp")
                _prop(pel, "resource", f"{clip.speed}:{media.path}")
            else:
                _prop(pel, "resource", media.path)
            if clip.kind == "image" or media.info.get("is_image"):
                _prop(pel, "length", self.f(clip.duration) + 2)
                _prop(pel, "in", 0)
                _prop(pel, "out", self.f(clip.duration))
            else:
                _prop(pel, "in", self.f(clip.in_point))
                _prop(pel, "out", max(self.f(clip.in_point),
                                      self.f(clip.in_point) + self.f(span) - 1))

        # per-clip fades (alpha, so they work over lower tracks and background)
        d = clip.duration
        if clip.fade_in > 0 and clip.kind not in ("audio",):
            self._filter(pel, "brightness", {
                "alpha": f"{_tc(0)}=0;{_tc(min(clip.fade_in, d))}=1"})
        if clip.fade_out > 0 and clip.kind not in ("audio",):
            self._filter(pel, "brightness", {
                "alpha": f"{_tc(max(0, d - clip.fade_out))}=1;{_tc(d)}=0"})
        if clip.kind in ("video", "audio") and (clip.fade_in > 0 or clip.fade_out > 0):
            kf = []
            if clip.fade_in > 0:
                kf.append(f"{_tc(0)}=0")
                kf.append(f"{_tc(min(clip.fade_in, d))}=1")
            if clip.fade_out > 0:
                kf.append(f"{_tc(max(0, d - clip.fade_out))}=1")
                kf.append(f"{_tc(d)}=0")
            self._filter(pel, "volume", {"level": ";".join(kf)})
        if clip.gain != 1.0 and clip.kind in ("video", "audio"):
            self._filter(pel, "volume", {"gain": clip.gain})
        if clip.kind == "text":
            self._text_filter(pel, clip)
        for fl in clip.filters:
            self._filter(pel, fl.service, fl.params)

        self._producers[pid] = pel
        return pid

    def _text_filter(self, pel: ET.Element, clip: Clip) -> None:
        pr = clip.props
        halign = pr.get("halign", "center")
        valign = pr.get("valign", "bottom")
        size = int(pr.get("size", max(24, self.p.h // 12)))
        pad = int(pr.get("pad", self.p.h // 20))
        geom = pr.get("geometry", f"0 0 {self.p.w} {self.p.h} 1")
        props = {
            "argument": pr.get("text", ""),
            "geometry": geom,
            "family": pr.get("family", "Sans"),
            "size": size,
            "weight": pr.get("weight", 500),
            "style": "italic" if pr.get("italic") else "normal",
            "fgcolour": pr.get("color", "#ffffffff"),
            "bgcolour": pr.get("bg", "#00000000"),
            "olcolour": pr.get("outline_color", "#aa000000"),
            "outline": pr.get("outline", 2),
            "pad": pad,
            "halign": halign,
            "valign": valign,
        }
        self._filter(pel, "dynamictext", props)

    # ---- tracks ------------------------------------------------------
    @staticmethod
    def _lanes(track: Track) -> list[list]:
        """Split a logical track into lanes of non-overlapping clips so
        stacked/overlapping clips (text over video, PiP) render correctly."""
        lanes: list[list] = []
        for clip in track.sorted_clips():
            for lane in lanes:
                if lane[-1].end <= clip.start + 1e-4:
                    lane.append(clip)
                    break
            else:
                lanes.append([clip])
        return lanes or [[]]

    def _lane_playlist(self, track: Track, lane: list, tag: str) -> ET.Element:
        pl = ET.Element("playlist", id=f"pl_{track.id}_{tag}")
        pos = 0
        for clip in lane:
            gap = self.f(clip.start) - pos
            if gap > 0:
                ET.SubElement(pl, "blank", length=str(gap))
                pos += gap
            pid = self._clip_producer(clip)
            nframes = max(1, self.f(clip.duration))
            ET.SubElement(pl, "entry", producer=pid,
                          **{"in": "0", "out": str(nframes - 1)})
            pos += nframes
        if tag == "0":
            for fl in track.filters:
                self._filter(pl, fl.service, fl.params)
        return pl

    def build(self) -> str:
        # profile
        prof = ET.SubElement(self.root, "profile",
                             description=f"{self.p.w}x{self.p.h} {self.fps}fps",
                             width=str(self.p.w), height=str(self.p.h),
                             progressive="1",
                             sample_aspect_num="1", sample_aspect_den="1",
                             display_aspect_num=str(self.p.w),
                             display_aspect_den=str(self.p.h),
                             frame_rate_num=str(int(round(self.fps * 1000))),
                             frame_rate_den="1000", colorspace="709")

        total_f = max(1, self.f(self.total))
        bg = ET.Element("producer", id="bg")
        _prop(bg, "mlt_service", "color")
        _prop(bg, "resource", self.p.background)
        _prop(bg, "length", str(total_f + 2))
        _prop(bg, "out", str(total_f - 1))

        # build lane playlists, bottom video tracks -> top, then audio
        lane_pls: list[tuple[Track, ET.Element]] = []
        v_tracks = [t for t in self.p.tracks if t.kind == "video"]
        a_tracks = [t for t in self.p.tracks if t.kind == "audio"]
        for tr in v_tracks + a_tracks:
            for li, lane in enumerate(self._lanes(tr)):
                lane_pls.append((tr, self._lane_playlist(tr, lane, str(li))))

        self.root.append(bg)
        for pel in self._producers.values():
            self.root.append(pel)
        for _, pl in lane_pls:
            self.root.append(pl)

        tractor = ET.SubElement(self.root, "tractor", id="tractor0")
        ET.SubElement(tractor, "track", producer="bg")
        for tr, pl in lane_pls:
            hide = ""
            if tr.kind == "video" and tr.hidden:
                hide = "video"
            elif tr.kind == "audio" and tr.muted:
                hide = "audio"
            attrs = {"producer": pl.get("id")}
            if hide:
                attrs["hide"] = hide
            ET.SubElement(tractor, "track", **attrs)

        tid = 0
        for i, (tr, _pl) in enumerate(lane_pls):
            mlt_track = i + 1  # +1 for bg at track 0
            trn = ET.SubElement(tractor, "transition", id=f"trans{tid}")
            tid += 1
            if tr.kind == "video":
                _prop(trn, "mlt_service", "qtblend")
                _prop(trn, "a_track", "0")
                _prop(trn, "b_track", str(mlt_track))
                _prop(trn, "compositing", "0")
                if tr.opacity < 1.0:
                    _prop(trn, "opacity", tr.opacity)
            else:
                _prop(trn, "mlt_service", "mix")
                _prop(trn, "a_track", "0")
                _prop(trn, "b_track", str(mlt_track))
                _prop(trn, "sum", "1")

        ET.indent(self.root, space="  ")
        return ET.tostring(self.root, encoding="unicode", xml_declaration=True)


def to_xml(project: Project) -> str:
    return _Builder(project).build()
