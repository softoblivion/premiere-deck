"""The quick actions, in the order of the table in docs/QUICK_ACTIONS.md 1 (the panel renders them in this order)."""

from premiere_deck.modules.quick_actions.actions.adjustment_layer import AdjustmentLayer
from premiere_deck.modules.quick_actions.actions.consolidate_duplicates import ConsolidateDuplicates
from premiere_deck.modules.quick_actions.actions.delete_empty_tracks import DeleteEmptyTracks
from premiere_deck.modules.quick_actions.actions.duplicate_sequence import DuplicateSequence
from premiere_deck.modules.quick_actions.actions.export_frame import ExportFrame
from premiere_deck.modules.quick_actions.actions.export_sequence import ExportSequence
from premiere_deck.modules.quick_actions.actions.label_by_bin import LabelByBin
from premiere_deck.modules.quick_actions.actions.lock_others import LockOthers
from premiere_deck.modules.quick_actions.actions.open_project_folder import OpenProjectFolder
from premiere_deck.modules.quick_actions.actions.razor_all import RazorAll
from premiere_deck.modules.quick_actions.actions.reveal_clip import RevealClip
from premiere_deck.modules.quick_actions.actions.save_version import SaveVersion
from premiere_deck.modules.quick_actions.actions.scale_to_frame import ScaleToFrame
from premiere_deck.modules.quick_actions.actions.select_offline import SelectOffline
from premiere_deck.modules.quick_actions.actions.select_same_source import SelectSameSource
from premiere_deck.modules.quick_actions.actions.social_copy import SocialCopy
from premiere_deck.modules.quick_actions.actions.solo_track import SoloTrack

ACTION_CLASSES = (
    AdjustmentLayer, RazorAll, DeleteEmptyTracks,
    SelectSameSource, SelectOffline,
    ScaleToFrame, LabelByBin,
    SoloTrack, LockOthers,
    SaveVersion, ConsolidateDuplicates, OpenProjectFolder, RevealClip,
    DuplicateSequence, SocialCopy,
    ExportFrame, ExportSequence,
)

__all__ = ["ACTION_CLASSES"] + [cls.__name__ for cls in ACTION_CLASSES]
