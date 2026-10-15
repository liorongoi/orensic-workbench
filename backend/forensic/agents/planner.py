"""Enhancement planner agent.

Given a goal ("make this plate readable", "brighten this night scene", "stabilise and
integrate frames 1200 to 1260"), it picks a workflow, builds candidate chains from
classical filters, previews each on the region of interest, ranks them with
no-reference quality measures and proposes the best two as new chains. The proposals
carry the planner's name and stay out of the report's findings until the examiner
accepts them; the examiner judges which, if either, is fit for purpose.
"""

from __future__ import annotations

import re

from ..audit import Actor
from ..filters import FilterError
from .quality import SCORE_NOTE, metrics, score

AGENT = Actor("planner-agent", "agent", "rules-1.0.0")

WORKFLOWS = {
    "plate": "Licence plate or text readability: denoise, deconvolve a mild blur, sharpen, local contrast; "
    "multi-frame super resolution or integration first when a frame range is given.",
    "low_light": "Dark or night footage: noise reduction with gamma or local-contrast lifting.",
    "stabilise_integrate": "Several frames of a static subject: register the frames, then average them to cut noise.",
    "general": "General clarity: mild denoise, sharpen and local contrast.",
}


def choose_workflow(goal: str, multi_frame: bool) -> str:
    g = goal.lower()
    if re.search(r"plate|licen[cs]e|registration|number|text|read", g):
        return "plate"
    if re.search(r"stabili[sz]|shak|integrat|averag|stack", g) and multi_frame:
        return "stabilise_integrate"
    if re.search(r"dark|night|low.?light|underexpos|bright", g):
        return "low_light"
    return "general"


def _node(f: str, **params) -> dict:
    return {"filter": f, "params": params}


def candidates(workflow: str, multi_frame: bool, roi_pts: list | None) -> list[tuple[str, list[dict]]]:
    roi = roi_pts or []
    out: list[tuple[str, list[dict]]] = []
    if workflow == "plate":
        prefixes: list[tuple[str, list[dict]]] = [("single frame", [])]
        if multi_frame:
            prefixes = [
                ("super resolution x2", [_node("stack.super_resolution", scale=2, motion="affine", roi=roi)]),
                ("stabilise + sigma-clipped integration", [_node("stack.stabilise", motion="affine", roi=roi),
                                                           _node("stack.integrate", method="sigma_clip")]),
            ]
        for pname, pre in prefixes:
            out += [
                (f"{pname}, NLM denoise, unsharp, CLAHE", pre + [_node("denoise.nlm", strength=4.0, color_strength=4.0),
                                                                 _node("sharpen.unsharp_mask", radius=1.5, amount=1.2),
                                                                 _node("exposure.clahe", clip_limit=2.0, tiles=8)]),
                (f"{pname}, Richardson-Lucy (Gaussian 1.2), CLAHE", pre + [
                    _node("deblur.richardson_lucy", psf="gaussian", sigma=1.2, iterations=15, tv=0.002),
                    _node("exposure.clahe", clip_limit=2.0, tiles=8)]),
                (f"{pname}, Wiener (Gaussian 1.5), bilateral", pre + [
                    _node("deblur.wiener", psf="gaussian", sigma=1.5, nsr=0.02),
                    _node("denoise.bilateral", diameter=5, sigma_color=0.08, sigma_space=3.0)]),
                (f"{pname}, unsharp strong", pre + [_node("sharpen.unsharp_mask", radius=2.0, amount=2.0)]),
            ]
    elif workflow == "low_light":
        pre = [_node("stack.stabilise", motion="euclidean", roi=roi), _node("stack.integrate", method="mean")] if multi_frame else []
        out.append(("Low-light enhancement (illumination map)", pre + [_node("exposure.low_light")]))
        for gamma in (1.6, 2.2):
            out.append((f"NLM denoise, gamma {gamma}", pre + [_node("denoise.nlm", strength=8.0, color_strength=8.0),
                                                            _node("exposure.levels", gamma=gamma)]))
        out.append(("NLM denoise, CLAHE", pre + [_node("denoise.nlm", strength=8.0, color_strength=8.0),
                                                 _node("exposure.clahe", clip_limit=3.0, tiles=8)]))
        out.append(("gamma 1.8, CLAHE, bilateral", pre + [_node("exposure.levels", gamma=1.8),
                                                          _node("exposure.clahe", clip_limit=2.0, tiles=8),
                                                          _node("denoise.bilateral", diameter=7, sigma_color=0.1, sigma_space=5.0)]))
    elif workflow == "stabilise_integrate":
        for motion in ("translation", "euclidean", "affine"):
            for method in ("mean", "sigma_clip"):
                out.append((f"stabilise ({motion}) + {method} integration",
                            [_node("stack.stabilise", motion=motion, roi=roi), _node("stack.integrate", method=method)]))
    else:
        out += [
            ("unsharp mask", [_node("sharpen.unsharp_mask", radius=1.5, amount=1.0)]),
            ("NLM denoise, unsharp", [_node("denoise.nlm", strength=5.0, color_strength=5.0), _node("sharpen.unsharp_mask", radius=1.5, amount=1.2)]),
            ("CLAHE, unsharp", [_node("exposure.clahe", clip_limit=2.0, tiles=8), _node("sharpen.unsharp_mask", radius=1.2, amount=0.8)]),
        ]
    return out


