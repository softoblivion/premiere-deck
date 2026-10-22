"""Image and text embeddings (docs/AI_SEARCH.md 1, 7).

`Embedder` is the interface the index and the query code use; `ClipOnnxEmbedder` implements it with the CLIP
ViT-B/32 ONNX exports of `models.json` on ONNX Runtime. The vision encoder runs on DirectML when available
(provider `auto`) or on the CPU; the text encoder always runs on the CPU (one short query, a few milliseconds,
and no GPU memory). Both return L2-normalised float32 vectors.
"""

from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import Optional, Protocol, Sequence

import numpy as np

from premiere_deck.modules.ai_search.models import Manifest, model_dir

log = logging.getLogger("module.search.embed")

IMAGE_SIZE = 224
BATCH = 32
MAX_TOKENS = 77
CLIP_MEAN = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
CLIP_STD = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)

PROVIDER_AUTO = "auto"
PROVIDER_DIRECTML = "directml"
PROVIDER_CPU = "cpu"
PROVIDERS = (PROVIDER_AUTO, PROVIDER_DIRECTML, PROVIDER_CPU)
_ORT_NAMES = {PROVIDER_DIRECTML: "DmlExecutionProvider", PROVIDER_CPU: "CPUExecutionProvider"}


class EmbedError(Exception):
    """The model cannot be loaded or run; the message is one sentence for the user."""


class Embedder(Protocol):
    model_id: str
    dim: int
    device: str  # "directml" | "cpu" once loaded

    def embed_images(self, frames: np.ndarray) -> np.ndarray:
        """uint8 RGB frames [N, H, W, 3] -> float32 [N, dim], L2-normalised."""
        ...

    def embed_text(self, text: str) -> np.ndarray:
        """float32 [dim], L2-normalised."""
        ...


def available_devices() -> list[str]:
    try:
        import onnxruntime as ort
    except ImportError:
        return []
    names = set(ort.get_available_providers())
    return [key for key, ort_name in _ORT_NAMES.items() if ort_name in names]


def resolve_device(provider: str) -> str:
    """`auto` and `directml` use DirectML when ONNX Runtime offers it; otherwise the CPU."""
    devices = available_devices()
    if provider in (PROVIDER_AUTO, PROVIDER_DIRECTML) and PROVIDER_DIRECTML in devices:
        return PROVIDER_DIRECTML
    return PROVIDER_CPU


_throttling_disabled = False


def disable_power_throttling() -> bool:
    """Opt the process out of Windows execution-speed throttling (EcoQoS).

    Windows 11 throttles a process that has no foreground window: CPU inference of the vision model drops from
    about 90 to about 16 frames/s after two seconds of load (measured on a desktop CPU). The opt-out is
    process-wide and only affects scheduling, so it is applied once, when the CPU device is chosen.
    """
    global _throttling_disabled
    if _throttling_disabled or os.name != "nt":
        return _throttling_disabled
    import ctypes

    class _PowerThrottling(ctypes.Structure):
        _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong), ("StateMask", ctypes.c_ulong)]

    process_power_throttling = 4  # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
    execution_speed = 0x1  # PROCESS_POWER_THROTTLING_EXECUTION_SPEED
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        kernel32.SetProcessInformation.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong]
        info = _PowerThrottling(1, execution_speed, 0)
        _throttling_disabled = bool(kernel32.SetProcessInformation(kernel32.GetCurrentProcess(), process_power_throttling,
                                                                   ctypes.byref(info), ctypes.sizeof(info)))
    except (OSError, AttributeError):
        _throttling_disabled = False
    if not _throttling_disabled:
        log.info("could not opt out of power throttling; CPU indexing may run slower in the background")
    return _throttling_disabled


def center_crop_resize(frame: np.ndarray, size: int = IMAGE_SIZE) -> np.ndarray:
    """Shorter side to `size` (bicubic), then the centre `size` x `size` square. uint8 RGB in and out."""
    h, w = frame.shape[:2]
    if (h, w) == (size, size):
        return frame
    from PIL import Image

    scale = size / min(h, w)
    nh, nw = max(size, round(h * scale)), max(size, round(w * scale))
    image = Image.fromarray(frame).resize((nw, nh), Image.BICUBIC)
    top, left = (nh - size) // 2, (nw - size) // 2
    return np.asarray(image.crop((left, top, left + size, top + size)))


def preprocess(frames: np.ndarray, dtype=np.float32) -> np.ndarray:
    """uint8 [N, H, W, 3] -> CLIP-normalised [N, 3, 224, 224] of `dtype`."""
    if frames.ndim != 4 or frames.shape[-1] != 3:
        raise ValueError("frames are uint8 [N, H, W, 3]")
    if frames.shape[1:3] != (IMAGE_SIZE, IMAGE_SIZE):
        frames = np.stack([center_crop_resize(f) for f in frames])
    x = frames.astype(np.float32) / 255.0
    x = (x - CLIP_MEAN) / CLIP_STD
    return np.ascontiguousarray(x.transpose(0, 3, 1, 2), dtype=dtype)


def normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    norm = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.maximum(norm, 1e-12)


def _ort_dtype(type_name: str):
    return np.float16 if "float16" in type_name else np.float32


