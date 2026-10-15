"""Convert the published Community Forensics detector into the ONNX file shipped in
forensic/models/weights/, and print its SHA-256 for the model manifest.

    pip install onnx
    python tools/convert_commfor.py SRC_DIR [--out forensic/models/weights]

SRC_DIR holds model.safetensors exactly as published by the authors on Hugging Face
(OwensLab/commfor-model-384, MIT licence; its SHA-256 is pinned below). The model is a
ViT-Small/16 at 384x384 (timm vit_small_patch16_384) whose classification head was replaced
by one logit: J. Park and A. Owens, 'Community Forensics: Using Thousands of Generators to
Train Fake Image Detectors', CVPR 2025 (https://github.com/JeongsooP/Community-Forensics).

PyTorch is not needed: safetensors is a JSON header followed by raw little-endian tensors, so
the weights are read with NumPy, copied unchanged as float32, and the transformer is rebuilt
operation for operation (patch convolution, class token, position embedding, 12 pre-norm
blocks of 6-head self-attention and a GELU MLP, final layer norm, linear head) as an ONNX
graph with a dynamic batch size. The output is the raw logit; sigmoid > 0.5 means generated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
from pathlib import Path

import numpy as np

SOURCE = ("model.safetensors", "b89f36275f3bf5e2b040eee36597a8f19db051bff9a473a9cf7b2466284fb387")
OUTPUT = "commfor_vit_s16_384.onnx"
DIM, HEADS, DEPTH, PATCH, SIZE, EPS = 384, 6, 12, 16, 384, 1e-6


def load_safetensors(path: Path) -> dict[str, np.ndarray]:
    data = path.read_bytes()
    (n,) = struct.unpack("<Q", data[:8])
    header = json.loads(data[8 : 8 + n])
    body = memoryview(data)[8 + n :]
    out = {}
    for k, v in header.items():
        if k == "__metadata__":
            continue
        if v["dtype"] != "F32":
            raise ValueError(f"{k}: unexpected dtype {v['dtype']}")
        a, b = v["data_offsets"]
        out[k] = np.frombuffer(body[a:b], dtype="<f4").reshape(v["shape"]).astype(np.float32)
    return out


def build(w: dict[str, np.ndarray]):
    from onnx import TensorProto, helper, numpy_helper

    nodes, inits = [], []
    count = [0]

    def name(p):
        count[0] += 1
        return f"{p}_{count[0]}"

    def const(arr, p="c"):
        n = name(p)
        inits.append(numpy_helper.from_array(np.asarray(arr), n))
        return n

    def op(t, ins, p=None, **attrs):
        out = name(p or t.lower())
        nodes.append(helper.make_node(t, ins, [out], **attrs))
        return out

    def weight(k):
        n = k.replace(".", "_")
        inits.append(numpy_helper.from_array(w[k], n))
        return n

    def linear(x, pre):
        return op("Add", [op("MatMul", [x, const(np.ascontiguousarray(w[pre + ".weight"].T), pre + "_wT")]), weight(pre + ".bias")])

    def layernorm(x, pre):
        return op("LayerNormalization", [x, weight(pre + ".weight"), weight(pre + ".bias")], axis=-1, epsilon=EPS)

    x = "image"
    t = op("Conv", [x, weight("vit.patch_embed.proj.weight"), weight("vit.patch_embed.proj.bias")],
           kernel_shape=[PATCH, PATCH], strides=[PATCH, PATCH])
    t = op("Transpose", [op("Reshape", [t, const(np.array([0, DIM, -1], np.int64))])], perm=[0, 2, 1])
    batch = op("Slice", [op("Shape", [x]), const(np.array([0], np.int64)), const(np.array([1], np.int64))])
    cls_shape = op("Concat", [batch, const(np.array([1, DIM], np.int64))], axis=0)
    cls = op("Expand", [weight("vit.cls_token"), cls_shape])
    t = op("Add", [op("Concat", [cls, t], axis=1), weight("vit.pos_embed")])
    scale = const(np.array(1.0 / np.sqrt(DIM // HEADS), np.float32))
    half, one, rsqrt2 = const(np.array(0.5, np.float32)), const(np.array(1.0, np.float32)), const(np.array(1 / np.sqrt(2), np.float32))
    for i in range(DEPTH):
        b = f"vit.blocks.{i}"
        h = layernorm(t, b + ".norm1")
        qkv = linear(h, b + ".attn.qkv")
        qkv = op("Transpose", [op("Reshape", [qkv, const(np.array([0, 0, 3, HEADS, DIM // HEADS], np.int64))])], perm=[2, 0, 3, 1, 4])
        q, k, v = (op("Gather", [qkv, const(np.array(j, np.int64))], axis=0) for j in range(3))
        att = op("Softmax", [op("MatMul", [op("Mul", [q, scale]), op("Transpose", [k], perm=[0, 1, 3, 2])])], axis=-1)
        a = op("Transpose", [op("MatMul", [att, v])], perm=[0, 2, 1, 3])
        a = linear(op("Reshape", [a, const(np.array([0, 0, DIM], np.int64))]), b + ".attn.proj")
        t = op("Add", [t, a])
        h = linear(layernorm(t, b + ".norm2"), b + ".mlp.fc1")
        h = op("Mul", [op("Mul", [h, half]), op("Add", [one, op("Erf", [op("Mul", [h, rsqrt2])])])])  # exact GELU
        t = op("Add", [t, linear(h, b + ".mlp.fc2")])
    t = layernorm(t, "vit.norm")
    tok = op("Gather", [t, const(np.array(0, np.int64))], axis=1)
    logit = linear(tok, "vit.head")
    nodes.append(helper.make_node("Identity", [logit], ["logit"]))
    graph = helper.make_graph(
        nodes, "commfor_vit_s16_384",
        [helper.make_tensor_value_info("image", TensorProto.FLOAT, ["batch", 3, SIZE, SIZE])],
        [helper.make_tensor_value_info("logit", TensorProto.FLOAT, ["batch", 1])],
        inits,
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)], producer_name="forensic.tools.convert_commfor")
    model.ir_version = 8
    return model


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "forensic" / "models" / "weights")
    a = ap.parse_args()
    import onnx

    src = a.src / SOURCE[0]
    got = hashlib.sha256(src.read_bytes()).hexdigest()
    if got != SOURCE[1]:
        raise SystemExit(f"{src}: SHA-256 {got} is not the published file ({SOURCE[1]})")
    model = build(load_safetensors(src))
    onnx.checker.check_model(model)
    a.out.mkdir(parents=True, exist_ok=True)
    out = a.out / OUTPUT
    onnx.save(model, out)
    print(OUTPUT, hashlib.sha256(out.read_bytes()).hexdigest())


if __name__ == "__main__":
    main()