def _crop(img, roi, src_shape):
    """Crop the region of interest given in source pixels, scaled to the output's size."""
    if not roi:
        return img
    sy, sx = img.shape[0] / src_shape[0], img.shape[1] / src_shape[1]
    x, y, w, h = roi
    x0, y0 = max(0, int(x * sx)), max(0, int(y * sy))
    return img[y0 : y0 + max(1, int(h * sy)), x0 : x0 + max(1, int(w * sx))]


def plan_enhancement(wb, case_id: str, evidence_id: str, goal: str, roi: list[int] | None = None,
                     frames: dict | None = None, workflow: str | None = None, propose: int = 2,
                     actor: Actor | None = None) -> dict:
    """Rank candidate chains for `goal` and create the best `propose` as proposed chains.

    `roi` is [x, y, width, height] in source pixels; `frames` a {start, end} range for video.
    """
    actor = actor or AGENT
    ev = wb.get_evidence(evidence_id)
    if ev["case_id"] != case_id:
        raise ValueError("evidence does not belong to this case")
    if ev["media_type"] not in ("image", "video"):
        raise ValueError("convert this recorder export before planning an enhancement")
    if roi is not None and (len(roi) != 4 or roi[2] <= 0 or roi[3] <= 0):
        raise ValueError("roi must be [x, y, width, height] with a positive size")
    multi = bool(frames and ev["media_type"] == "video" and frames.get("end", 0) > frames.get("start", 0))
    if not frames:
        frames = {"start": 0, "end": 0} if ev["media_type"] == "video" else None
    wf = workflow or choose_workflow(goal, multi)
    if wf not in WORKFLOWS:
        raise ValueError(f"unknown workflow '{wf}'")
    roi_pts = [[roi[0], roi[1]], [roi[0] + roi[2], roi[1] + roi[3]]] if roi else None
    base_img, _ = wb.preview({"source": {"frames": frames}, "nodes": []}, evidence_id)
    src_shape = base_img.shape[:2]
    before = metrics(_crop(base_img, roi, src_shape))
    ranked = []
    for label, nodes in candidates(wf, multi, roi_pts):
        graph = {"source": {"frames": frames}, "nodes": nodes}
        try:
            img, info = wb.preview(graph, evidence_id)
        except FilterError as e:
            ranked.append({"label": label, "nodes": nodes, "error": str(e), "score": None})
            continue
        after = metrics(_crop(img, roi, src_shape))
        ranked.append({"label": label, "nodes": nodes, "score": score(before, after), "metrics": after,
                       "output_hash": info["output_hash"]})
    ok = sorted([r for r in ranked if r["score"] is not None], key=lambda r: -r["score"])
    proposed = []
    for i, r in enumerate(ok[: max(0, propose)]):
        ch = wb.create_chain(case_id, evidence_id, f"Planner {chr(65 + i)}: {r['label']}", actor,
                             {"source": {"frames": frames}, "nodes": [dict(n, note=f"planner: {goal[:200]}") for n in r["nodes"]]})
        r["chain_id"] = ch["id"]
        proposed.append(ch["id"])
    wb.audit.append(actor, "planner.plan", case_id=case_id, target=evidence_id,
                    params={"goal": goal[:500], "workflow": wf, "roi": roi, "frames": frames,
                            "ranking": [{"label": r["label"], "score": r["score"]} for r in ranked], "proposed": proposed})
    return {"workflow": wf, "workflow_description": WORKFLOWS[wf], "baseline": before, "candidates": ranked,
            "proposed_chains": proposed, "score_note": SCORE_NOTE}
