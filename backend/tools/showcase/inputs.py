"""Test inputs for the showcase report.

Real photographs come from the OpenCV sample data (BSD/Apache-licensed; see
https://github.com/opencv/opencv/tree/4.x/samples/data). Degraded versions are made here with
known, stated degradations, so a restoration can be measured against the clean original
(ground truth). Every degradation is listed in the report next to its result.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import cv2
import numpy as np

RNG = np.random.default_rng(2026)


def _save(img_bgr: np.ndarray, path: Path, quality: int | None = None) -> Path:
    if quality:
        cv2.imwrite(str(path), img_bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    else:
        cv2.imwrite(str(path), img_bgr)
    return path


def _video(frames: list[np.ndarray], path: Path, fps: float = 10, codec: str = "ffv1", audio: str | None = None) -> Path:
    h, w = frames[0].shape[:2]
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{w}x{h}", "-r", f"{fps}", "-i", "-"]
    if audio:
        cmd += ["-f", "lavfi", "-i", audio, "-shortest"]
    cmd += {"ffv1": ["-c:v", "ffv1"], "h264": ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18"]}[codec]
    if audio:
        cmd += ["-c:a", "pcm_s16le" if path.suffix == ".mkv" else "aac"]
    cmd.append(str(path))
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for f in frames:
        p.stdin.write(np.ascontiguousarray(f).tobytes())
    p.stdin.close()
    if p.wait() != 0:
        raise RuntimeError(f"ffmpeg failed for {path}")
    return path


def motion_psf(length: float, angle: float, size: int = 41) -> np.ndarray:
    k = np.zeros((size, size), np.float32)
    c = size // 2
    a = np.deg2rad(angle)
    for t in np.linspace(-length / 2, length / 2, int(length * 4) + 1):
        x, y = c + t * np.cos(a), c - t * np.sin(a)
        k[int(round(y)), int(round(x))] += 1
    return k / k.sum()


def text_card(w=520, h=300) -> np.ndarray:
    img = np.full((h, w, 3), 235, np.uint8)
    lines = ["FORENSIC VIDEO 2026", "Plate 12-345-67  Cam 4", "Time 10:15:02  Exit B", "abcdefghijklmnopqrstuvwxyz"]
    for i, t in enumerate(lines):
        cv2.putText(img, t, (14, 60 + i * 64), cv2.FONT_HERSHEY_SIMPLEX, 1.0 if i < 3 else 0.8, (25, 25, 25), 2, cv2.LINE_AA)
    return img


def make_all(media: Path, out: Path) -> dict[str, dict]:
    """Return {key: {"path", "description", "truth" (optional path)}}."""
    out.mkdir(parents=True, exist_ok=True)
    items: dict[str, dict] = {}

    def add(key, path, desc, truth=None):
        items[key] = {"path": str(path), "description": desc, "truth": str(truth) if truth else None}

    building = cv2.imread(str(media / "building.jpg"))
    building = cv2.resize(building, (520, 360), interpolation=cv2.INTER_AREA)
    clean = _save(building, out / "building_clean.png")
    add("building", clean, "OpenCV sample 'building.jpg', resized to 520×360 (reference photograph).")
    lin = (building.astype(np.float32) / 255) ** 2.2
    dark = np.clip(((lin * 0.2) ** (1 / 2.2)) * 255, 0, 255).astype(np.uint8)
    add("dark", _save(dark, out / "building_dark.png"),
        "building under-exposed by 2.32 stops: linear light × 0.2 (gamma 2.2 model), then 8-bit.", clean)
    flat = np.clip(building.astype(np.float32) * 0.35 + 95, 0, 255).astype(np.uint8)
    add("flat", _save(flat, out / "building_flat.png"), "building contrast reduced to 35 % around mid-grey (hazy, flat image).", clean)
    cast = np.clip(building.astype(np.float32) * np.array([0.62, 0.86, 1.0]), 0, 255).astype(np.uint8)
    add("cast", _save(cast, out / "building_cast.png"), "building with a warm colour cast (blue × 0.62, green × 0.86).", clean)
    noisy = np.clip(building.astype(np.float32) + RNG.normal(0, 22, building.shape), 0, 255).astype(np.uint8)
    add("noisy", _save(noisy, out / "building_noisy.png"), "building + Gaussian noise, σ = 22 grey levels.", clean)
    sp = building.copy()
    m = RNG.random(building.shape[:2])
    sp[m < 0.04] = 0
    sp[m > 0.96] = 255
    add("saltpepper", _save(sp, out / "building_saltpepper.png"), "building with 8 % salt-and-pepper (impulse) noise.", clean)
    yy, xx = np.mgrid[0:360, 0:520]
    pattern = 28 * np.sin(2 * np.pi * (xx / 7.0 + yy / 23.0))
    periodic = np.clip(building.astype(np.float32) + pattern[..., None], 0, 255).astype(np.uint8)
    add("periodic", _save(periodic, out / "building_periodic.png"), "building + sinusoidal interference pattern (amplitude 28).", clean)
    add("blocky", _save(building, out / "building_q8.jpg", quality=8), "building saved as JPEG at quality 8 (heavy 8×8 blocking).", clean)
    half = cv2.resize(building, (260, 180), interpolation=cv2.INTER_AREA)
    add("small", _save(half, out / "building_half.png"), "building downscaled to 260×180 (for upscaling).", clean)

    card = text_card()
    card_path = _save(card, out / "text_clean.png")
    add("text", card_path, "Rendered text card (ground truth for deblurring).")
    psf = motion_psf(15, 30)
    mb = cv2.filter2D(card.astype(np.float32), -1, psf, borderType=cv2.BORDER_REFLECT)
    mb = np.clip(mb + RNG.normal(0, 1.0, mb.shape), 0, 255).astype(np.uint8)
    add("motion", _save(mb, out / "text_motion15_30.png"), "Text card blurred by straight motion, length 15 px at 30°, plus noise σ = 1.", card_path)
    disk = np.zeros((21, 21), np.float32)
    cv2.circle(disk, (10, 10), 5, 1, -1)
    df = cv2.filter2D(card.astype(np.float32), -1, disk / disk.sum(), borderType=cv2.BORDER_REFLECT)
    df = np.clip(df + RNG.normal(0, 1.0, df.shape), 0, 255).astype(np.uint8)
    add("defocus", _save(df, out / "text_defocus_r5.png"), "Text card out of focus (uniform disk, radius 5 px), plus noise σ = 1.", card_path)
    gb = np.clip(cv2.GaussianBlur(card.astype(np.float32), (0, 0), 2.0) + RNG.normal(0, 1.0, card.shape), 0, 255).astype(np.uint8)
    add("gauss_blur", _save(gb, out / "text_gauss2.png"), "Text card with Gaussian blur σ = 2 px, plus noise σ = 1.", card_path)
    add("real_motion", media / "text_motion.jpg", "OpenCV sample 'text_motion.jpg' (real motion-blurred photograph, unknown blur).")
    add("plate_motion", media / "licenseplate_motion.jpg", "OpenCV sample 'licenseplate_motion.jpg' (real motion blur).")

    # Lens geometry: a chessboard pushed through an equidistant fisheye projection (focal 0.35·w).
    W, H = 640, 480
    board = np.full((H * 3, W * 3, 3), 255, np.uint8)
    s = 48
    for y in range(0, H * 3, s):
        for x in range(0, W * 3, s):
            if (x // s + y // s) % 2 == 0:
                board[y:y + s, x:x + s] = 30
    f = 0.35 * W
    yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
    dx, dy = xx - W / 2, yy - H / 2
    rd = np.hypot(dx, dy)
    theta = rd / f
    ru = np.where(theta < 1.45, f * np.tan(np.minimum(theta, 1.45)), np.nan)
    scale = np.where(rd > 0, ru / np.maximum(rd, 1e-6), 1)
    mx, my = (dx * scale + W * 1.5).astype(np.float32), (dy * scale + H * 1.5).astype(np.float32)
    fish = cv2.remap(board, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))
    add("fisheye", _save(fish, out / "chess_fisheye.png"), "Chessboard through an equidistant fisheye projection, focal = 0.35 × width.")
    k1 = -0.25
    xn, yn = dx / W, dy / W
    r2 = xn * xn + yn * yn
    barrel = cv2.remap(board[H:2 * H, W:2 * W], (W / 2 + dx * (1 + 0.25 * r2 * 4)).astype(np.float32),
                       (H / 2 + dy * (1 + 0.25 * r2 * 4)).astype(np.float32), cv2.INTER_LINEAR)
    add("barrel", _save(barrel, out / "chess_barrel.png"), "Chessboard with barrel (radial) distortion.")
    # Equirectangular "360°" test pattern: longitude/latitude grid with labels.
    eq = np.full((512, 1024, 3), 245, np.uint8)
    for lon in range(0, 1024, 64):
        cv2.line(eq, (lon, 0), (lon, 511), (60, 60, 200), 2)
    for lat in range(0, 512, 64):
        cv2.line(eq, (0, lat), (1023, lat), (200, 80, 40), 2)
    for i, name in enumerate(["N", "E", "S", "W"]):
        cv2.putText(eq, name, (i * 256 + 100, 270), cv2.FONT_HERSHEY_SIMPLEX, 2.5, (20, 20, 20), 5)
    add("equirect", _save(eq, out / "equirect_grid.png"), "Synthetic 360° equirectangular grid (meridians red, parallels blue).")
    car = cv2.imread(str(media / "car_crop.png"))
    add("car", _save(car, out / "car_crop.png"), "OpenCV sample 'car_crop.png' (real UK car, plate about 60 px wide).")
    add("plate_car", media / "synth_plate_car.jpg", "Car photograph with a synthetic Israeli-format plate '12-345-67' (known ground truth).")
    messi = cv2.imread(str(media / "messi5.jpg"))
    add("people", _save(messi, out / "messi5.png"), "OpenCV sample 'messi5.jpg' (people, small faces).")
    fruits = cv2.imread(str(media / "fruits.jpg"))
    add("fruits", _save(fruits, out / "fruits.png"), "OpenCV sample 'fruits.jpg' (strong colours).")

    # Interlaced frame: even lines from one moment, odd lines from 6 px later.
    street = cv2.imread(str(media / "street.png"))
    a, b = street[:, :500], street[:, 6:506]
    inter = a.copy()
    inter[1::2] = b[1::2]
    add("interlaced", _save(inter, out / "street_interlaced.png"), "Street photo with combing: odd lines taken 6 px of motion later (interlaced).")

    # Authentication inputs.
    home = cv2.imread(str(media / "home.jpg"))
    spl = home.copy()
    spl[150:270, 300:440] = cv2.resize(building[40:200, 60:250], (140, 120))
    spl[150:270, 300:440] = cv2.imdecode(cv2.imencode(".jpg", spl, [cv2.IMWRITE_JPEG_QUALITY, 55])[1], cv2.IMREAD_COLOR)[150:270, 300:440]
    add("spliced", _save(spl, out / "home_spliced.jpg", quality=92),
        "OpenCV 'home.jpg' with a pasted region (x 300–440, y 150–270) that was JPEG-compressed at Q55 before the whole image was saved at Q92.")
    add("authentic", _save(home, out / "home_q92.jpg", quality=92), "OpenCV 'home.jpg' saved once at JPEG Q92 (no edit).")
    cm = home.copy()
    cm[40:110, 60:150] = home[40:110, 330:420]
    add("copymove", _save(cm, out / "home_copymove.jpg", quality=95),
        "home.jpg with a 90×70 region copied from x 330 to x 60 (copy-move forgery), saved at Q95.")

    # Video: static camera, a "vehicle" (car crop) crossing, sensor noise; truth = background.
    bg = cv2.resize(building, (480, 320))
    carsm = cv2.resize(car, (85, 106))
    frames = []
    for i in range(24):
        f_ = bg.copy()
        x = 20 + i * 15
        f_[180:286, x:x + 85] = carsm
        f_ = np.clip(f_.astype(np.float32) + RNG.normal(0, 6, f_.shape), 0, 255).astype(np.uint8)
        frames.append(f_)
    add("moving", _video(frames, out / "static_cam_moving_car.mkv"), "24 frames, static camera, car cut-out crossing left to right, noise σ = 6 (FFV1, 10 fps).")
    # Shaky noisy burst of a static scene (for stabilisation, integration, super-resolution, temporal denoise).
    shaky = []
    tight = cv2.resize(building, (520, 360))
    for i in range(10):
        dx_, dy_ = RNG.uniform(-4, 4), RNG.uniform(-3, 3)
        M = np.float32([[1, 0, dx_], [0, 1, dy_]])
        fr = cv2.warpAffine(tight, M, (520, 360), borderMode=cv2.BORDER_REFLECT)
        fr = cv2.resize(fr, (260, 180), interpolation=cv2.INTER_AREA)
        fr = np.clip(fr.astype(np.float32) + RNG.normal(0, 12, fr.shape), 0, 255).astype(np.uint8)
        shaky.append(fr)
    add("shaky", _video(shaky, out / "shaky_burst.mkv"), "10 frames of the building, 260×180, random shifts up to ±4 px, noise σ = 12 (FFV1).",
        _save(cv2.resize(tight, (260, 180), interpolation=cv2.INTER_AREA), out / "shaky_truth.png"))
    # Heat-haze style turbulence: smooth random warps per frame.
    turb = []
    for i in range(10):
        fx = cv2.GaussianBlur(RNG.normal(0, 1, (180, 260)).astype(np.float32), (0, 0), 6) * 25
        fy = cv2.GaussianBlur(RNG.normal(0, 1, (180, 260)).astype(np.float32), (0, 0), 6) * 25
        base = cv2.resize(tight, (260, 180), interpolation=cv2.INTER_AREA)
        yy2, xx2 = np.mgrid[0:180, 0:260].astype(np.float32)
        turb.append(cv2.remap(base, xx2 + fx, yy2 + fy, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT))
    add("turbulence", _video(turb, out / "turbulence.mkv"), "10 frames of the building with smooth random warps (simulated heat haze).",
        out / "shaky_truth.png")
    # Street pan with audio (detection, tracking, redaction).
    W2, H2 = 432, 492
    pan = [street[10:10 + H2, int(i * 80 / 30):int(i * 80 / 30) + W2] for i in range(30)]
    add("street", _video(pan, out / "street_pan.mp4", fps=10, codec="h264", audio="sine=frequency=700:sample_rate=16000"),
        "30 frames panning across OpenCV 'street.png' (real people and cars), H.264 with a tone audio track, 10 fps.")
    return items
