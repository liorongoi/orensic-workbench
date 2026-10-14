"""Convert the published FSRCNN and ESPCN TensorFlow graphs into the ONNX files shipped in
forensic/models/weights/, and print their SHA-256 for the model manifest.

    pip install onnx
    python tools/convert_sr_models.py SRC_DIR [--out forensic/models/weights]

SRC_DIR holds the .pb files exactly as published (their SHA-256 values are pinned below):
  FSRCNN_x{2,3,4}.pb  https://github.com/Saafke/FSRCNN_Tensorflow (models/), Apache-2.0
  ESPCN_x{2,3,4}.pb   https://github.com/fannymonori/TF-ESPCN (export/), Apache-2.0

The conversion copies the weights unchanged (float32) and rebuilds the same operations
(convolution with SAME padding, PReLU or ReLU, depth-to-space, bias or tanh) as an ONNX
graph, so the model can run under ONNX Runtime without TensorFlow. It reads the
TensorFlow GraphDef with a minimal protocol-buffer reader rather than TensorFlow itself.
"""

from __future__ import annotations

import argparse
import hashlib
import struct
from pathlib import Path

import numpy as np

SOURCES = {
    "FSRCNN_x2.pb": "366b33f0084c7b3f2bf6724f0a2c77bca94fcec9d7b6d72389d330073b380d5c",
    "FSRCNN_x3.pb": "efd38655a815908c6c8954db6052f128e76a735f1de657894c477d0dc0b64481",
    "FSRCNN_x4.pb": "5c68d18db561aed8ead4ffedf1b897ea615baaf60ebf6c35f8e641f8fa4a21bf",
    "ESPCN_x2.pb": "59f77351e1d7c0057bf6fe088b4a8a07e42c468c8c8aebb674a6b4ea1823221d",
    "ESPCN_x3.pb": "0e667db7a431d14c32568ed79bf21701a64df8b1d162e48ca1f8800affe2c2b3",
    "ESPCN_x4.pb": "e403f06309229cf36009cd8fb0da032ba7643fae9f15cf94fe562e8edf8fef47",
}


# Minimal protocol-buffer reading ------------------------------------------------------------


def _varint(b: bytes, i: int) -> tuple[int, int]:
    r = s = 0
    while True:
        c = b[i]
        i += 1
        r |= (c & 0x7F) << s
        s += 7
        if c < 0x80:
            return r, i


def _fields(b: bytes):
    i = 0
    while i < len(b):
        key, i = _varint(b, i)
        f, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _varint(b, i)
        elif wire == 1:
            v, i = b[i : i + 8], i + 8
        elif wire == 2:
            n, i = _varint(b, i)
            v, i = b[i : i + n], i + n
        elif wire == 5:
            v, i = b[i : i + 4], i + 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        yield f, wire, v


def _tensor(b: bytes):
    dtype, shape, content, floats = None, [], None, []
    for f, wire, v in _fields(b):
        if f == 1:
            dtype = v
        elif f == 2:
            for f2, _, v2 in _fields(v):
                if f2 == 2:
                    shape.append(dict((a, c) for a, _, c in _fields(v2)).get(1, 0))
        elif f == 4:
            content = v
        elif f == 5:
            floats += list(struct.unpack(f"<{len(v) // 4}f", v)) if wire == 2 else [struct.unpack("<f", v)[0]]
    if dtype != 1:  # DT_FLOAT
        return None
    a = np.frombuffer(content, "<f4").copy() if content else np.array(floats, np.float32)
    if shape and a.size == 1 and int(np.prod(shape)) > 1:
        a = np.full(shape, a[0], np.float32)
    return a.reshape(shape) if shape else a


def _attr(b: bytes):
    for f, _, v in _fields(b):
        if f == 3:
            return v  # int
        if f == 8:
            return _tensor(v)
    return None


