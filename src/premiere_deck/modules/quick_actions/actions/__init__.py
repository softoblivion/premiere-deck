"""The quick actions, in the order of the table in docs/QUICK_ACTIONS.md 1 (the panel renders them in this order)."""

from premiere_deck.modules.quick_actions.actions.add_audio_track import AddAudioTrack
from premiere_deck.modules.quick_actions.actions.add_video_track import AddVideoTrack
from premiere_deck.modules.quick_actions.actions.adjustment_layer import AdjustmentLayer
from premiere_deck.modules.quick_actions.actions.colour import Colour
from premiere_deck.modules.quick_actions.actions.consolidate_duplicates import ConsolidateDuplicates
from premiere_deck.modules.quick_actions.actions.create_16_9 import Create169
from premiere_deck.modules.quick_actions.actions.create_9_16 import Create916
from premiere_deck.modules.quick_actions.actions.create_custom import CreateCustom
from premiere_deck.modules.quick_actions.actions.delete_empty_tracks import DeleteEmptyTracks
from premiere_deck.modules.quick_actions.actions.duplicate_sequence import DuplicateSequence
from premiere_deck.modules.quick_actions.actions.ease_fast import EaseFast
from premiere_deck.modules.quick_actions.actions.ease_linear import EaseLinear
from premiere_deck.modules.quick_actions.actions.ease_med import EaseMed
from premiere_deck.modules.quick_actions.actions.ease_slow import EaseSlow
from premiere_deck.modules.quick_actions.actions.export_frame import ExportFrame
from premiere_deck.modules.quick_actions.actions.export_open import ExportOpen
from premiere_deck.modules.quick_actions.actions.label_by_bin import LabelByBin
from premiere_deck.modules.quick_actions.actions.lock_others import LockOthers
from premiere_deck.modules.quick_actions.actions.open_project_folder import OpenProjectFolder
from premiere_deck.modules.quick_actions.actions.razor_all import RazorAll
from premiere_deck.modules.quick_actions.actions.remove_audio_track import RemoveAudioTrack
from premiere_deck.modules.quick_actions.actions.remove_video_track import RemoveVideoTrack
from premiere_deck.modules.quick_actions.actions.reveal_clip import RevealClip
from premiere_deck.modules.quick_actions.actions.save_version import SaveVersion
from premiere_deck.modules.quick_actions.actions.scale_to_frame import ScaleToFrame
from premiere_deck.modules.quick_actions.actions.select_offline import SelectOffline
from premiere_deck.modules.quick_actions.actions.select_same_source import SelectSameSource
from premiere_deck.modules.quick_actions.actions.solo_track import SoloTrack

ACTION_CLASSES = (
    AdjustmentLayer, RazorAll, DeleteEmptyTracks,
    AddVideoTrack, AddAudioTrack, RemoveVideoTrack, RemoveAudioTrack, SoloTrack, LockOthers,
    SelectSameSource, SelectOffline,
    ScaleToFrame, Colour, LabelByBin,
    SaveVersion, ConsolidateDuplicates, OpenProjectFolder, RevealClip,
    DuplicateSequence, Create169, Create916, CreateCustom,
    ExportOpen, ExportFrame,
    EaseSlow, EaseMed, EaseFast, EaseLinear,
)

__all__ = ["ACTION_CLASSES"] + [cls.__name__ for cls in ACTION_CLASSES]
