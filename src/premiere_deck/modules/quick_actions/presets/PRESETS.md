# Export presets of `export_sequence`

Owner: actions worker. Resolution order per built-in preset (`presets.py`): the `.epr` in this folder, then
Adobe's own system preset of the installed Premiere Pro or Media Encoder (`<Program Files>\Adobe\<app>\MediaIO\
systempresets\<folder>\<file>`, read only, newest version first, Premiere before Media Encoder of the same year).
A built-in preset without any file is left out of the select. Every `*.epr` in `<data>\presets\` follows as an
extra option labelled with its file name (id `user:<stem>`); the host window's Actions page opens that folder and
rescans it.

## Built-in presets

| id | Label | File in this folder | Fallback system preset (`4E49434B_48323634` = H.264) |
|---|---|---|---|
| `youtube_1080p` | YouTube 1080p | `YouTube 1080p.epr` | `YouTube 1080p HD.epr` |
| `instagram_9x16` | Instagram 9:16 | `Instagram 9x16.epr` | `01 - Match Source - High bitrate.epr` |
| `tiktok_9x16` | TikTok 9:16 | `TikTok 9x16.epr` | `01 - Match Source - High bitrate.epr` |
| `match_h264` | Match source H.264 | `Match source H264.epr` | `01 - Match Source - High bitrate.epr` |

Both fallback files exist in Premiere Pro 2026 and Media Encoder 2026 on the development machine (checked
2026-10-21). With the fallbacks, the two 9:16 options export the sequence's own frame size, which is 1080x1920 for
a copy made with Social copy 9:16 from a 1080p sequence.

## Status: not shipped, system fallback in use (decided in phase 2, 2026-10-22)

No `.epr` is in this folder. ExtendScript and QE have no call that saves an export preset (checked on 26.5.2), and
Premiere's Export dialog saves a preset into the user's own preset folder
(`Documents\Adobe\Adobe Media Encoder\<version>\Presets`), so no script makes these files without touching the
user's presets. `export_sequence` therefore runs on the fallback system presets of the table above; phase 2
queued jobs with `YouTube 1080p` and `Instagram 9:16` (fallback files) on Premiere 26.5.2.

Manual steps to make the four files (a person at the Premiere screen, on a machine where adding presets to the
user folder is acceptable): open a 1080p sequence, File > Export > Media (Export mode), Format H.264, apply the
settings of the table below, Preset menu > Save Preset, name it exactly as the "File in this folder" column,
"Save Effect Settings" off; then copy the file from the user preset folder into this folder and delete it there.
Adobe's own `.epr` files are not copied into the repository (they are part of Adobe's software); the shipped files
must be presets saved by us.

Settings (fields of Premiere's Export Settings, H.264 format, everything not listed keeps the H.264 default):

| Field | YouTube 1080p | Instagram 9x16 | TikTok 9x16 | Match source H264 |
|---|---|---|---|---|
| Base preset | YouTube 1080p Full HD | Match Source - High bitrate | Match Source - High bitrate | Match Source - High bitrate |
| Frame size | 1920 x 1080 | 1080 x 1920 | 1080 x 1920 | match source |
| Frame rate | match source | match source (max 60) | match source (max 60) | match source |
| Field order / aspect | progressive / square pixels | progressive / square pixels | progressive / square pixels | match source |
| Profile / level | High / 4.2 | High / 4.2 | High / 4.2 | High / 5.1 |
| Bitrate encoding | VBR, 1 pass | VBR, 1 pass | VBR, 1 pass | VBR, 1 pass |
| Target / maximum bitrate | 16 / 20 Mbps | 10 / 12 Mbps | 12 / 16 Mbps | 20 / 24 Mbps |
| Key frame distance | automatic | automatic | automatic | automatic |
| Audio | AAC, 48 kHz, stereo, 320 kbps | AAC, 48 kHz, stereo, 256 kbps | AAC, 48 kHz, stereo, 256 kbps | AAC, 48 kHz, stereo, 320 kbps |
| Use Maximum Render Quality | on | on | on | on |
| Multiplexer | MP4 | MP4 | MP4 | MP4 |

After exporting: check each file with `Sequence.getExportFileExtension(<path>)` (returns `mp4`), then run
`export_sequence` once per preset on a scratch project (phase 2 checklist, `reports\actions.md`).
