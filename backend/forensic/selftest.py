"""Acceptance self-test for an installation (including an air-gapped one).

    python -m forensic.selftest            # exit code 0 when every check passes

Checks the tools and models are present and unmodified, that learned models run
deterministically on this machine (GPU or CPU), and runs a small case end to end in a temporary
data folder: ingest, render, reproduce, analyse, measure, review and report.
It makes no network connection.
"""

from __future__ import annotations

import io
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

import numpy as np


def _checks():
    from . import __version__, compute, models
    from .audit import Actor
    from .config import load_settings

    yield "version", lambda: __version__

    def tools():
        s = load_settings(Path(tempfile.gettempdir()) / "forensic-selftest-probe")
        missing = [t for t in (s.ffmpeg, s.ffprobe) if not shutil.which(t)]
        if missing:
            raise RuntimeError(f"not found on PATH: {', '.join(missing)}")
        from .media import ffmpeg_version

        return ffmpeg_version(s.ffmpeg)

    yield "ffmpeg and ffprobe", tools

    def model_hashes():
        bad = [v for v in models.verify_all() if not v["ok"]]
        if bad:
            raise RuntimeError("; ".join(f"{v['id']}: {v['problem']}" for v in bad))
        return f"{len(models.manifest())} model(s), weights match pinned SHA-256"

    yield "model weights", model_hashes

    def sr_deterministic():
        x = np.random.default_rng(0).random((1, 1, 48, 48)).astype(np.float32)
        a, b = models.run("sr.fsrcnn_x2", x), models.run("sr.fsrcnn_x2", x)
        if not np.array_equal(a, b) or a.shape != (1, 1, 96, 96):
            raise RuntimeError("super-resolution output is not reproducible on this machine")
        return f"FSRCNN x2 reproducible on {models.device('sr.fsrcnn_x2')}; {compute.describe()}"

    yield "ONNX Runtime", sr_deterministic

    def faces():
        models.face_detector(320, 240)
        return "YuNet loads"

    yield "face detector", faces

    def c2pa():
        import c2pa as _c2pa  # noqa: F401

        from .workbench import _c2pa_version

        return f"c2pa-python {_c2pa_version()} (offline validation)"

    yield "C2PA reader", c2pa

    def qt_database():
        from . import qtables

        st = qtables.stats(qtables.load())
        if st["camera_models"] < 100:
            raise RuntimeError(f"built-in database incomplete ({st['camera_models']} camera models)")
        return f"{st['camera_models']} camera models, {st['tables']} table sets"

    yield "quantisation-table database", qt_database

    def end_to_end():
        from PIL import Image

        from .agents import review_case
        from .report import build_bundle, render_pdf
        from .workbench import Workbench

        with tempfile.TemporaryDirectory() as d:
            wb = Workbench(load_settings(Path(d) / "data"))
            try:
                me = Actor("selftest")
                case = wb.create_case("Self-test", "selftest", me)
                rng = np.random.default_rng(1)
                img = (np.clip(rng.normal(0.5, 0.2, (120, 160, 3)), 0, 1) * 255).astype(np.uint8)
                buf = io.BytesIO()
                Image.fromarray(img).save(buf, "JPEG", quality=85)
                ev = wb.ingest(case["id"], io.BytesIO(buf.getvalue()), "selftest.jpg", me)
                ch = wb.create_chain(case["id"], ev["id"], "check", me, {"source": {}, "nodes": [
                    {"id": "n1", "filter": "exposure.clahe", "params": {}},
                    {"id": "n2", "filter": "ai.super_resolution", "params": {"scale": "2"},
                     "justification": "Installation self-test of the generative path only."}]})
                wb.render_chain(ch["id"], me)
                if not wb.verify_chain(ch["id"], me)["reproduced"]:
                    raise RuntimeError("chain did not reproduce bit for bit")
                wb.smart_report(ev["id"], me)
                wb.measure(ev["id"], "distance", {"plane_image_points": [[0, 0], [150, 0], [150, 110], [0, 110]],
                                                  "plane_world_points": [[0, 0], [3, 0], [3, 2], [0, 2]],
                                                  "point_a": [10, 10], "point_b": [140, 100]}, me)
                rv = review_case(wb, case["id"])
                fails = [i["text"] for i in rv["result"]["items"] if i["level"] == "fail"]
                if fails:
                    raise RuntimeError("reviewer: " + "; ".join(fails))
                pdf = render_pdf(wb, build_bundle(wb, case["id"], me))
                if not pdf.startswith(b"%PDF"):
                    raise RuntimeError("PDF report not produced")
                return f"case run end to end; reviewer {rv['level']} ({rv['result']['line']})"
            finally:
                wb.db.close()

    yield "end-to-end case", end_to_end


def main() -> int:
    ok = True
    for name, fn in _checks():
        try:
            detail = fn()
            print(f"PASS  {name}: {detail}")
        except Exception as e:  # noqa: BLE001
            ok = False
            print(f"FAIL  {name}: {e}")
            if "-v" in sys.argv:
                traceback.print_exc()
    print("All checks passed." if ok else "Self-test FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
