# Assets

Images referenced by the README, the user guide and the repository page. Each screenshot is a PNG under 500 KB.

| File | Content | Shown at |
|---|---|---|
| `hero.svg` | Banner at the top of the README (1280 x 400), generated from the app icon and the Crystal Black theme colours | README header, `width="100%"` |
| `social-preview.png` | Social preview of the repository (1280 x 640), uploaded in the repository settings | repository settings |
| `screenshot.png` | Main window (720 x 560) on the Files tab in Crystal Black, the module tabs in the title bar, project set up, Auto copy and Auto sort on, version pill v3.0.0 | README header, `width="860"` |
| `panel.png` | The Premiere panel (360 x 640) on the Actions tab in Crystal Black: the pinned row ADJ, CLR, DUP, EXP and the eight lanes | README header, `width="28%"` |
| `idle.png` | Start screen (420 x 560): Launch Premiere Pro and the recent projects, version pill v3.0.0 | README header, `width="36%"` |
| `files.png` | Main window on the Actions tab in Crystal Sky: one visibility switch per quick action in the eight groups (file name kept from 2.0) | user guide, `width="860"` |
| `settings.png` | Settings sheet over the main window: Appearance with the two theme cards and the Scale row, Behaviour with Keep running and Show activity | not referenced yet (kept for the user guide) |
| `panel-files.png` | The panel on the Files tab in Crystal Black: SORT and SYNC with their AUTO capsules, USED, ZIP, SETUP and the Offline group | not referenced yet (kept for the user guide) |

The window screenshots are captured from the app running in its simulated mode (`--fake`, no Premiere Pro) at Scale
100 %: a capture at device scale 2, shrunk to 0.75 and reduced to 256 colours. The panel screenshots are the real
panel page served by `tests\panel\live_server.py`, rendered in headless Chrome at 360 x 640 px, device scale 2 and
shrunk to 0.75 without a palette (a 256-colour palette drops the small mint status dot and lamps).