class ClipOnnxEmbedder:
    """CLIP ViT-B/32 on ONNX Runtime. Sessions load lazily (vision on first `embed_images`, text on first
    `embed_text`) and are safe to share between the index job and the query code."""

    def __init__(self, manifest: Manifest, models_dir: Path, provider: str = PROVIDER_AUTO,
                 files: Optional[dict] = None) -> None:
        self.manifest = manifest
        self.model_id = manifest.id
        self.dim = manifest.dim
        self.provider = provider if provider in PROVIDERS else PROVIDER_AUTO
        folder = model_dir(models_dir, manifest)
        self._files = files or {role: folder / manifest.file(role).name for role in ("vision", "text", "tokenizer")}
        self.device = ""
        self._vision = None
        self._vision_input = ("pixel_values", np.float32)
        self._vision_output = "image_embeds"
        self._text = None
        self._text_inputs: list[str] = []
        self._text_output = "text_embeds"
        self._tokenizer = None
        self._lock = threading.Lock()

    # sessions
    def _session(self, path: Path, device: str):
        try:
            import onnxruntime as ort
        except ImportError as exc:
            raise EmbedError("ONNX Runtime is not installed") from exc
        options = ort.SessionOptions()
        options.log_severity_level = 3
        # The fp16 export fails the extended graph fusions of the CPU provider; basic optimisation runs it.
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_BASIC
        if device == PROVIDER_DIRECTML:
            options.enable_mem_pattern = False  # required by DirectML
            options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        providers = [_ORT_NAMES[device]] + ([_ORT_NAMES[PROVIDER_CPU]] if device != PROVIDER_CPU else [])
        try:
            return ort.InferenceSession(str(path), options, providers=providers)
        except Exception as exc:  # noqa: BLE001 - ORT raises its own types
            raise EmbedError(f"Cannot load {Path(path).name}: {str(exc).splitlines()[0][:160]}") from exc

    def _ensure_vision(self):
        with self._lock:
            if self._vision is not None:
                return self._vision
            device = resolve_device(self.provider)
            try:
                session = self._session(self._files["vision"], device)
            except EmbedError:
                if device == PROVIDER_CPU:
                    raise
                log.warning("DirectML session failed; falling back to the CPU", exc_info=True)
                device = PROVIDER_CPU
                session = self._session(self._files["vision"], device)
            first = session.get_inputs()[0]
            self._vision_input = (first.name, _ort_dtype(first.type))
            outputs = [o.name for o in session.get_outputs()]
            self._vision_output = "image_embeds" if "image_embeds" in outputs else outputs[0]
            self.device = device if _ORT_NAMES[device] in session.get_providers() else PROVIDER_CPU
            if self.device == PROVIDER_CPU:
                disable_power_throttling()
            self._vision = session
            return session

    def _ensure_text(self):
        with self._lock:
            if self._text is not None:
                return self._text
            try:
                from tokenizers import Tokenizer
            except ImportError as exc:
                raise EmbedError("The tokenizers library is not installed") from exc
            try:
                tokenizer = Tokenizer.from_file(str(self._files["tokenizer"]))
            except Exception as exc:  # noqa: BLE001
                raise EmbedError("Cannot load the tokenizer") from exc
            tokenizer.enable_truncation(MAX_TOKENS)
            session = self._session(self._files["text"], PROVIDER_CPU)
            self._text_inputs = [i.name for i in session.get_inputs()]
            outputs = [o.name for o in session.get_outputs()]
            self._text_output = "text_embeds" if "text_embeds" in outputs else outputs[0]
            self._tokenizer = tokenizer
            self._text = session
            return session

    def load(self) -> str:
        """Load the vision session now; returns the device it runs on."""
        self._ensure_vision()
        return self.device

    # Embedder
    def embed_images(self, frames: np.ndarray) -> np.ndarray:
        session = self._ensure_vision()
        name, dtype = self._vision_input
        out = []
        for start in range(0, len(frames), BATCH):
            x = preprocess(frames[start:start + BATCH], dtype)
            out.append(np.asarray(session.run([self._vision_output], {name: x})[0], dtype=np.float32))
        if not out:
            return np.zeros((0, self.dim), dtype=np.float32)
        return normalize(np.concatenate(out))

    def embed_text(self, text: str) -> np.ndarray:
        session = self._ensure_text()
        ids = self._tokenizer.encode(text).ids[:MAX_TOKENS]
        feeds = {}
        for name in self._text_inputs:
            if name == "attention_mask":
                feeds[name] = np.ones((1, len(ids)), dtype=np.int64)
            else:
                feeds[name] = np.asarray([ids], dtype=np.int64)
        return normalize(np.asarray(session.run([self._text_output], feeds)[0], dtype=np.float32))[0]

    def close(self) -> None:
        with self._lock:
            self._vision = None
            self._text = None


def device_label(device: str) -> str:
    return {PROVIDER_DIRECTML: "GPU (DirectML)", PROVIDER_CPU: "CPU · slow"}.get(device, "not loaded")


def stack_frames(frames: Sequence[np.ndarray]) -> np.ndarray:
    return np.stack(frames) if frames else np.zeros((0, IMAGE_SIZE, IMAGE_SIZE, 3), dtype=np.uint8)
