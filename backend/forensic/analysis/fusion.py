"""Fusion map: one map that weighs the local tamper analyses together.

Each local analysis gives, at each place, a degree to which that place looks tampered, and
each is only trustworthy under its own conditions (A-DJPG needs a double-compressed file,
the dimple map an image that has dimples, NA-DJPG a file without a global secondary grid).
Following Fontani et al., every analysis becomes a Dempster-Shafer belief assignment over
{tampered, authentic}: belief r * s in "tampered", r * (1 - s) / 2 in "authentic" and the
rest left uncommitted, where s in [0, 1] is its local strength of tamper evidence and r its
reliability (zero when its condition fails). A-DJPG's strength is taken relative to the
image's own typical block, since its absolute probabilities drift with the qualities used. Dempster's rule combines them place by place. Two analyses that agree
reinforce each other; one that says nothing (r = 0) changes nothing; conflict is measured.

This is a fixed, classical rule (no learned model), so the map is reproducible and every
contribution can be shown separately.
"""

from __future__ import annotations

import cv2
import numpy as np

from .base import AnalysisContext, AnalysisResult, register
from .compression import _colormap
from . import dimples as dm
from . import double_jpeg as dj
from .jpeg import is_jpeg
from .noise import expected_noise, noise_level_map

# Reliability of each source when its condition holds, from its detector card (localisation
# quality on the validation corpus) and kept below 1 so no single source is decisive.
RELIABILITY = {"double_jpeg": 0.6, "nonaligned_grid": 0.45, "dimples": 0.6, "noise": 0.3}
BELIEF_THRESHOLD = 0.55  # only A-DJPG and dimples (reliability 0.6) can pass it alone, and only with strong evidence


