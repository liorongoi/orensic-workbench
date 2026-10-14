"""Runtime configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    ffmpeg: str
    ffprobe: str

    @property
    def originals_dir(self) -> Path:
        return self.data_dir / "originals"

    @property
    def derived_dir(self) -> Path:
        return self.data_dir / "derived"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "forensic.sqlite3"


def load_settings(data_dir: str | os.PathLike | None = None) -> Settings:
    root = Path(data_dir or os.environ.get("FORENSIC_DATA_DIR", "./data")).resolve()
    s = Settings(
        data_dir=root,
        ffmpeg=os.environ.get("FORENSIC_FFMPEG", "ffmpeg"),
        ffprobe=os.environ.get("FORENSIC_FFPROBE", "ffprobe"),
    )
    s.originals_dir.mkdir(parents=True, exist_ok=True)
    s.derived_dir.mkdir(parents=True, exist_ok=True)
    return s
