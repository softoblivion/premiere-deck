"""Audio module (notes/docs/AUDIO_EXPORT.md): quick export of audio tracks of the active sequence chosen in the
panel, as separate files or one mix, through Premiere's own renderer (`Sequence.exportAsMediaDirect` with an
audio-only system preset). One script per output file saves, sets and restores the track mutes; the host renames
each finished file to its final name and never overwrites; a silence check with ffmpeg warns about stems a solo
button in Premiere may have silenced. The user's media is never touched: the module writes only into
`<root>\\<exports_dir>\\Audio`."""

from __future__ import annotations

import dataclasses
import time
from pathlib import Path
from typing import Any, Optional

from premiere_deck.modules.audio_export import plan
from premiere_deck.modules.audio_export.plan import AudioFormat, Pass
from premiere_deck.modules.base import ActionResult, Module, ModuleContext, ModuleThreadPremiere
from premiere_deck.premiere.model import AudioTrack, PremiereError, PremiereUnavailable, SequenceInfo

JSX_DIR = Path(__file__).parent / "jsx"
CHOICES_KEY = "choices"
NOT_CONNECTED = "Premiere Pro is not connected"
INTERNAL_ERROR = "An internal error occurred. Details are in the log."
SOLO_DETAIL = ("A track soloed in Premiere silences the other tracks in an export. Turn solo off in the timeline "
               "and export again.")
UNRESTORED_DETAIL = "The track mutes of {name} may not be restored. Check them in the timeline."
EXPORT = "export"
STOP = "export.stop"
LABEL = "Export audio"
CHOICES = {
    "mode": (plan.MODES, plan.MODE_SEPARATE),
    "format": (tuple((fmt.id, fmt.label) for fmt in plan.FORMATS), "wav"),
    "range": (plan.RANGES, plan.RANGE_INOUT),
}
REFRESH_EVENTS = ("project.opened", "sequence.activated", "sequence.structure_changed", "panel.opened")


class _Stopped(Exception):
    """The user pressed Stop; carries how many files were written."""


