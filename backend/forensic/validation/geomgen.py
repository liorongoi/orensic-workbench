"""Validation of the geometric checks and the bundled generated-image detector.

    python -m forensic.validation.geomgen geometry                 # seeded synthetic constraints
    python -m forensic.validation.geomgen generated --sample DIR   # DIR/real/*, DIR/fake/<generator>/*
    python -m forensic.validation.geomgen generated --parquet SHARDS  # Community Forensics evaluation shards

Geometry: constraint sets are drawn from one light (or vanishing point) with simulated click
error; a manipulated set has one constraint whose direction is turned by a known angle, as an
object pasted in under a different light would show. The generated-image detector is measured
on a held-out sample of real photographs and generated images from generators named per folder,
stored as given and after JPEG re-compression and down-scaling.
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import math
import platform
from pathlib import Path

import numpy as np
from PIL import Image

from ..analysis import ANALYSES
from ..analysis import geometry as geo
from .cards import CARD_DIR

W, H = 640, 480
TOLERANCE = 2.0  # the analyses' defaults
CLICK_PX = 2.0
CLICK_SIGMA = (1.0, 2.0)  # simulated click error (standard deviation per coordinate)
ANGLES = (5, 10, 20, 45)
SIZES = (3, 5, 8)


def _software() -> dict:
    import cv2

    from .. import __version__

    return {"forensic_workbench": __version__, "numpy": np.__version__, "opencv": cv2.__version__,
            "pillow": Image.__version__, "python": platform.python_version()}


def _target(rng) -> np.ndarray:
    """A light projection or vanishing point: inside the frame, near it, or far away."""
    kind = rng.integers(3)
    if kind == 0:
        return rng.uniform([0, 0], [W, H])
    r = rng.uniform(1.5, 4.0) * math.hypot(W, H) if kind == 1 else rng.uniform(10, 50) * math.hypot(W, H)
    a = rng.uniform(0, 2 * math.pi)
    return np.array([W / 2 + r * math.cos(a), H / 2 + r * math.sin(a)])


def _constraints(rng, P, n, sigma, two_sided, behind=False, turn_one=0.0):
    pts = []
    k = int(rng.integers(n)) if turn_one else -1
    for i in range(n):
        while True:
            o = rng.uniform([40, 40], [W - 40, H - 40])
            d = P - o
            if np.hypot(*d) > (60 if behind else 5):
                break
        d = d / np.hypot(*d)
        if i == k:
            t = math.radians(turn_one) * (1 if rng.random() < 0.5 else -1)
            d = np.array([math.cos(t) * d[0] - math.sin(t) * d[1], math.sin(t) * d[0] + math.cos(t) * d[1]])
        length = rng.uniform(40, 160)
        if behind:  # a shadow cannot reach past the light's projection
            length = min(length, 0.9 * float(np.hypot(*(P - o))))
        if two_sided:
            a, b = o, o + d * length * (1 if rng.random() < 0.5 else -1)
        elif behind:  # light behind the camera: the shadow lies toward the light's projection
            a, b = o + d * length, o
        else:
            a, b = o - d * length, o
        pts += [list(a + rng.normal(0, sigma, 2)), list(b + rng.normal(0, sigma, 2))]
    return pts


def _verdict(pts, two_sided) -> bool:
    """True when the set is found inconsistent."""
    ws = geo.wedges(pts, TOLERANCE, CLICK_PX)
    if two_sided:  # as geometry.perspective: apex at the segment midpoint
        mid = []
        for i in range(0, len(pts), 2):
            mid += [list((np.array(pts[i]) + np.array(pts[i + 1])) / 2), pts[i + 1]]
        ws = geo.wedges(mid, TOLERANCE, CLICK_PX / 2)
    d = math.hypot(W, H) * geo.FAR
    return geo.solve(ws, two_sided, (-d, -d, W + d, H + d)) is None


def eval_geometry(two_sided: bool, trials: int = 60, seed: int = 0) -> dict:
    rng = np.random.default_rng(seed)
    neg, pos = {}, {a: [] for a in ANGLES}
    by_size = {n: [] for n in SIZES}
    for sigma in CLICK_SIGMA:
        flags = []
        for _ in range(trials):
            n = int(rng.choice(SIZES))
            P = _target(rng)
            behind = (not two_sided) and rng.random() < 0.3
            flags.append(_verdict(_constraints(rng, P, n, sigma, two_sided, behind), two_sided))
            for a in ANGLES:
                hit = _verdict(_constraints(rng, P, n, sigma, two_sided, behind, turn_one=a), two_sided)
                pos[a].append(hit)
                if a == 20:
                    by_size[n].append(hit)
        neg[f"click error sigma {sigma:g} px"] = round(float(np.mean(flags)), 4)
    all_neg = float(np.mean(list(neg.values())))
    return {
        "task": "Is one constraint inconsistent with the others (as an object pasted in under another light or perspective)?",
        "positives": f"{trials * len(CLICK_SIGMA)} sets of 3, 5 or 8 constraints per angle, one turned by the stated angle",
        "negatives": f"{trials * len(CLICK_SIGMA)} consistent sets with simulated click error (sigma 1 and 2 px)",
        "tolerance_deg": TOLERANCE,
        "click_error_px": CLICK_PX,
        "true_positive_rate": round(float(np.mean(pos[20])), 4),
        "false_positive_rate": round(all_neg, 4),
        "tpr_by_turn_angle": {f"{a} deg": round(float(np.mean(v)), 4) for a, v in pos.items()},
        "tpr_at_20_deg_by_constraints": {f"{n} constraints": round(float(np.mean(v)), 4) for n, v in by_size.items() if v},
        "fpr_by_click_error": neg,
    }


GEOMETRY_MODES = {
    "geometry.shadows": [
        "Detects a conflict only when the turned constraint leaves the others' feasible region: a pasted object lit from a "
        "similar direction (small angle) passes, and with few constraints the region is wide (see tpr_by_turn_angle).",
        "Wrongly paired shadow and object points, extended or diffuse lights, and shadows on non-planar ground produce "
        "false conflicts; synthetic constraints cannot measure how often examiners mis-pair points.",
    ],
    "geometry.reflections": [
        "Requires one planar reflecting surface; curved, rippled or several mirrors break the model.",
        "Measured with the same synthetic generator as the shadow check (the geometry is identical: lines meeting at one "
        "point, with one global orientation).",
    ],
    "geometry.perspective": [
        "Lens distortion bends straight lines and can create false conflicts; use lens-corrected images.",
        "Lines that are only nearly parallel in the scene, or that belong to different directions, produce false conflicts.",
    ],
}


def geometry_cards(trials: int = 60) -> dict[str, dict]:
    shadows = eval_geometry(False, trials, seed=1)
    return {
        "geometry.shadows": {"detection": shadows},
        "geometry.reflections": {"detection": eval_geometry(False, trials, seed=2)},
        "geometry.perspective": {"detection": eval_geometry(True, trials, seed=3)},
    }


# Generated-image detector --------------------------------------------------------------------------

EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def _load(p: Path) -> np.ndarray:
    with Image.open(p) as im:
        return np.asarray(im.convert("RGB"), np.float32) / 255.0


def _jpeg(rgb: np.ndarray, q: int) -> np.ndarray:
    buf = io.BytesIO()
    Image.fromarray((rgb * 255 + 0.5).astype(np.uint8)).save(buf, "JPEG", quality=q)
    return _load(io.BytesIO(buf.getvalue()))  # type: ignore[arg-type]


def _half(rgb: np.ndarray) -> np.ndarray:
    im = Image.fromarray((rgb * 255 + 0.5).astype(np.uint8))
    im = im.resize((max(1, im.width // 2), max(1, im.height // 2)), Image.BICUBIC)
    return np.asarray(im, np.float32) / 255.0


def _auc(pos: list[float], neg: list[float]) -> float:
    p, n = np.asarray(pos), np.asarray(neg)
    if not len(p) or not len(n):
        return float("nan")
    gt = (p[:, None] > n[None, :]).sum() + 0.5 * (p[:, None] == n[None, :]).sum()
    return float(gt / (len(p) * len(n)))


def _folder_samples(sample: Path):
    """DIR/real/[<source>/]* and DIR/fake/<generator>/* -> (is_fake, group, format, loader)."""
    for p in sorted((sample / "real").rglob("*")):
        if p.suffix.lower() in EXTS:
            src = p.parent.name if p.parent != sample / "real" else "real"
            yield False, src, p.suffix.lower().lstrip("."), lambda p=p: _load(p)
    for d in sorted((sample / "fake").iterdir()):
        for p in sorted(d.rglob("*")) if d.is_dir() else []:
            if p.suffix.lower() in EXTS:
                yield True, d.name, p.suffix.lower().lstrip("."), lambda p=p: _load(p)


def _parquet_samples(files: list[Path]):
    """Community Forensics evaluation shards (columns label, model_name, real_source, format, image_data)."""
    import pyarrow.parquet as pq

    for f in files:
        t = pq.read_table(f, columns=["label", "model_name", "real_source", "format", "image_data"]).to_pylist()
        for r in t:
            fake = int(r["label"]) == 1
            data = r["image_data"]
            if isinstance(data, dict):
                data = data.get("bytes")
            group = r["model_name"] if fake else (r["real_source"] or "real")
            yield fake, str(group), str(r["format"] or "?").lower(), lambda d=data: _load(io.BytesIO(d))  # type: ignore[arg-type]


def eval_generated(samples, log=print) -> dict:
    from ..analysis.generated import score

    conds = {"as given": lambda x: x, "jpeg 90": lambda x: _jpeg(x, 90), "jpeg 75": lambda x: _jpeg(x, 75),
             "half size": _half}
    rows = []  # (is_fake, group, format, {condition: score})
    for i, (fake, group, fmt, load) in enumerate(samples):
        try:
            x = load()
        except Exception as e:  # noqa: BLE001 - unreadable sample: skip and count
            log(f"skipped {group}: {e}")
            continue
        rows.append((fake, group, fmt, {c: score(f(x)) for c, f in conds.items()}))
        if i % 100 == 0:
            log(f"{i} images")
    rate = lambda xs: round(float(np.mean(np.asarray(xs) >= 0.5)), 4) if len(xs) else None  # noqa: E731
    pick = lambda fake, c, key=None, val=None: [r[3][c] for r in rows if r[0] == fake and (key is None or r[key] == val)]  # noqa: E731
    base = "as given"
    gens = sorted({r[1] for r in rows if r[0]})
    srcs = sorted({r[1] for r in rows if not r[0]})
    fmts = sorted({r[2] for r in rows})
    return {
        "task": "Was the whole image made by a generative model (diffusion, GAN, other)?",
        "positives": f"{len(pick(True, base))} generated images from {len(gens)} generators unseen in training",
        "negatives": f"{len(pick(False, base))} real images ({', '.join(srcs)})",
        "threshold": 0.5,
        "true_positive_rate": rate(pick(True, base)),
        "false_positive_rate": rate(pick(False, base)),
        "auc": round(_auc(pick(True, base), pick(False, base)), 4),
        "tpr_by_generator": {g: rate(pick(True, base, 1, g)) for g in gens},
        "images_by_generator": {g: len(pick(True, base, 1, g)) for g in gens},
        "fpr_by_real_source": {s: rate(pick(False, base, 1, s)) for s in srcs},
        "images_by_real_source": {s: len(pick(False, base, 1, s)) for s in srcs},
        "by_stored_format": {f: {"tpr": rate(pick(True, base, 2, f)), "fpr": rate(pick(False, base, 2, f)),
                                 "fake": len(pick(True, base, 2, f)), "real": len(pick(False, base, 2, f))} for f in fmts},
        "tpr_by_condition": {c: rate(pick(True, c)) for c in conds},
        "fpr_by_condition": {c: rate(pick(False, c)) for c in conds},
        "auc_by_condition": {c: round(_auc(pick(True, c), pick(False, c)), 4) for c in conds},
        "note": "Real images in the sample are mostly JPEG and generated ones mostly PNG; the 'jpeg 90' and 'jpeg 75' "
        "conditions re-encode both alike, so they show accuracy without that format cue.",
    }


GENERATED_MODES = [
    "Generators released after the model was trained (late 2024) can be missed; the authors report the lowest accuracy on "
    "commercial models. Check the per-generator rates and treat a low score on a modern image with caution.",
    "Re-compression and down-scaling move scores (see the by-condition rates); screenshots and social-media copies are "
    "harder than originals.",
    "Scores the whole image: a generated object pasted into a real photograph, or an inpainted region, is usually not detected.",
    "Heavily processed real photos (phone night modes, beautification, strong denoising or upscaling) can score as generated.",
]


def _write(det: str, results: dict, corpus: str, seeded: bool, modes: list[str], command: str, out: Path) -> Path:
    a = ANALYSES[det]
    card = {"detector": det, "name": a.name, "version": a.version, "validated_on": datetime.date.today().isoformat(),
            "corpus": corpus, "seeded": seeded, "results": results, "known_failure_modes": modes,
            "software": _software(), "how_to_reproduce": command}
    p = out / f"{det}.json"
    p.write_text(json.dumps(card, indent=2) + "\n")
    return p


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m forensic.validation.geomgen", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("geometry")
    g.add_argument("--trials", type=int, default=60)
    d = sub.add_parser("generated")
    d.add_argument("--sample", type=Path, help="folder with real/[<source>/] and fake/<generator>/ images")
    d.add_argument("--parquet", type=Path, nargs="*", help="Community Forensics evaluation shards instead of a folder")
    d.add_argument("--corpus", default="", help="description of the sample for the card")
    for p in (g, d):
        p.add_argument("--out", type=Path, default=CARD_DIR)
    a = ap.parse_args(argv)
    if a.cmd == "geometry":
        for det, res in geometry_cards(a.trials).items():
            print(_write(det, res, "seeded synthetic constraint sets (forensic.validation.geomgen)", True,
                         GEOMETRY_MODES[det], "python -m forensic.validation.geomgen geometry", a.out))
            print(json.dumps(res, indent=2))
    else:
        if not a.sample and not a.parquet:
            ap.error("give --sample DIR or --parquet FILES")
        res = eval_generated(_parquet_samples(a.parquet) if a.parquet else _folder_samples(a.sample))
        print(_write("synthetic.generated", {"detection": res}, a.corpus or str(a.sample or "parquet shards"), False,
                     GENERATED_MODES, "python -m forensic.validation.geomgen generated --parquet SHARDS", a.out))
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
