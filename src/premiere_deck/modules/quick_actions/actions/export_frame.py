from __future__ import annotations

from pathlib import Path, PureWindowsPath

from premiere_deck.modules.quick_actions.action import (
    Action, ActionAbort, require_project, require_sequence, safe_file_stem,
)
from premiere_deck.modules.quick_actions.results import text

FRAMES_DIR = ("Exports", "Frames")


class ExportFrame(Action):
    id = "export_frame"
    label = "Export frame"
    hint = "Saves the frame at the playhead as a PNG in Exports\\Frames and shows it in Explorer."
    group = "Export"
    long = True
    timeout_ms = 60000
    feasibility = (
        "QE sequence exportFramePNG(timecodeString, pathWithoutExtension) (undocumented; used by Adobe's "
        "PProPanel sample; appends .png itself and cuts the file name at its first dot, so a dot-free temporary "
        "name is renamed after); Time.getFormatted for the timecode; ExtendScript File / Folder."
    )

    def run(self, ctx, module):
        sequence = require_sequence(ctx)
        project = require_project(ctx)
        folder = Path(project.root).joinpath(*FRAMES_DIR)
        folder.mkdir(parents=True, exist_ok=True)
        result = self.eval(ctx, {"out_dir": str(folder), "base_name": safe_file_stem(sequence.name)})
        path = text(result, "path")
        if not path:
            raise ActionAbort("error", f"{self.label} failed: Premiere wrote no frame file.")
        ctx.host.reveal(path)
        return self.done(f"Frame saved · {PureWindowsPath(path).name}")
