import io
import os
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from forensic.audit import Actor
from forensic.config import load_settings
from forensic.workbench import Workbench

# FastAPI's TestClient addresses the app as http://testserver.
os.environ.setdefault("FORENSIC_ALLOWED_HOSTS", "testserver")

EXAMINER = Actor("Test Examiner")


@pytest.fixture()
def wb(tmp_path):
    w = Workbench(load_settings(tmp_path / "data"))
    yield w
    w.db.close()


@pytest.fixture()
def case(wb):
    return wb.create_case("Test case", "Test Examiner", EXAMINER)


def _scene(h=240, w=320, seed=0):
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    img = np.stack([xx / w, yy / h, 0.5 + 0.3 * np.sin(xx / 9.0)], axis=2)
    img[60:120, 80:200] = [0.9, 0.9, 0.2]
    img += rng.normal(0, 0.03, img.shape)
    return np.clip(img * 255, 0, 255).astype(np.uint8)


@pytest.fixture(scope="session")
def jpeg_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("media") / "scene.jpg"
    Image.fromarray(_scene()).save(p, "JPEG", quality=85)
    return p


@pytest.fixture(scope="session")
def spliced_path(tmp_path_factory):
    """A JPEG whose centre was saved at quality 60 before the whole image was saved at 95."""
    base = _scene(seed=1)
    buf = io.BytesIO()
    Image.fromarray(base).save(buf, "JPEG", quality=60)
    low = np.asarray(Image.open(io.BytesIO(buf.getvalue())).convert("RGB"))
    out = base.copy()
    out[80:176, 96:224] = low[80:176, 96:224]
    p = tmp_path_factory.mktemp("media") / "spliced.jpg"
    Image.fromarray(out).save(p, "JPEG", quality=95)
    return p


@pytest.fixture(scope="session")
def video_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("media") / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25", "-t", "1",
         "-c:v", "libx264", "-g", "10", "-bf", "2", "-pix_fmt", "yuv420p", str(p)],
        check=True,
    )
    return p


def sign_in(client, full_name="Test Examiner"):
    """Name the built-in local examiner (there is no sign-in) and return the client."""
    client.patch("/api/me", json={"full_name": full_name}).raise_for_status()
    return client


def ingest(wb, case, path: Path):
    return wb.ingest_path(case["id"], path, EXAMINER)
