"""Authentication analyses: definitions and registry.

An analysis reads an original evidence file and returns structured findings
plus zero or more maps (images). Like filters, each has a stable id, version
and reference so the report can cite exactly what ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any, Callable

import numpy as np

from ..config import Settings
from ..filters.base import Param
from .. import media


@dataclass
class AnalysisContext:
    path: Path
    probe: dict
    settings: Settings

    @cached_property
    def data(self) -> bytes:
        return self.path.read_bytes()

    @cached_property
    def image(self) -> np.ndarray:
        if self.probe["media_type"] != "image":
            raise ValueError("this analysis needs a still image")
        return media.decode_image(self.path)


@dataclass
class AnalysisResult:
    summary: str
    findings: dict[str, Any]
    flags: list[dict] = field(default_factory=list)  # {"level": "info"|"warning", "text": ...}
    maps: dict[str, np.ndarray] = field(default_factory=dict)  # name -> float RGB image


@dataclass(frozen=True)
class Analysis:
    id: str
    name: str
    version: str
    category: str
    description: str
    reference: str
    applies_to: tuple[str, ...]
    fn: Callable[[AnalysisContext, dict], AnalysisResult]
    params: tuple[Param, ...] = ()
    reading_guide: str = ""

    def schema(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "category": self.category,
            "description": self.description,
            "reference": self.reference,
            "applies_to": list(self.applies_to),
            "reading_guide": self.reading_guide,
            "params": [p.schema() for p in self.params],
        }

    def validate(self, params: dict | None) -> dict:
        params = dict(params or {})
        unknown = set(params) - {p.name for p in self.params}
        if unknown:
            raise ValueError(f"{self.id}: unknown parameter(s) {', '.join(sorted(unknown))}")
        return {p.name: p.coerce(params.get(p.name)) for p in self.params}


ANALYSES: dict[str, Analysis] = {}


def register(id: str, **kw):
    def deco(fn):
        ANALYSES[id] = Analysis(id=id, fn=fn, params=tuple(kw.pop("params", ())), **kw)
        return fn

    return deco


def get_analysis(analysis_id: str) -> Analysis:
    try:
        return ANALYSES[analysis_id]
    except KeyError:
        raise ValueError(f"unknown analysis '{analysis_id}'") from None
