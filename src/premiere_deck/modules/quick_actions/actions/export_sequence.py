from __future__ import annotations

from pathlib import Path, PureWindowsPath

from premiere_deck.modules.quick_actions.action import (
    Action, ActionAbort, Control, require_project, require_sequence, safe_file_stem,
)
from premiere_deck.modules.quick_actions.results import text

EXPORTS_DIR = "Exports"
NO_PRESETS = "No presets found"


class ExportSequence(Action):
    id = "export_sequence"
    label = "Export sequence"
    hint = "Queues the active sequence in Media Encoder into the Exports folder (in to out when set)."
    group = "Export"
    long = True
    timeout_ms = 120000  # the first launchEncoder starts Media Encoder
    feasibility = (
        "Encoder.launchEncoder(), Encoder.encodeSequence(sequence, path, presetPath, workAreaType, "
        "removeOnCompletion) with ENCODE_ENTIRE / ENCODE_IN_TO_OUT, and Sequence.getExportFileExtension(preset) "
        "(documented, ppro-scripting, Premiere 14+). The job is queued, not started (as Queue in Premiere's "
        "Export dialog)."
    )

    def controls(self, module):
        options = tuple((preset.id, preset.label) for preset in module.presets)
        if not options:
            return [Control("preset", "select", "Preset", "", (("", NO_PRESETS),))]
        return [Control("preset", "select", "Preset", options[0][0], options)]

    def button(self, module) -> dict:
        if not module.presets:
            return {"disabled": True}
        return {}

    def run(self, ctx, module):
        preset = module.preset(module.choice(self, "preset"))
        if preset is None:
            raise ActionAbort("warn", "No export presets found")
        sequence = require_sequence(ctx)
        project = require_project(ctx)
        folder = Path(project.root) / EXPORTS_DIR
        folder.mkdir(parents=True, exist_ok=True)
        in_to_out = (sequence.in_point is not None and sequence.out_point is not None
                     and sequence.out_point > sequence.in_point)
        result = self.eval(ctx, {"sequence_id": sequence.sequence_id, "preset": preset.path, "out_dir": str(folder),
                                 "base_name": safe_file_stem(sequence.name), "in_to_out": in_to_out})
        name = PureWindowsPath(text(result, "path")).name or sequence.name
        return self.done(f"Queued in Media Encoder · {name}")
