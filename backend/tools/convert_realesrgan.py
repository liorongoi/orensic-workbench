"""Convert the published Real-ESRGAN PyTorch weights into the ONNX files shipped in
forensic/models/weights/, and print their SHA-256 for the model manifest.

    pip install onnx
    python tools/convert_realesrgan.py SRC_DIR [--out forensic/models/weights]

SRC_DIR holds the .pth files exactly as published (their SHA-256 values are pinned below), from
https://github.com/xinntao/Real-ESRGAN/releases (BSD-3-Clause):
  RealESRGAN_x4plus.pth       release v0.1.0   (RRDBNet, 23 blocks: the full model)
  realesr-general-x4v3.pth    release v0.2.5.0 (SRVGGNetCompact: the small, fast model)

PyTorch is not needed: the checkpoint's pickle is read with an unpickler that only accepts
tensor records (anything else is refused, so a tampered file cannot run code), the weights
are copied unchanged as float32, and the network is rebuilt operation for operation
(convolutions, PReLU or leaky ReLU, nearest-neighbour up-sampling, depth-to-space) as an
ONNX graph with dynamic height and width.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import pickle
import zipfile
from pathlib import Path

import numpy as np

SOURCES = {
    "RealESRGAN_x4plus.pth": "4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1",
    "realesr-general-x4v3.pth": "8dc7edb9ac80ccdc30c3a5dca6616509367f05fbc184ad95b731f05bece96292",
}
OUTPUTS = {"RealESRGAN_x4plus.pth": "realesrgan_x4plus.onnx", "realesr-general-x4v3.pth": "realesrgan_compact_x4.onnx"}


# Reading a PyTorch checkpoint without PyTorch ---------------------------------------------


class _Tensor:
    def __init__(self, storage, offset, size, stride):
        self.storage, self.offset, self.size, self.stride = storage, offset, tuple(size), tuple(stride)


class _Unpickler(pickle.Unpickler):
    def find_class(self, module, name):
        if (module, name) == ("torch._utils", "_rebuild_tensor_v2"):
            return lambda storage, offset, size, stride, *_: _Tensor(storage, offset, size, stride)
        if (module, name) == ("collections", "OrderedDict"):
            return collections.OrderedDict
        if module == "torch" and name == "FloatStorage":
            return name
        raise pickle.UnpicklingError(f"refusing {module}.{name} in a weights file")

    def persistent_load(self, pid):
        # ('storage', storage_type, key, location, numel)
        return pid


def load_state_dict(path: Path) -> dict[str, np.ndarray]:
    z = zipfile.ZipFile(path)
    pkl = next(n for n in z.namelist() if n.endswith("data.pkl"))
    root = pkl[: -len("data.pkl")]
    obj = _Unpickler(z.open(pkl)).load()
    sd = obj.get("params_ema") or obj.get("params") or obj
    out = {}
    for k, t in sd.items():
        _, kind, key, _, numel = t.storage
        if kind != "FloatStorage":
            raise ValueError(f"{k}: unexpected storage {kind}")
        flat = np.frombuffer(z.read(f"{root}data/{key}"), dtype="<f4", count=numel)
        n = int(np.prod(t.size)) if t.size else 1
        contiguous = tuple(int(np.prod(t.size[i + 1:])) for i in range(len(t.size)))
        if t.stride != contiguous:
            raise ValueError(f"{k}: non-contiguous tensor")
        out[k] = flat[t.offset : t.offset + n].reshape(t.size).astype(np.float32)
    return out


# Building the ONNX graphs -------------------------------------------------------------------


class _Graph:
    def __init__(self, sd):
        from onnx import helper, numpy_helper

        self.h, self.nh, self.sd = helper, numpy_helper, sd
        self.nodes, self.inits, self.n = [], [], 0

    def name(self, p="t"):
        self.n += 1
        return f"{p}{self.n}"

    def const(self, arr, p="c"):
        nm = self.name(p)
        self.inits.append(self.nh.from_array(np.asarray(arr), nm))
        return nm

    def op(self, kind, inputs, **attrs):
        out = self.name(kind.lower())
        self.nodes.append(self.h.make_node(kind, inputs, [out], **attrs))
        return out

    def conv(self, x, key):
        w, b = self.sd[f"{key}.weight"], self.sd[f"{key}.bias"]
        return self.op("Conv", [x, self.const(w, key + ".w"), self.const(b, key + ".b")], kernel_shape=list(w.shape[2:]),
                       pads=[w.shape[2] // 2] * 2 + [w.shape[3] // 2] * 2)

    def lrelu(self, x):
        return self.op("LeakyRelu", [x], alpha=0.2)

    def up2(self, x):
        return self.op("Resize", [x, "", self.const(np.array([1, 1, 2, 2], np.float32))], mode="nearest",
                       coordinate_transformation_mode="asymmetric", nearest_mode="floor")

    def scale(self, x, s):
        return self.op("Mul", [x, self.const(np.array(s, np.float32))])

    def model(self, x_name, y, doc):
        from onnx import TensorProto

        inp = self.h.make_tensor_value_info(x_name, TensorProto.FLOAT, [1, 3, "height", "width"])
        out = self.h.make_tensor_value_info(y, TensorProto.FLOAT, [1, 3, "height4", "width4"])
        g = self.h.make_graph(self.nodes, "realesrgan", [inp], [out], self.inits)
        m = self.h.make_model(g, opset_imports=[self.h.make_opsetid("", 17)], producer_name="forensic-workbench")
        m.doc_string = doc
        m.ir_version = 8
        return m


def build_rrdb(sd):
    """RRDBNet (ESRGAN generator) as used by RealESRGAN_x4plus: 23 residual-in-residual dense blocks."""
    g = _Graph(sd)
    feat = g.conv("input", "conv_first")
    x = feat
    nblocks = 1 + max(int(k.split(".")[1]) for k in sd if k.startswith("body."))
    for b in range(nblocks):
        block_in = x
        for r in (1, 2, 3):
            pre = f"body.{b}.rdb{r}"
            rdb_in, feats = x, [x]
            for c in range(1, 5):
                cat = feats[0] if len(feats) == 1 else g.op("Concat", feats, axis=1)
                feats.append(g.lrelu(g.conv(cat, f"{pre}.conv{c}")))
            x5 = g.conv(g.op("Concat", feats, axis=1), f"{pre}.conv5")
            x = g.op("Add", [g.scale(x5, 0.2), rdb_in])
        x = g.op("Add", [g.scale(x, 0.2), block_in])
    feat = g.op("Add", [feat, g.conv(x, "conv_body")])
    feat = g.lrelu(g.conv(g.up2(feat), "conv_up1"))
    feat = g.lrelu(g.conv(g.up2(feat), "conv_up2"))
    out = g.conv(g.lrelu(g.conv(feat, "conv_hr")), "conv_last")
    return g.model("input", out, "Real-ESRGAN x4plus (RRDBNet), converted from RealESRGAN_x4plus.pth, weights unchanged")


def build_compact(sd):
    """SRVGGNetCompact as used by realesr-general-x4v3: plain 3x3 convolutions with PReLU,
    depth-to-space x4, plus a nearest-neighbour up-sampled copy of the input."""
    g = _Graph(sd)
    idx = sorted({int(k.split(".")[1]) for k in sd})
    x = "input"
    for i in idx:
        if f"body.{i}.bias" in sd:
            x = g.conv(x, f"body.{i}")
        else:  # PReLU with one slope per channel
            x = g.op("PRelu", [x, g.const(sd[f"body.{i}.weight"].reshape(-1, 1, 1), f"body.{i}.slope")])
    x = g.op("DepthToSpace", [x], blocksize=4, mode="CRD")  # same channel order as torch.pixel_shuffle
    base = g.op("Resize", ["input", "", g.const(np.array([1, 1, 4, 4], np.float32))], mode="nearest",
                coordinate_transformation_mode="asymmetric", nearest_mode="floor")
    out = g.op("Add", [x, base])
    return g.model("input", out, "Real-ESRGAN general x4v3 (SRVGGNetCompact), converted from realesr-general-x4v3.pth, weights unchanged")


def main():
    import onnx

    ap = argparse.ArgumentParser()
    ap.add_argument("src", type=Path)
    ap.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "forensic" / "models" / "weights")
    a = ap.parse_args()
    for fname, sha in SOURCES.items():
        p = a.src / fname
        got = hashlib.sha256(p.read_bytes()).hexdigest()
        if got != sha:
            raise SystemExit(f"{fname}: SHA-256 {got} does not match the pinned {sha}")
        sd = load_state_dict(p)
        m = build_rrdb(sd) if "x4plus" in fname else build_compact(sd)
        onnx.checker.check_model(m)
        dest = a.out / OUTPUTS[fname]
        onnx.save(m, dest)
        print(f"{dest.name}  {hashlib.sha256(dest.read_bytes()).hexdigest()}  (from {fname})")


if __name__ == "__main__":
    main()
