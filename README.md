# NeuroCut

An MCP server that gives an AI a **multi-track video editor**. It drives
[MLT](https://www.mltframework.org/) — the engine behind Shotcut & Kdenlive — so
the model lays down a timeline with terse tool calls, reviews **cheap downscaled
preview frames** (never the whole video), iterates, and renders the real file to
disk. Same philosophy as a good painting tool, for video.

- **Stateful project** in server memory: video/audio tracks, clips, trims,
  speed, fades, per-clip & per-track filters, transforms (PiP/overlay),
  crossfades, undo/redo.
- **MLT under the hood** → real compositing, `dynamictext` captions, `timewarp`
  speed, `qtblend`, `avfilter.*` (every FFmpeg filter). Projects save as `.mlt`
  and open in Shotcut.
- **Media & fonts from the internet** — `add_media(url=…)`,
  `add_text(font_google="Bebas Neue")`.
- **Token-frugal**: mutations return one line; `preview(at=…)` and
  `storyboard(count=9)` (a contact sheet of the whole edit) are the only things
  that cost image tokens, and they're downscaled; `render_video` costs none.

## Requirements

System packages (not pip): **ffmpeg**, **ffprobe**, and **MLT** with its Python
bindings + the `melt` CLI.

```bash
# Gentoo
sudo tee /etc/portage/package.use/neurocut-mlt <<< 'media-libs/mlt python ffmpeg xml qt6 rubberband opengl sdl gtk vorbis'
sudo emerge media-libs/mlt

# Debian/Ubuntu
sudo apt install melt libmlt++-dev python3-mlt ffmpeg
```

Then:

```bash
python -m venv --system-site-packages .venv   # --system-site-packages so it sees mlt7
. .venv/bin/activate
pip install -e .

python -m neurocut                 # HTTP, 127.0.0.1:8766
NEUROCUT_TRANSPORT=stdio python -m neurocut   # stdio for local Claude Code
```

## Environment

| var | default | meaning |
|---|---|---|
| `NEUROCUT_TRANSPORT` | `http` | `http` or `stdio` |
| `NEUROCUT_HOST` / `NEUROCUT_PORT` | `127.0.0.1` / `8766` | HTTP bind |
| `NEUROCUT_TOKEN` | *(unset)* | if set, require `X-Neurocut-Token` (or `Authorization: Bearer`) header |
| `NEUROCUT_EXPORT` | `~/neurocut-output` | where `render_video` / `save_mlt` write |
| `NEUROCUT_CACHE` | `~/.cache/neurocut` | downloaded media & fonts |
| `NEUROCUT_MAX_DOWNLOAD` | `536870912` | max bytes per fetched asset |

## Connect as a Claude connector

```bash
./run.sh          # starts server + a Cloudflare quick tunnel, prints URL + token
```

In Claude → Settings → Connectors → Add custom connector:
- URL `https://<tunnel>/mcp`
- **Authentication: None**
- **Additional request headers:** `X-Neurocut-Token` = `<token>`

## Tool map

`new_project` · `project_info` · `list_projects` · `delete_project`
`add_media`
`add_track` · `update_track` · `remove_track`
`add_clip` · `add_color` · `add_text` · `move_clip` · `trim_clip` · `split_clip`
· `remove_clip` · `set_clip_speed` · `set_clip_fade` · `set_clip_gain`
`add_filter` · `add_transform` · `clear_filters` · `crossfade`
`preview` · `storyboard` · `render_video` · `save_mlt` · `get_download_link`
· `undo` · `redo`
`batch`

`render_video` / `save_mlt` return a `download_url` (`GET /dl/<token>/<name>`,
opens in a browser with no auth) so the model can hand the finished file to the
user; `get_download_link` re-issues one for an earlier render. Set
`NEUROCUT_PUBLIC_URL` (run.sh does this from the tunnel) so the links are
absolute.

Time is in **seconds**. Track 0 is the bottom video layer; higher video tracks
composite on top. Overlapping clips on one track are auto-laned, so text/PiP
"just works".

## Status / limits (v1)

- Transitions: crossfade (alpha dissolve) + per-clip fades. Wipes/luma TODO.
- `add_transform` uses `qtblend` rect; keyframed motion TODO.
- Audio track-level volume not yet wired (per-clip gain works).