class AudioExportModule(Module):
    id = "audio"
    title = "Audio"
    icon = "audio"
    description = "Exports chosen audio tracks of the active sequence as separate files or one mix."
    order = 7
    events = REFRESH_EVENTS
    requires = ("ffmpeg",)  # the silence check only; the export itself runs in Premiere

    def __init__(self, ctx: ModuleContext) -> None:
        super().__init__(ctx)
        self.sequence: Optional[SequenceInfo] = None
        self.tracks: list[AudioTrack] = []
        # The tracks chosen per sequence id, for this session only (AUDIO_EXPORT.md 2).
        self.chosen: dict[str, set[int]] = {}
        self.job: Any = None
        self.progress_value: Optional[float] = None
        self.progress_label = ""
        self.loaded = False

    # --- lifecycle -------------------------------------------------------------------------------------------

    def start(self) -> None:
        self.ctx.post(self.refresh)

    def stop(self) -> None:
        self._cancel()

    def project_opened(self, project) -> None:
        self.ctx.post(self.refresh)

    def project_closed(self) -> None:
        self._cancel()
        self.sequence = None
        self.tracks = []
        self.chosen.clear()
        self.loaded = False
        self.ctx.ui.invalidate()

    def on_event(self, name: str, data: Any) -> None:
        if name in REFRESH_EVENTS and self.job is None:
            self.ctx.post(self.refresh)

    def _cancel(self) -> None:
        cancel = getattr(self.job, "cancel", None)
        if callable(cancel):
            cancel()

    # --- state -----------------------------------------------------------------------------------------------

    def refresh(self) -> None:
        """On the module thread: the active sequence and its audio tracks. Premiere not answering keeps the last
        known state (the next event or press reads again)."""
        try:
            sequence = self.ctx.premiere.active_sequence()
            tracks = self.ctx.premiere.audio_tracks(sequence.sequence_id) if sequence else []
        except PremiereError as exc:
            self.log.debug("audio tracks not read: %s", exc.message)
            return
        self.sequence, self.tracks, self.loaded = sequence, list(tracks), True
        self.ctx.ui.invalidate()

    def chosen_tracks(self) -> set[int]:
        if self.sequence is None:
            return set()
        known = {track.index for track in self.tracks}
        if self.sequence.sequence_id not in self.chosen:
            return plan.default_choice(self.tracks)
        return self.chosen[self.sequence.sequence_id] & known

    def choice(self, name: str) -> str:
        options, default = CHOICES[name]
        stored = self.ctx.settings.get(CHOICES_KEY, {})
        value = stored.get(name) if isinstance(stored, dict) else None
        return value if isinstance(value, str) and value in dict(options) else default

    def set_choice(self, name: str, value: Any) -> bool:
        options, _ = CHOICES[name]
        if not isinstance(value, str) or value not in dict(options):
            return False
        stored = self.ctx.settings.get(CHOICES_KEY, {})
        stored = dict(stored) if isinstance(stored, dict) else {}
        stored[name] = value
        self.ctx.settings.set(CHOICES_KEY, stored)
        return True

    def format(self) -> AudioFormat:
        return plan.FORMAT_BY_ID[self.choice("format")]

    # --- actions ---------------------------------------------------------------------------------------------

    def on_action(self, action_id: str, value: Any = None, row: Optional[str] = None) -> Optional[ActionResult]:
        local = self.strip_prefix(action_id)
        if local.startswith("track."):
            return self._toggle_track(local[len("track."):], value)
        name, _, part = local.partition(".")
        if part == "choice" and name in CHOICES:
            self.set_choice(name, value)
            return None
        if local in CHOICES:
            # A press on a choice tile steps to its next option; the corner menu picks one directly.
            options = [value for value, _ in CHOICES[local][0]]
            self.set_choice(local, options[(options.index(self.choice(local)) + 1) % len(options)])
            return None
        if local == EXPORT:
            return self.export()
        if local == STOP:
            self._cancel()
            return None
        if local == "open_folder":
            return self.open_folder()
        return super().on_action(action_id, value, row)

    def _toggle_track(self, index_text: str, value: Any) -> Optional[ActionResult]:
        if self.sequence is None or not index_text.isdigit() or not isinstance(value, bool):
            return None
        index = int(index_text)
        if index not in {track.index for track in self.tracks}:
            return None
        chosen = set(self.chosen_tracks())
        chosen.add(index) if value else chosen.discard(index)
        self.chosen[self.sequence.sequence_id] = chosen
        return None

    def _result(self, outcome: str, text: str, detail: Optional[str] = None, button: str = EXPORT) -> ActionResult:
        self.ctx.ui.result(button, outcome, text, detail)
        return ActionResult(toast=None)

    def _root(self) -> Optional[str]:
        project = None
        try:
            project = self.ctx.premiere.project()
        except PremiereError:
            project = None
        project = project or self.ctx.project
        return project.root if project is not None else None

    def open_folder(self) -> ActionResult:
        root = self._root()
        if root is None:
            return self._result("warn", "No project open", button="open_folder")
        folder = plan.audio_folder(root)
        if not folder.is_dir():
            return self._result("neutral", "No audio exported yet", button="open_folder")
        self.ctx.host.open_path(str(folder))
        return self._result("neutral", f"Opened {folder.name}", button="open_folder")

    def export(self) -> ActionResult:
        """A press on Export audio: preconditions on the module thread, the files in a job."""
        if self.job is not None:
            return ActionResult(toast=None, rerender=False)
        try:
            root = self._root()
            if root is None:
                return self._result("warn", "No project open")
            sequence = self.ctx.premiere.active_sequence()
            self.tracks = list(self.ctx.premiere.audio_tracks(sequence.sequence_id)) if sequence else []
            self.sequence, self.loaded = sequence, True
        except PremiereUnavailable:
            return self._result("warn", NOT_CONNECTED)
        except PremiereError as exc:
            self.log.warning("audio export precheck: %s", exc.message)
            return self._result("error", "Audio export failed", plan.error_sentence(exc.message))
        if sequence is None:
            return self._result("warn", "No active sequence")
        if not self.tracks:
            return self._result("warn", "No audio tracks")
        fmt = self.format()
        passes = plan.passes(sequence, self.tracks, self.chosen_tracks(), self.choice("mode"), fmt)
        if not passes:
            return self._result("warn", "Choose an audio track")
        area = plan.work_area(sequence, self.choice("range"))
        folder = plan.audio_folder(root)
        job_ctx = dataclasses.replace(self.ctx, premiere=ModuleThreadPremiere(self.ctx.premiere))
        self.progress_value, self.progress_label = 0.0, f"{passes[0].label} · 1 of {len(passes)}"
        pending = object()
        self.job = pending

        def work(job: Any) -> tuple[str, str, Optional[str], Optional[str]]:
            return self._run(job, job_ctx, sequence, passes, fmt, area, folder)

        def finish(job: Any, outcome: Any) -> None:
            self.job = None
            tone, text, detail, reveal = outcome
            if reveal:
                self.ctx.host.reveal(reveal)
            self.ctx.ui.result(EXPORT, tone, text, detail)
            self.ctx.post(self.refresh)
            self.ctx.ui.invalidate()

        def fail(job: Any, exc: BaseException) -> None:
            self.log.error("audio export failed in its job: %s", exc)
            finish(job, ("error", "Audio export failed", INTERNAL_ERROR, None))

        job = self.ctx.jobs.submit(work, title=LABEL, on_done=finish, on_error=fail)
        if self.job is pending:
            self.job = job
        self.ctx.ui.invalidate()
        return ActionResult(toast=None)

    def _run(self, job: Any, ctx: ModuleContext, sequence: SequenceInfo, passes: list[Pass], fmt: AudioFormat,
             area: int, folder: Path) -> tuple[str, str, Optional[str], Optional[str]]:
        """The job: the preset, then one script per file; Stop takes effect between files."""
        written: list[tuple[Pass, Path]] = []
        unrestored = False
        try:
            preset = ctx.premiere.eval("audio.preset", ctx.premiere.script(str(JSX_DIR / "preset.jsx")),
                                       {"folder": fmt.folder, "file": fmt.file, "label": fmt.badge})
            preset_path = str(preset.get("path") or "") if isinstance(preset, dict) else ""
            if not preset_path:
                return "error", "Audio export failed", f"The {fmt.badge} preset of Premiere was not found.", None
            folder.mkdir(parents=True, exist_ok=True)
            for leftover in plan.stale_temps(folder, time.time()):
                self.log.info("audio export: removing the leftover %s", leftover.name)
                _remove_own(leftover)
            code =ctx.premiere.script(str(JSX_DIR / "export_audio.jsx"))
            timeout = plan.timeout_ms(plan.range_seconds(sequence, area))
            for number, item in enumerate(passes, start=1):
                if getattr(job, "cancelled", False):
                    raise _Stopped()
                self._progress(job, (number - 1) / len(passes), f"{item.label} · {number} of {len(passes)}")
                temp = plan.temp_path(folder, f"{int(time.time() * 1000)}{number}", fmt)
                try:
                    result = ctx.premiere.eval("audio.export_audio", code, {
                        "sequence_id": sequence.sequence_id, "audible": list(item.tracks), "preset": preset_path,
                        "temp_path": str(temp), "work_area": area}, timeout_ms=timeout)
                    if not (isinstance(result, dict) and result.get("written")) or not temp.is_file():
                        raise PremiereError("deck: Premiere wrote no audio file.")
                    if isinstance(result, dict) and result.get("restored") is False:
                        self.log.warning("audio export: the mute states of %s may not be restored", sequence.name)
                        unrestored = True
                    final = self._rename(temp, folder, item.name)
                except BaseException:
                    _remove_own(temp)
                    raise
                written.append((item, final))
            self._progress(job, 1.0, f"{len(passes)} of {len(passes)}")
        except _Stopped:
            text = f"Audio export stopped · {plan.plural(len(written), 'file')} written"
            if unrestored:
                return "warn", text, UNRESTORED_DETAIL.format(name=sequence.name), None
            return "neutral", text, None, None
        except PremiereUnavailable:
            return "warn", NOT_CONNECTED, self._written_note(written), None
        except PremiereError as exc:
            self.log.warning("audio export: script error %s (line %s) %s", exc.message, exc.line, exc.source)
            detail = plan.error_sentence(exc.message)
            note = self._written_note(written)
            return "error", "Audio export failed", f"{detail} {note}" if note else detail, None
        silent = self._silent(ctx, written)
        last = str(written[-1][1]) if written else None
        if len(passes) == 1 and passes[0].label == "Mix":
            text = f"Exported mix of {plan.plural(len(passes[0].tracks), 'track')} · {fmt.badge}"
        else:
            text = f"Exported {plan.plural(len(written), 'file')} · {fmt.badge}"
        mutes = UNRESTORED_DETAIL.format(name=sequence.name) if unrestored else None
        if silent:
            names = ", ".join(silent)
            detail = f"{SOLO_DETAIL} {mutes}" if mutes else SOLO_DETAIL
            return "warn", f"{text} · {names} {'is' if len(silent) == 1 else 'are'} silent", detail, last
        if mutes:
            return "warn", text, mutes, last
        return "ok", text, None, last

    def _progress(self, job: Any, value: float, label: str) -> None:
        self.progress_value, self.progress_label = value, label
        job.progress(value, label)
        self.ctx.ui.invalidate()

    @staticmethod
    def _written_note(written: list) -> Optional[str]:
        return f"{plan.plural(len(written), 'file was', 'files were')} written before." if written else None

    def _rename(self, temp: Path, folder: Path, name: str) -> Path:
        """The finished file under its final name, never over an existing file ("name (2).wav")."""
        for _ in range(100):
            final = plan.free_path(folder, name)
            try:
                temp.rename(final)  # Windows refuses an existing target
                return final
            except FileExistsError:
                continue
        raise PremiereError("deck: The audio file could not be renamed.")

    def _silent(self, ctx: ModuleContext, written: list[tuple[Pass, Path]]) -> list[str]:
        """The labels of written files whose loudest sample is at or below -60 dB (ffmpeg volumedetect)."""
        if not written or not ctx.tools.has("ffmpeg"):
            return []
        silent = []
        for item, path in written:
            lines: list[str] = []
            try:
                code = ctx.tools.ffmpeg(plan.silence_args(str(path)), timeout=300, on_line=lines.append)
            except Exception:  # noqa: BLE001 - the check is advisory
                self.log.exception("silence check of %s failed", path)
                continue
            if code == 0 and plan.is_silent(plan.max_volume(lines)):
                silent.append(item.label)
        return silent

    # --- rendering -------------------------------------------------------------------------------------------

    def render(self) -> list:
        if self.sequence is None:
            text = "Open a sequence to export its audio tracks." if self.loaded else "Reading the active sequence."
            return [self.text(text, small=True)]
        busy = self.job is not None
        chosen = self.chosen_tracks()
        tiles = []
        for track in self.tracks:
            clips = plan.plural(track.clip_count, "clip") if track.clip_count else "Empty"
            hint = clips + (" · muted in Premiere" if track.muted else "")
            label = track.code if plan.track_label(track) == track.code else f"{track.code} · {track.name}"
            tiles.append(self.switch(f"track.{track.index}", label, track.index in chosen, short=track.code,
                                     icon="audio-track", hint=hint, disabled=busy or not track.clip_count))
        nodes: list = []
        if tiles:
            nodes.append(self.group("Tracks", tiles, id="tracks", tiles=True, short="TRKS"))
        else:
            nodes.append(self.text("The active sequence has no audio tracks.", small=True))
        out = [self._menu_tile("mode", "Mode", "audio-mode", "MODE", plan.MODE_BADGES,
                               "Separate files: one file per chosen track. One file: a mix of the chosen tracks.",
                               busy),
               self._menu_tile("format", "Format", "audio-format", "FMT",
                               tuple((fmt.id, fmt.badge) for fmt in plan.FORMATS),
                               "The audio preset of Premiere the files are rendered with.", busy),
               self._menu_tile("range", "Range", "audio-range", "RNG", plan.RANGE_BADGES,
                               "In to Out uses the sequence In and Out marks when both are set, else the whole "
                               "sequence.", busy),
               self.button(EXPORT, LABEL, short="EXP", icon="audio-export", busy=busy,
                           hint=f"Renders the chosen tracks into {self._folder_text()} in the project folder."),
               self.button("open_folder", "Open audio folder", short="DIR", icon="open-folder",
                           hint="Opens the folder the audio files are written to.")]
        if busy:
            out.append(self.progress(self.progress_value, "Exporting audio", detail=self.progress_label or None,
                                     cancel=STOP))
        nodes.append(self.group("Export", out, id="output", tiles=True, short="OUT"))
        return nodes

    def _folder_text(self) -> str:
        """The output folder relative to the project root ("Exports\\Audio", or the layout's exports folder)."""
        project = self.ctx.project
        root = getattr(project, "root", None) if project is not None else None
        if root:
            try:
                return str(plan.audio_folder(root).relative_to(Path(root)))
            except ValueError:
                pass
        return f"{plan.DEFAULT_EXPORTS_DIR}\\{plan.AUDIO_DIR}"

    def _menu_tile(self, name: str, label: str, icon: str, short: str, badges: tuple, hint: str, busy: bool) -> dict:
        options, _ = CHOICES[name]
        current = self.choice(name)
        badge = dict(badges).get(current, short)
        menu = self.menu(f"{name}.choice", current, [{"value": value, "label": text} for value, text in options], badge)
        # The tile's code is the current choice ("WAV", "SEP", "IN/OUT"), so the lane reads as the export setup.
        return self.button(name, label, short=badge, icon=icon, hint=hint, menu=menu, disabled=busy)

    def section_status(self) -> Optional[dict]:
        if self.job is not None:
            return self.status(f"Running · {LABEL}", "accent")
        return None

    def render_host(self) -> Optional[dict]:
        return {
            "title": self.title,
            "blocks": [
                {"type": "text", "id": "about",
                 "text": "Choose tracks, mode, format and range on the Audio tab of the panel. Files are written to "
                         f"{plan.DEFAULT_EXPORTS_DIR}\\{plan.AUDIO_DIR} in the project folder (the layout's exports "
                         "folder when the project is set up), never over an existing file.", "small": True},
            ],
        }


def _remove_own(temp: Path) -> None:
    """Deletes the module's own temporary output of a failed pass (never a file of the user)."""
    try:
        if temp.name.startswith(plan.TEMP_PREFIX) and temp.is_file():
            temp.unlink()
    except OSError:
        pass
