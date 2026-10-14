"""Phase 2: deblur, super resolution, double JPEG, PRNU, measurement, DVR formats, triage and planner."""

import io
import subprocess

import cv2
import numpy as np
import pytest
from PIL import Image

from forensic import formats
from forensic.agents import plan_enhancement, triage_case
from forensic.filters import get_filter
from forensic.filters.deblur import estimate_motion_blur, make_psf

from .conftest import EXAMINER, ingest


def _texture(h=256, w=256, seed=0):
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 3))
    for s, a in ((2, 0.15), (8, 0.25), (32, 0.35)):
        n = rng.normal(0, 1, (h // s + 2, w // s + 2, 3))
        img += a * cv2.resize(n, (w, h), interpolation=cv2.INTER_CUBIC)
    return np.clip(0.5 + 0.18 * img, 0, 1).astype(np.float32)


def _sharp(h=256, w=256):
    """Hard edges and text: content that blur visibly destroys."""
    yy, xx = np.mgrid[0:h, 0:w]
    u8 = (255 * (0.5 + 0.2 * np.sign(np.sin(xx / 3.1) * np.sin(yy / 4.3)))).astype(np.uint8)
    u8 = np.repeat(u8[..., None], 3, axis=2).copy()
    cv2.putText(u8, "AB12CD", (10, h // 2), cv2.FONT_HERSHEY_SIMPLEX, 1.6, (12, 12, 12), 3)
    return u8.astype(np.float32) / 255


def _jpeg(arr_u8, q) -> bytes:
    b = io.BytesIO()
    Image.fromarray(arr_u8).save(b, "JPEG", quality=q)
    return b.getvalue()


def _decode(data):
    return np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))


def _psnr(a, b, m=8):
    a, b = a[m:-m, m:-m], b[m:-m, m:-m]
    return 10 * np.log10(1 / np.mean((a - b) ** 2))


# Enhancement -------------------------------------------------------------------------------


@pytest.mark.parametrize("fid,extra", [("deblur.wiener", {"nsr": 0.005}), ("deblur.richardson_lucy", {"iterations": 25, "tv": 0.002})])
def test_deconvolution_restores_motion_blur(fid, extra):
    img = _sharp()
    p = {"psf": "motion", "length": 9.0, "angle": 30.0}
    psf = make_psf({**p, "radius": 3.0, "sigma": 1.5}).astype(np.float32)
    blurred = np.clip(cv2.filter2D(img, -1, psf, borderType=cv2.BORDER_REFLECT)
                      + np.random.default_rng(1).normal(0, 0.003, img.shape), 0, 1).astype(np.float32)
    f = get_filter(fid)
    out = f.fn(blurred, f.validate({**p, **extra}))
    assert _psnr(out, img) > _psnr(blurred, img) + 2.0


def test_motion_blur_estimate():
    img = _sharp()
    psf = make_psf({"psf": "motion", "length": 12.0, "angle": 0.0, "radius": 3.0, "sigma": 1.5}).astype(np.float32)
    y = cv2.cvtColor(cv2.filter2D(img, -1, psf, borderType=cv2.BORDER_REFLECT), cv2.COLOR_RGB2GRAY)
    est = estimate_motion_blur(y)
    assert abs(est["length"] - 12) <= 1 and min(est["angle"] % 180, 180 - est["angle"] % 180) <= 3
    assert est["strength"] > 4


def test_super_resolution_beats_bicubic():
    rng = np.random.default_rng(0)
    hr = _sharp(192, 192)
    s = 2
    frames, shifts = [], []
    for _ in range(8):
        dx, dy = rng.uniform(-2, 2, 2)
        shifts.append((dx, dy))
        sh = cv2.warpAffine(hr, np.float32([[1, 0, dx], [0, 1, dy]]), (192, 192), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
        lr = cv2.resize(cv2.GaussianBlur(sh, (0, 0), 0.8 * s), (96, 96), interpolation=cv2.INTER_AREA)
        frames.append(np.clip(lr + rng.normal(0, 0.005, lr.shape), 0, 1).astype(np.float32))
    f = get_filter("stack.super_resolution")
    out = f.fn(np.stack(frames), f.validate({"scale": 2, "motion": "translation"}))
    assert out.shape == (192, 192, 3)
    dx, dy = shifts[0]
    gt = cv2.warpAffine(hr, np.float32([[1, 0, dx], [0, 1, dy]]), (192, 192), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT)
    bicubic = cv2.resize(frames[0], (192, 192), interpolation=cv2.INTER_CUBIC)
    assert _psnr(out, gt, 12) > _psnr(bicubic, gt, 12) + 1.0


def test_new_filters_reproduce_in_a_chain(wb, case, video_path):
    ev = ingest(wb, case, video_path)
    ch = wb.create_chain(case["id"], ev["id"], "sr", EXAMINER, {
        "source": {"frames": {"start": 0, "end": 4}},
        "nodes": [{"filter": "stack.super_resolution", "params": {"scale": 2, "iterations": 3}},
                  {"filter": "deblur.richardson_lucy", "params": {"psf": "gaussian", "sigma": 1.0, "iterations": 3}}],
    })
    r = wb.render_chain(ch["id"], EXAMINER)
    assert r["shape"] == [240, 320, 3]
    assert wb.verify_chain(ch["id"], EXAMINER)["reproduced"]


# Double JPEG -------------------------------------------------------------------------------


def test_double_jpeg_global_and_first_quality(wb, case, tmp_path):
    x = (_texture(256, 256, seed=7) * 255).astype(np.uint8)
    single = tmp_path / "single.jpg"
    single.write_bytes(_jpeg(x, 90))
    double = tmp_path / "double.jpg"
    double.write_bytes(_jpeg(_decode(_jpeg(x, 60)), 90))
    r1 = wb.run_analysis(ingest(wb, case, single)["id"], "tamper.double_jpeg", {}, EXAMINER)["result"]
    r2 = wb.run_analysis(ingest(wb, case, double)["id"], "tamper.double_jpeg", {}, EXAMINER)["result"]
    assert r1["findings"]["double_compressed"] is False
    assert r2["findings"]["double_compressed"] is True
    assert abs(r2["findings"]["estimated_first_quality"] - 60) <= 3
    assert any(f.get("code") == "djpg.double" for f in r2["flags"])


def test_double_jpeg_localises_splice(tmp_path):
    from forensic.analysis.double_jpeg import adjpg

    bg = _decode(_jpeg((_texture(384, 384, seed=8) * 255).astype(np.uint8), 60)).copy()
    patch = (_texture(384, 384, seed=9) * 255).astype(np.uint8)
    bg[128:256, 128:256] = patch[128:256, 128:256]
    llr = adjpg(_jpeg(bg, 90))["block_llr"]
    inside = llr[18:30, 18:30].mean()
    outside = np.concatenate([llr[:12].ravel(), llr[36:].ravel()]).mean()
    assert inside < 0 < outside


def test_nonaligned_grid_detects_crop(wb, case, tmp_path):
    x = (_texture(320, 320, seed=11) * 255).astype(np.uint8)
    cropped = _decode(_jpeg(x, 70))[5:, 3:]
    p = tmp_path / "cropped.jpg"
    p.write_bytes(_jpeg(np.ascontiguousarray(cropped), 90))
    res = wb.run_analysis(ingest(wb, case, p)["id"], "tamper.nonaligned_grid", {}, EXAMINER)["result"]
    assert res["findings"]["secondary_grid"] is True
    assert res["findings"]["offset_xy"] == [5, 3]


# PRNU -----------------------------------------------------------------------------------------


def _camera_shot(K, seed, flat=False, size=(256, 320)):
    h, w = size
    rng = np.random.default_rng(seed)
    if flat:
        base = 0.7 + 0.05 * cv2.GaussianBlur(rng.normal(0, 1, (h, w, 3)), (0, 0), 20)
    else:
        base = _texture(h, w, seed)
    img = np.clip(base * (1 + K[..., None]) + rng.normal(0, 0.008, base.shape), 0, 1)
    return _jpeg((img * 255).round().astype(np.uint8), 92)


def test_prnu_reference_and_match(wb, case, tmp_path):
    ka = np.random.default_rng(1).normal(0, 0.01, (256, 320))
    kb = np.random.default_rng(2).normal(0, 0.01, (256, 320))
    flats = []
    for i in range(8):
        p = tmp_path / f"flat{i}.jpg"
        p.write_bytes(_camera_shot(ka, 100 + i, flat=True))
        flats.append(ingest(wb, case, p)["id"])
    ref = wb.create_camera_reference(case["id"], "Camera A", flats, EXAMINER, camera="Test cam")
    assert ref["stats"]["images"] == 8 and ref["width"] == 320
    same = tmp_path / "same.jpg"
    same.write_bytes(_camera_shot(ka, 500))
    other = tmp_path / "other.jpg"
    other.write_bytes(_camera_shot(kb, 600))
    crop = tmp_path / "crop.jpg"
    crop.write_bytes(_jpeg(_decode(_camera_shot(ka, 700))[40:200, 30:270].copy(), 95))
    r_same = wb.run_analysis(ingest(wb, case, same)["id"], "camera.prnu", {"reference_id": ref["id"]}, EXAMINER)["result"]["findings"]
    r_other = wb.run_analysis(ingest(wb, case, other)["id"], "camera.prnu", {"reference_id": ref["id"]}, EXAMINER)["result"]["findings"]
    r_crop = wb.run_analysis(ingest(wb, case, crop)["id"], "camera.prnu", {"reference_id": ref["id"]}, EXAMINER)["result"]["findings"]
    assert r_same["consistent"] and not r_other["consistent"]
    assert r_crop["consistent"] and r_crop["crop_offset_xy"] == [30, 40]
    assert any(e["action"] == "prnu.reference" for e in wb.audit_entries(case["id"]))


# Measurement ----------------------------------------------------------------------------------


def _camera():
    f = 800
    K = np.array([[f, 0, 640], [0, f, 360], [0, 0, 1.0]])
    R1, _ = cv2.Rodrigues(np.array([np.deg2rad(-105), 0, 0.0]))
    yaw = np.deg2rad(20)
    Rz = np.array([[np.cos(yaw), -np.sin(yaw), 0], [np.sin(yaw), np.cos(yaw), 0], [0, 0, 1]])
    R = R1 @ Rz.T
    t = -R @ np.array([0, 0, 3.0])

    def proj(X):
        x = K @ (R @ np.asarray(X, float) + t)
        return [float(x[0] / x[2]), float(x[1] / x[2])]

    return proj


def test_height_by_single_view_metrology(wb, case, jpeg_path):
    proj = _camera()
    seg = lambda a, b: [proj(a), proj(b)]  # noqa: E731
    inp = {
        "vertical": seg([2, 10, 0], [2, 10, 2]) + seg([-3, 15, 0], [-3, 15, 3]),
        "horizontal_a": seg([-2, 8, 0], [-2, 25, 0]) + seg([3, 8, 0], [3, 25, 0]),
        "horizontal_b": seg([-4, 9, 0], [4, 9, 0]) + seg([-4, 18, 0], [4, 18, 0]),
        "ref_base": proj([0, 12, 0]), "ref_top": proj([0, 12, 2.0]), "ref_height": 2.0,
        "target_base": proj([-1, 16, 0]), "target_top": proj([-1, 16, 1.78]), "point_sigma": 1.0,
    }
    ev = ingest(wb, case, jpeg_path)
    m = wb.measure(ev["id"], "height", inp, EXAMINER, label="suspect")
    r = m["result"]
    assert abs(r["value"] - 1.78) < 1e-6 and r["interval_95"][0] < 1.78 < r["interval_95"][1]
    # The Monte Carlo interval is seeded: the same inputs give the same record hash.
    assert wb.measure(ev["id"], "height", inp, EXAMINER, label="suspect")["output_hash"] == m["output_hash"]


def test_speed_uses_frame_timestamps(wb, case, video_path):
    proj = _camera()
    wp = [[-3, 8], [3, 8], [3, 20], [-3, 20]]
    ev = ingest(wb, case, video_path)
    inp = {"plane_image_points": [proj([x, y, 0]) for x, y in wp], "plane_world_points": wp,
           "point_a": proj([0, 10, 0]), "point_b": proj([0, 15, 0]), "frame_a": 0, "frame_b": 10}
    r = wb.measure(ev["id"], "speed", inp, EXAMINER)["result"]
    assert abs(r["time_s"] - 0.4) < 1e-6  # 25 fps test clip
    assert abs(r["value"] - 12.5) < 1e-3 and abs(r["value_kmh"] - 45.0) < 1e-2


# DVR formats ------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def vendor_export(tmp_path_factory):
    """An H.264 stream wrapped in a made-up vendor container with DHAV-like frame headers."""
    d = tmp_path_factory.mktemp("dvr")
    es = d / "clip.h264"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25", "-t", "1",
                    "-c:v", "libx264", "-g", "10", "-bf", "0", "-pix_fmt", "yuv420p", "-f", "h264", str(es)], check=True)
    data = es.read_bytes()
    rng = np.random.default_rng(0)
    junk = lambda n: bytes(rng.integers(1, 255, n, dtype=np.uint8))  # noqa: E731
    parts = [m.start() for m in __import__("re").finditer(rb"\x00\x00\x00\x01", data)] + [len(data)]
    out = bytearray(b"VNDR" + junk(60))
    for a, b in zip(parts, parts[1:]):
        out += b"DHAV" + junk(20) + data[a:b] + b"dhav" + junk(4)
    p = d / "export.dav"
    p.write_bytes(bytes(out))
    return p


def test_sniff_and_carve(vendor_export):
    info = formats.sniff(vendor_export.read_bytes()[:65536])
    assert info["family"] == "dahua_dav" and info["elementary_codec"] == "h264"
    stream, stats = formats.carve(vendor_export.read_bytes(), "h264")
    assert stats["nal_units_trimmed"] > 0 and b"DHAV" not in stream and b"dhav" not in stream


def test_convert_vendor_export(wb, case, vendor_export, monkeypatch):
    from forensic import media

    real_probe = media.probe

    def probe(path, settings):  # stand in for a vendor format FFmpeg cannot open
        if path.suffix == ".dav":
            raise media.MediaError("Invalid data found when processing input")
        return real_probe(path, settings)

    monkeypatch.setattr(media, "probe", probe)
    ev = ingest(wb, case, vendor_export)
    assert ev["media_type"] == "container" and ev["probe"]["identified"]["family"] == "dahua_dav"
    with pytest.raises(Exception, match="converted"):
        wb.source_frame(ev["id"], 0)
    conv = wb.convert_evidence(ev["id"], "carve", EXAMINER, frame_rate=25)
    assert conv["media_type"] == "video" and conv["probe"]["frame_count"] == 25
    assert conv["derived_from"] == ev["id"] and conv["conversion"]["source_sha256"] == ev["sha256"]
    # Converting again gives the same bytes: the working copy is reproducible.
    again = wb.convert_evidence(ev["id"], "carve", EXAMINER, frame_rate=25)
    assert again["sha256"] == conv["sha256"]
    assert wb.source_frame(conv["id"], 12).shape == (120, 160, 3)
    table = wb.frame_table(conv["id"])
    assert table[10]["time"] == 0.4 and table[10]["synthetic_time"]


def test_remux_program_stream(wb, case, tmp_path):
    ps = tmp_path / "rec.mpg"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25", "-t", "1",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-f", "mpeg", str(ps)], check=True)
    ev = ingest(wb, case, ps)
    conv = wb.convert_evidence(ev["id"], "remux", EXAMINER)
    assert conv["probe"]["codec"] == "h264" and conv["probe"]["frame_count"] == 25
    assert wb.convert_evidence(ev["id"], "remux", EXAMINER)["sha256"] == conv["sha256"]
    assert conv["conversion"]["identified"]["family"] == "mpeg_ps"