def read_graph(path: Path) -> list[dict]:
    nodes = []
    for f, _, v in _fields(path.read_bytes()):
        if f != 1:
            continue
        n: dict = {"input": [], "attr": {}}
        for f2, _, v2 in _fields(v):
            if f2 == 1:
                n["name"] = v2.decode()
            elif f2 == 2:
                n["op"] = v2.decode()
            elif f2 == 3:
                n["input"].append(v2.decode())
            elif f2 == 5:
                kv = dict((a, c) for a, _, c in _fields(v2))
                n["attr"][kv[1].decode()] = _attr(kv.get(2, b""))
        nodes.append(n)
    return nodes


# Conversion --------------------------------------------------------------------------------------


def convert(pb: Path, out: Path) -> dict:
    import onnx
    from onnx import TensorProto, helper, numpy_helper

    nodes = read_graph(pb)
    consts = {n["name"]: n["attr"]["value"] for n in nodes if n["op"] == "Const"}
    convs = [n for n in nodes if n["op"] == "Conv2D"]
    d2s = next(n for n in nodes if n["op"] == "DepthToSpace")
    arch = "espcn" if any(n["op"] == "Tanh" for n in nodes) else "fsrcnn"
    onodes, inits, cur = [], [], "input"
    for i, c in enumerate(convs, 1):
        w = consts[c["input"][1]]  # HWIO
        last = i == len(convs)
        k = w.shape[0]
        inits.append(numpy_helper.from_array(np.ascontiguousarray(w.transpose(3, 2, 0, 1)).astype(np.float32), f"W{i}"))
        ins = [cur, f"W{i}"]
        if not (arch == "fsrcnn" and last):  # FSRCNN adds its last bias after depth-to-space
            inits.append(numpy_helper.from_array(consts[f"b{i}"].astype(np.float32), f"B{i}"))
            ins.append(f"B{i}")
        onodes.append(helper.make_node("Conv", ins, [f"c{i}"], pads=[k // 2] * 4, kernel_shape=[k, k]))
        cur = f"c{i}"
        if last:
            break
        if arch == "fsrcnn":  # relu(x) + alpha * (x - |x|) * 0.5 is PReLU
            inits.append(numpy_helper.from_array(consts[f"alpha{i}"].astype(np.float32).reshape(-1, 1, 1), f"A{i}"))
            onodes.append(helper.make_node("PRelu", [cur, f"A{i}"], [f"a{i}"]))
        else:
            onodes.append(helper.make_node("Relu", [cur], [f"a{i}"]))
        cur = f"a{i}"
    onodes.append(helper.make_node("DepthToSpace", [cur], ["d2s"], blocksize=int(d2s["attr"]["block_size"]), mode="DCR"))
    if arch == "fsrcnn":
        inits.append(numpy_helper.from_array(consts[f"b{len(convs)}"].astype(np.float32).reshape(1, 1, 1, 1), "Bout"))
        onodes.append(helper.make_node("Add", ["d2s", "Bout"], ["output"]))
    else:
        onodes.append(helper.make_node("Tanh", ["d2s"], ["output"]))
    g = helper.make_graph(
        onodes, pb.stem,
        [helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, 1, None, None])],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 1, None, None])],
        inits,
    )
    m = helper.make_model(g, opset_imports=[helper.make_opsetid("", 13)], producer_name="forensic-workbench tools/convert_sr_models.py")
    m.ir_version = 8
    onnx.checker.check_model(m)
    out.write_bytes(m.SerializeToString())
    return {"architecture": arch, "scale": int(d2s["attr"]["block_size"]), "sha256": hashlib.sha256(out.read_bytes()).hexdigest()}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "forensic" / "models" / "weights")
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    for name, expected in SOURCES.items():
        pb = a.src / name
        got = hashlib.sha256(pb.read_bytes()).hexdigest()
        if got != expected:
            raise SystemExit(f"{name}: SHA-256 {got} does not match the published file {expected}")
        info = convert(pb, a.out / (pb.stem.lower() + ".onnx"))
        print(f"{pb.stem.lower()}.onnx  {info['sha256']}  ({info['architecture']} x{info['scale']}, from {name} {expected})")


if __name__ == "__main__":
    main()
