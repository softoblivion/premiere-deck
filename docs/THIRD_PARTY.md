# Third-party components

| Component | License | How it ships |
|---|---|---|
| [CPython](https://www.python.org/) | PSF-2.0 | bundled into the exe |
| [pywebview](https://github.com/r0x0r/pywebview) | BSD-3-Clause | bundled into the exe |
| [pythonnet](https://github.com/pythonnet/pythonnet), clr_loader | MIT | bundled into the exe |
| bottle, proxy_tools | MIT | bundled into the exe |
| [websockets](https://github.com/python-websockets/websockets) | BSD-3-Clause | bundled into the exe |
| [watchdog](https://github.com/gorakhargosh/watchdog) | Apache-2.0 | bundled into the exe |
| [NumPy](https://numpy.org/) | BSD-3-Clause | bundled into the exe |
| [ONNX Runtime](https://github.com/microsoft/onnxruntime) (CPU) | MIT | bundled into the exe |
| [YAMNet](https://www.kaggle.com/models/google/yamnet) audio classifier (ONNX export; source and checksums in `src/premiere_deck/modules/files/models/LICENSE-yamnet.txt`) | Apache-2.0; AudioSet class labels CC BY 4.0 | bundled into the exe |
| [PyInstaller](https://pyinstaller.org/) bootloader | GPL-2.0-or-later with the bootloader exception | bundled into the exe |
| Liquid UI kit copy | MIT | bundled into the exe |
| [Sora](https://github.com/sora-xor/sora-font) and [Geist Mono](https://github.com/vercel/geist-font) fonts | SIL OFL 1.1 | bundled into the exe (window and panel) |
| `CSInterface.js` ([Adobe CEP](https://github.com/Adobe-CEP/CEP-Resources)) | Adobe license, see the file header | bundled into the exe as part of the panel |
| [FFmpeg](https://ffmpeg.org/) / ffprobe (static GPL build from [yt-dlp/FFmpeg-Builds](https://github.com/yt-dlp/FFmpeg-Builds)) | GPL, see the build | read from the data folder or PATH; downloaded once when none is found |
| Microsoft Edge WebView2 Runtime | Microsoft licence | part of Windows; not shipped |
