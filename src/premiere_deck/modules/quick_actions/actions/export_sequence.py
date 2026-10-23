from __future__ import annotations

from pathlib import Path, PureWindowsPath

from premiere_deck.modules.quick_actions.action import (
    Action, ActionAbort, Menu, require_project, require_sequence, safe_file_stem,
)
from premiere_deck.modules.quick_actions.results import text

EXPORTS_DIR = "Exports"
BADGES = {"youtube_1080p": "YT 1080p", "instagram_9x16": "IG 9:16", "tiktok_9x16": "TT 9:16", "match_h264": "H.264"}
BADGE_MAX = 8


class ExportSequence(Action):
    id = "export_sequence"
    label = "Export sequence"
    short = "Export"
    icon = "export-sequence"
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

    def menu(self, module):
        options = tuple((preset.id, preset.label) for preset in module.presets)
        if not options:
            return None
        badges = tuple((preset.id, BADGES.get(preset.id) or preset.label[:BADGE_MAX].rstrip())
                       for preset in module.presets)
        return Menu("preset", "Preset", options[0][0], options, badges)

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
