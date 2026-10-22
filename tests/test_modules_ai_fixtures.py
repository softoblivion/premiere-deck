"""Shared fixtures of the AI search tests: tiny random-weight ONNX models with the input and output names of the
real CLIP exports, a tiny tokenizer, a test manifest, ffmpeg-generated clips. No test case lives here.

The `onnx` package is not a dependency, so the models are written as ONNX protobuf by hand (wire format of
onnx.proto: ModelProto, GraphProto, NodeProto, TensorProto, ValueInfoProto). ONNX Runtime parses them.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
import subprocess
from pathlib import Path

import numpy as np

DIM = 512
FLOAT, INT64, FLOAT16 = 1, 7, 10


# --- protobuf wire format ----------------------------------------------------------------------------------------


def _varint(value: int) -> bytes:
    value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _key(field: int, wire: int) -> bytes:
    return _varint((field << 3) | wire)


def _int(field: int, value: int) -> bytes:
    return _key(field, 0) + _varint(value)


def _bytes(field: int, data: bytes) -> bytes:
    return _key(field, 2) + _varint(len(data)) + data


def _str(field: int, text: str) -> bytes:
    return _bytes(field, text.encode("utf-8"))


def _float(field: int, value: float) -> bytes:
    return _key(field, 5) + struct.pack("<f", value)


def tensor(name: str, array: np.ndarray) -> bytes:
    dtype = {np.dtype(np.float32): FLOAT, np.dtype(np.int64): INT64, np.dtype(np.float16): FLOAT16}[array.dtype]
    body = b"".join(_int(1, d) for d in array.shape) + _int(2, dtype) + _str(8, name)
    return body + _bytes(9, np.ascontiguousarray(array).tobytes())


def value_info(name: str, elem_type: int, dims: list) -> bytes:
    shape = b"".join(_bytes(1, _int(1, d) if isinstance(d, int) else _str(2, d)) for d in dims)
    tensor_type = _int(1, elem_type) + _bytes(2, shape)
    return _str(1, name) + _bytes(2, _bytes(1, tensor_type))


def attr_int(name: str, value: int) -> bytes:
    return _str(1, name) + _int(3, value) + _int(20, 2)


def attr_ints(name: str, values: list) -> bytes:
    return _str(1, name) + b"".join(_int(8, v) for v in values) + _int(20, 7)


def node(op: str, inputs: list, outputs: list, attrs: list = ()) -> bytes:
    body = b"".join(_str(1, i) for i in inputs) + b"".join(_str(2, o) for o in outputs) + _str(4, op)
    return body + b"".join(_bytes(5, a) for a in attrs)


def model(nodes: list, initializers: list, inputs: list, outputs: list, name: str = "tiny") -> bytes:
    graph = (b"".join(_bytes(1, n) for n in nodes) + _str(2, name) + b"".join(_bytes(5, t) for t in initializers)
             + b"".join(_bytes(11, i) for i in inputs) + b"".join(_bytes(12, o) for o in outputs))
    opset = _str(1, "") + _int(2, 13)
    return _int(1, 8) + _str(2, "premiere-deck-tests") + _bytes(7, graph) + _bytes(8, opset)


# --- tiny CLIP stand-ins -----------------------------------------------------------------------------------------


def tiny_vision(fp16_weights: bool = False, seed: int = 1) -> bytes:
    """pixel_values float32 [N, 3, H, W] -> image_embeds float32 [N, 512]: channel means through a random
    projection, so frames of different colours get different embeddings and equal colours equal ones."""
    rng = np.random.default_rng(seed)
    weight = rng.standard_normal((3, DIM)).astype(np.float32)
    nodes = [node("GlobalAveragePool", ["pixel_values"], ["pooled"]),
             node("Flatten", ["pooled"], ["flat"], [attr_int("axis", 1)])]
    if fp16_weights:
        inits = [tensor("w", weight.astype(np.float16))]
        nodes += [node("Cast", ["flat"], ["flat16"], [attr_int("to", FLOAT16)]),
                  node("MatMul", ["flat16", "w"], ["proj16"]),
                  node("Cast", ["proj16"], ["image_embeds"], [attr_int("to", FLOAT)])]
    else:
        inits = [tensor("w", weight)]
        nodes += [node("MatMul", ["flat", "w"], ["image_embeds"])]
    return model(nodes, inits, [value_info("pixel_values", FLOAT, ["batch_size", "num_channels", "height", "width"])],
                 [value_info("image_embeds", FLOAT, ["batch_size", DIM])], "tiny_vision")


def tiny_text(seed: int = 2) -> bytes:
    """input_ids int64 [N, L] -> text_embeds float32 [N, 512]: mean token id through a random projection."""
    rng = np.random.default_rng(seed)
    weight = rng.standard_normal((1, DIM)).astype(np.float32)
    bias = rng.standard_normal((DIM,)).astype(np.float32)
    nodes = [node("Cast", ["input_ids"], ["ids_f"], [attr_int("to", FLOAT)]),
             node("ReduceMean", ["ids_f"], ["mean"], [attr_ints("axes", [1]), attr_int("keepdims", 1)]),
             node("MatMul", ["mean", "w"], ["proj"]),
             node("Add", ["proj", "b"], ["text_embeds"])]
    return model(nodes, [tensor("w", weight), tensor("b", bias)],
                 [value_info("input_ids", INT64, ["batch_size", "sequence_length"])],
                 [value_info("text_embeds", FLOAT, ["batch_size", DIM])], "tiny_text")


def tiny_tokenizer_json() -> str:
    from tokenizers import Tokenizer, models, normalizers, pre_tokenizers, processors

    words = ["<|startoftext|>", "<|endoftext|>", "a", "photo", "of", "red", "blue", "green", "car", "frame", "sky"]
    tok = Tokenizer(models.WordLevel({w: i for i, w in enumerate(words)}, unk_token="<|endoftext|>"))
    tok.normalizer = normalizers.Lowercase()
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    tok.post_processor = processors.TemplateProcessing(
        single="<|startoftext|> $A <|endoftext|>", special_tokens=[("<|startoftext|>", 0), ("<|endoftext|>", 1)])
    return tok.to_str()


def write_tiny_model(models_dir: Path, *, fp16: bool = True) -> dict:
    """Write the tiny model files into `<models_dir>/clip-vit-b32-tiny/` and return a manifest dict that pins them
    (sizes and SHA-256) under the same roles as the real one."""
    files = {"vision": ("vision_model_fp16.onnx", tiny_vision(fp16_weights=fp16)),
             "text": ("text_model.onnx", tiny_text()),
             "tokenizer": ("tokenizer.json", tiny_tokenizer_json().encode("utf-8"))}
    source = Path(models_dir).parent / "tiny-source"
    source.mkdir(parents=True, exist_ok=True)
    entries = []
    for role, (name, data) in files.items():
        (source / name).write_bytes(data)
        entries.append({"name": name, "source": name, "role": role, "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
    return {"id": "clip-vit-b32-tiny", "title": "Tiny test model", "repo": "tests/tiny", "revision": "0" * 40,
            "url": "https://huggingface.co/{repo}/resolve/{revision}/{source}", "dim": DIM, "files": entries,
            "_source": str(source)}


def install_tiny_model(models_dir: Path, manifest_dict: dict) -> None:
    """Put the tiny files in place with a verification record, as a finished download would."""
    from premiere_deck.modules.ai_search import models as md

    manifest = manifest_from(manifest_dict)
    folder = md.model_dir(models_dir, manifest)
    folder.mkdir(parents=True, exist_ok=True)
    for f in manifest.files:
        shutil.copyfile(Path(manifest_dict["_source"]) / f.source, folder / f.name)
        md._write_record(models_dir, manifest, f, "file://test")


def manifest_from(data: dict):
    from premiere_deck.modules.ai_search import models as md

    path = Path(data["_source"]) / "manifest.json"
    path.write_text(json.dumps({k: v for k, v in data.items() if not k.startswith("_")}), encoding="utf-8")
    return md.load_manifest(path)


# --- media -------------------------------------------------------------------------------------------------------


def ffmpeg_clip(path: Path, colors: list, seconds_each: float = 1.5, size: str = "320x180") -> Path:
    """Concatenated solid-colour scenes (hard cuts between them), H.264."""
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for color in colors:
        args += ["-f", "lavfi", "-i", f"color={color}:s={size}:d={seconds_each}:r=25"]
    graph = "".join(f"[{i}:v]" for i in range(len(colors))) + f"concat=n={len(colors)}:v=1[v]"
    args += ["-filter_complex", graph, "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(args, check=True, stdin=subprocess.DEVNULL, capture_output=True,
                   creationflags=0x08000000)
    return path
