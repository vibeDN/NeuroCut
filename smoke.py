"""End-to-end smoke test via FastMCP in-memory client."""
import asyncio
import base64
import json
import os

from fastmcp import Client

from neurocut.server import mcp

OUT = os.path.join(os.path.dirname(__file__), "_smoke_out")
os.makedirs(OUT, exist_ok=True)

VIDEO = "https://test-videos.co.uk/vids/bigbuckbunny/mp4/h264/360/Big_Buck_Bunny_360_10s_1MB.mp4"
IMG = "https://www.gstatic.com/webp/gallery/1.jpg"


def save(res, name):
    for b in res.content:
        if getattr(b, "type", "") == "text":
            print(f"  [{name}] {b.text[:200]}")
        elif getattr(b, "type", "") == "image":
            raw = base64.b64decode(b.data)
            p = os.path.join(OUT, f"{name}.{b.mimeType.split('/')[-1]}")
            open(p, "wb").write(raw)
            print(f"  [{name}] image {b.mimeType} {len(raw)}B -> {p}")


async def main():
    async with Client(mcp) as c:
        print("TOOLS:", [t.name for t in await c.list_tools()])

        r = await c.call_tool("new_project", {"width": 1280, "height": 720, "fps": 30})
        pid = json.loads(r.content[0].text)["project_id"]
        print("project", pid)

        rv = await c.call_tool("add_media", {"url": VIDEO})
        vid = json.loads(rv.content[0].text)
        print("media video:", vid)
        ri = await c.call_tool("add_media", {"url": IMG})
        img = json.loads(ri.content[0].text)
        print("media image:", img["media_id"], img.get("is_image"))

        await c.call_tool("add_clip", {"media_id": vid["media_id"], "track": 0,
                                       "in_point": 1.0, "out_point": 5.0})
        await c.call_tool("add_clip", {"media_id": vid["media_id"], "track": 0,
                                       "in_point": 6.0, "out_point": 9.0})
        r2 = await c.call_tool("project_info", {})
        clips = json.loads(r2.content[0].text)["tracks"][0]["clips"]
        c0, c1 = clips[0]["id"], clips[1]["id"]

        rb = await c.call_tool("batch", {"ops": [
            {"op": "add_track", "kind": "video"},
            {"op": "add_image", "media_id": img["media_id"], "track": 2,
             "start": 1.0, "duration": 3.0} if False else
            {"op": "add_clip", "media_id": img["media_id"], "track": 2,
             "start": 1.0, "out_point": 3.0},
            {"op": "add_text", "text": "BIG BUCK BUNNY", "track": 2, "start": 0.5,
             "duration": 3.0, "size": 90,
             "position": "top", "color": "#ffdd33ff"},
            {"op": "add_filter", "target": "track0",
             "service": "saturation", "params": {"level": 1.4}},
            {"op": "set_clip_fade", "clip_id": c0, "fade_in": 0.5},
        ]})
        save(rb, "batch")

        await c.call_tool("crossfade", {"clip_a": c0, "clip_b": c1, "duration": 1.0})
        await c.call_tool("set_clip_speed", {"clip_id": c1, "speed": 1.5})

        for d in ("thumb", "preview"):
            rp = await c.call_tool("preview", {"at": 2.0, "detail": d})
            save(rp, f"preview_{d}")

        rs = await c.call_tool("storyboard", {"count": 9, "detail": "preview"})
        save(rs, "storyboard")

        u = await c.call_tool("undo", {"steps": 1})
        print(" ", u.content[0].text[:100])
        await c.call_tool("redo", {"steps": 1})

        rm = await c.call_tool("save_mlt", {"filename": os.path.join(OUT, "proj.mlt")})
        print(" ", rm.content[0].text)
        rr = await c.call_tool("render_video", {"filename": os.path.join(OUT, "out.mp4"),
                                                "preset": "mp4"})
        print(" ", rr.content[0].text)
    print("\nSMOKE OK")


asyncio.run(main())