def combine(masses: list[tuple[np.ndarray, np.ndarray]]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Dempster's rule over {T, A} for a list of (m(T), m(A)) maps; m(Theta) = 1 - m(T) - m(A).

    Returns (belief in T, plausibility of T, total conflict).
    """
    t, a = masses[0]
    u = 1 - t - a
    conflict = np.zeros_like(t)
    for t2, a2 in masses[1:]:
        u2 = 1 - t2 - a2
        k = t * a2 + a * t2
        norm = np.maximum(1 - k, 1e-9)
        t, a, u = (t * t2 + t * u2 + u * t2) / norm, (a * a2 + a * u2 + u * a2) / norm, (u * u2) / norm
        conflict = 1 - (1 - conflict) * (1 - k)
    return t, t + u, conflict


def _blocks_to_full(b: np.ndarray, shape: tuple[int, int], cell: int = 8) -> np.ndarray:
    out = np.zeros(shape, np.float32)
    big = cv2.resize(b.astype(np.float32), (b.shape[1] * cell, b.shape[0] * cell), interpolation=cv2.INTER_NEAREST)
    hh, ww = min(shape[0], big.shape[0]), min(shape[1], big.shape[1])
    out[:hh, :ww] = big[:hh, :ww]
    return out


def sources(data: bytes | None, img: np.ndarray) -> dict[str, dict]:
    """Per-pixel tamper score p, the mask where it is defined, and reliability r for each source."""
    h, w = img.shape[:2]
    out: dict[str, dict] = {}
    if data is not None and is_jpeg(data):
        res = dj.adjpg(data)
        double = res["score"] >= dj.ADJPG_THRESHOLD and res["supporting_frequencies"] >= 2
        llr = cv2.blur(res["block_llr"], (3, 3), borderType=cv2.BORDER_REFLECT)
        # Relative to the image itself: how much less double-compressed a block looks than the image's typical
        # block (robust z of the log-likelihood ratio). Absolute probabilities drift with the qualities used.
        informative = cv2.blur(res["block_nonzero"].astype(np.float32), (3, 3)) >= 3
        ref = llr[informative] if informative.any() else llr.ravel()
        med = float(np.median(ref))
        mad = float(np.median(np.abs(ref - med))) * 1.4826 + 1e-9
        pz = np.clip(((med - llr) / mad - 2.0) / 4.0, 0, 1)  # 0 below z 2, full at z 6
        out["double_jpeg"] = {"p": _blocks_to_full(pz, (h, w)), "defined": _blocks_to_full(informative, (h, w)) > 0,
                              "r": RELIABILITY["double_jpeg"] if double else 0.0,
                              "condition": "aligned double compression found" if double else "no aligned double compression"}
        y = dj.read_luma(data)
        E = dj.shift_energy(y)
        glob = dj.grid_residual(E.mean(axis=(2, 3)))[2] >= 1.0
        local, heat = dj.local_grid(E, 96, (h, w))
        out["nonaligned_grid"] = {"p": heat, "defined": heat > 0, "r": 0.0 if glob else RELIABILITY["nonaligned_grid"],
                                  "condition": "secondary grid over the whole image" if glob else "no global secondary grid"}
        d = dm.dimples(data)
        present = abs(d["z"]) >= dm.DIMPLE_THRESHOLD
        if present:
            strength, ok = dm.local_strength(d["cells"], d["position"], 8, d["textured"])
            out["dimples"] = {"p": _blocks_to_full(np.clip(1 - strength, 0, 1), (h, w)), "defined": _blocks_to_full(ok, (h, w)) > 0,
                              "r": RELIABILITY["dimples"], "condition": "camera dimple present"}
        else:
            out["dimples"] = {"p": np.zeros((h, w), np.float32), "defined": np.zeros((h, w), bool), "r": 0.0,
                              "condition": "no camera dimple"}
    sigma, reliable, lum, step = noise_level_map(img, 32, 8)
    lr = np.log(np.maximum(sigma, 1e-3) / expected_noise(sigma, reliable, lum))
    pn = np.clip((np.abs(lr) - np.log(2.0)) / np.log(4.0), 0, 1)
    pn_full = np.zeros((h, w), np.float32)
    def_full = np.zeros((h, w), bool)
    o = (32 - step) // 2
    big = cv2.resize(pn.astype(np.float32), (pn.shape[1] * step, pn.shape[0] * step), interpolation=cv2.INTER_NEAREST)
    bigr = cv2.resize(reliable.astype(np.uint8), (pn.shape[1] * step, pn.shape[0] * step), interpolation=cv2.INTER_NEAREST) > 0
    hh, ww = min(big.shape[0], h - o), min(big.shape[1], w - o)
    pn_full[o : o + hh, o : o + ww] = big[:hh, :ww]
    def_full[o : o + hh, o : o + ww] = bigr[:hh, :ww]
    out["noise"] = {"p": pn_full, "defined": def_full, "r": RELIABILITY["noise"], "condition": "always"}
    if "double_jpeg" in out:
        # Flat areas (sky, walls) are where every one of these analyses errs; none speaks there.
        textured = out["double_jpeg"]["defined"]
        for s in out.values():
            s["defined"] = s["defined"] & textured
    return out


@register(
    "tamper.fusion",
    name="Fusion map",
    version="1.0.0",
    category="Local analysis",
    description="Combines the local analyses (aligned and non-aligned double JPEG, JPEG dimples and noise level) into "
    "one map of the belief that each area was altered. Each analysis counts only when its own conditions hold "
    "(for example A-DJPG only in a double-compressed file) and with a fixed weight; agreement raises the belief, "
    "a silent analysis changes nothing. A classical rule, not a learned model: each contribution is listed.",
    reference="M. Fontani, T. Bianchi, A. De Rosa, A. Piva and M. Barni, 'A Framework for Decision Fusion in Image "
    "Forensics Based on Dempster-Shafer Theory of Evidence', IEEE TIFS 8(4), 2013; G. Shafer, A Mathematical Theory "
    "of Evidence, Princeton University Press, 1976.",
    applies_to=("image",),
    reading_guide="Bright = belief of alteration (0 to 1); an area above 0.55 is outlined. The map is only as good as its "
    "inputs: check which analyses contributed (listed with their conditions) and open their own maps. A non-JPEG image "
    "has only the noise contribution, which is weak.",
)
def fusion(ctx: AnalysisContext, p: dict) -> AnalysisResult:
    img = ctx.image
    h, w = img.shape[:2]
    data = ctx.data if is_jpeg(ctx.data) else None
    src = sources(data, img)
    masses = []
    used = []
    for name, s in src.items():
        r = np.where(s["defined"], s["r"], 0.0)
        # A strength of tamper evidence; its absence only half supports authenticity.
        masses.append((r * s["p"], 0.5 * r * (1 - s["p"])))
        used.append({"analysis": name, "reliability": s["r"], "condition": s["condition"], "contributes": s["r"] > 0})
    belief, plaus, conflict = combine(masses)
    bel = belief.astype(np.float32)
    canvas = np.clip(np.rint(_colormap(bel) * 255), 0, 255).astype(np.uint8)
    region_mask = cv2.morphologyEx((bel >= BELIEF_THRESHOLD).astype(np.uint8), cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
    n, lab, stats, _ = cv2.connectedComponentsWithStats(region_mask, connectivity=8)
    regions = []
    min_area = max(1024, h * w // 100)
    for i in range(1, n):
        x, y, bw, bh, area = (int(v) for v in stats[i])
        if area < min_area:
            continue
        sel = lab == i
        regions.append({"bbox_px": [x, y, bw, bh], "area_share": round(area / (h * w), 4),
                        "mean_belief": round(float(bel[sel].mean()), 3),
                        "supported_by": [k for k, s in src.items() if s["r"] > 0 and float((s["p"][sel] * s["defined"][sel]).mean()) >= 0.5]})
        cnts, _ = cv2.findContours(sel.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(canvas, cnts, -1, (255, 255, 255), 2)
    share = float(sum(r["area_share"] for r in regions))
    contributing = [u["analysis"] for u in used if u["contributes"]]
    flags = []
    if regions and share <= 0.4:
        agree = sum(len(r["supported_by"]) >= 2 for r in regions)
        flags.append({"level": "warning", "code": "fusion.local",
                      "text": f"{len(regions)} area(s) with belief of alteration above {BELIEF_THRESHOLD}"
                      + (f", {agree} backed by two or more analyses" if agree else ", each backed by one analysis") + "; inspect the map."})
    findings = {
        "contributions": used,
        "belief_threshold": BELIEF_THRESHOLD,
        "regions": regions,
        "max_belief": round(float(bel.max()), 3),
        "mean_conflict": round(float(conflict.mean()), 3),
    }
    summary = (f"{len(regions)} area(s) above belief {BELIEF_THRESHOLD} from {len(contributing)} contributing analyses."
               if regions else f"No area above belief {BELIEF_THRESHOLD} ({len(contributing)} contributing analyses).")
    return AnalysisResult(summary=summary, findings=findings, flags=flags, maps={"fusion_belief": canvas.astype(np.float32) / 255.0})