# Triage and planner --------------------------------------------------------------------------------


def test_triage_case(wb, case, spliced_path, jpeg_path):
    ingest(wb, case, spliced_path)
    ingest(wb, case, jpeg_path)
    out = triage_case(wb, case["id"])
    assert sum(out["counts"].values()) == 2
    levels = {i["filename"]: i["level"] for i in out["items"]}
    assert levels["spliced.jpg"] in ("red", "yellow")
    for i in out["items"]:
        assert i["line"]
    entries = wb.audit_entries(case["id"])
    assert any(e["action"] == "triage.smart_report" and e["actor_kind"] == "agent" for e in entries)
    assert any(e["action"] == "case.note" and "[agent opinion]" in e["params"]["text"] for e in entries)
    assert wb.list_triage(case["id"])


def test_planner_proposes_two_chains(wb, case, video_path):
    ev = ingest(wb, case, video_path)
    out = plan_enhancement(wb, case["id"], ev["id"], "make the plate readable", roi=[40, 30, 80, 60],
                           frames={"start": 0, "end": 5})
    assert out["workflow"] == "plate" and len(out["proposed_chains"]) == 2
    scores = [c["score"] for c in out["candidates"] if c["score"] is not None]
    assert len(scores) >= 4
    for cid in out["proposed_chains"]:
        ch = wb.get_chain(cid)
        assert all(n.get("proposed_by", "").startswith("agent:planner-agent") and not n.get("accepted_by") for n in ch["graph"]["nodes"])
        assert wb.render_chain(cid, EXAMINER)["pending_proposals"]
