"""Filter definitions and the registry.

A filter is a pure function of its input pixels and parameters. It carries a
stable id, a semantic version, a parameter schema, a plain-language
description and a scientific reference, all of which are printed in the report.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np


class FilterError(ValueError):
    pass


@dataclass(frozen=True)
class Param:
    name: str
    type: str  # float | int | bool | choice | points | text
    default: Any
    label: str = ""
    min: float | None = None
    max: float | None = None
    step: float | None = None
    choices: tuple[str, ...] = ()
    help: str = ""
    count: int | None = None  # for points: exact number of points required

    def schema(self) -> dict:
        d = {
            "name": self.name,
            "type": self.type,
            "default": self.default,
            "label": self.label or self.name.replace("_", " ").capitalize(),
            "help": self.help,
        }
        for k in ("min", "max", "step", "count"):
            if getattr(self, k) is not None:
                d[k] = getattr(self, k)
        if self.choices:
            d["choices"] = list(self.choices)
        return d

    def coerce(self, value: Any) -> Any:
        if value is None:
            value = self.default
        try:
            if self.type == "float":
                v = float(value)
                if not math.isfinite(v):
                    raise ValueError
            elif self.type == "int":
                v = int(value)
                if float(value) != v:
                    raise ValueError
            elif self.type == "bool":
                if isinstance(value, str):
                    v = value.lower() in ("1", "true", "yes", "on")
                else:
                    v = bool(value)
            elif self.type == "choice":
                v = str(value)
                if v not in self.choices:
                    raise FilterError(f"{self.name}: '{v}' is not one of {', '.join(self.choices)}")
            elif self.type == "points":
                v = [[float(x), float(y)] for x, y in value]
                if self.count is not None and len(v) != self.count:
                    raise FilterError(f"{self.name}: needs exactly {self.count} points")
            elif self.type == "text":
                v = str(value)
            else:
                raise FilterError(f"unknown parameter type {self.type}")
        except FilterError:
            raise
        except (TypeError, ValueError) as e:
            raise FilterError(f"{self.name}: invalid value {value!r}") from e
        if self.type in ("float", "int"):
            if self.min is not None and v < self.min:
                raise FilterError(f"{self.name}: {v} is below the minimum {self.min}")
            if self.max is not None and v > self.max:
                raise FilterError(f"{self.name}: {v} is above the maximum {self.max}")
        return v


@dataclass(frozen=True)
class Filter:
    id: str
    name: str
    version: str
    category: str
    description: str
    reference: str
    fn: Callable[..., np.ndarray]
    params: tuple[Param, ...] = ()
    input_kind: str = "image"  # "image": applied per frame; "stack": consumes (N,H,W,3)
    output_kind: str = "image"  # for stack filters: "image" or "stack"
    generative: bool = False  # can invent detail; excluded from evidential export
    ai: bool = False  # runs a learned model (printed as AI-assisted in the report)
    # For AI filters: the model ids a given parameter set runs (their weights hashes go in the report).
    models: Callable[[dict], list[str]] | None = None

    def model_ids(self, params: dict) -> list[str]:
        return list(self.models(params)) if self.models else []

    def schema(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "category": self.category,
            "description": self.description,
            "reference": self.reference,
            "input_kind": self.input_kind,
            "output_kind": self.output_kind if self.input_kind == "stack" else self.input_kind,
            "generative": self.generative,
            "ai": self.ai,
            "params": [p.schema() for p in self.params],
        }

    def validate(self, params: dict[str, Any] | None) -> dict[str, Any]:
        params = dict(params or {})
        known = {p.name for p in self.params}
        unknown = set(params) - known
        if unknown:
            raise FilterError(f"{self.id}: unknown parameter(s) {', '.join(sorted(unknown))}")
        return {p.name: p.coerce(params.get(p.name)) for p in self.params}


REGISTRY: dict[str, Filter] = {}


def register(
    id: str,
    *,
    name: str,
    version: str,
    category: str,
    description: str,
    reference: str,
    params: list[Param] | tuple[Param, ...] = (),
    input_kind: str = "image",
    output_kind: str = "image",
    generative: bool = False,
    ai: bool = False,
    models: Callable[[dict], list[str]] | None = None,
):
    def deco(fn):
        if id in REGISTRY:
            raise RuntimeError(f"duplicate filter id {id}")
        REGISTRY[id] = Filter(
            id=id,
            name=name,
            version=version,
            category=category,
            description=description,
            reference=reference,
            fn=fn,
            params=tuple(params),
            input_kind=input_kind,
            output_kind=output_kind,
            generative=generative,
            ai=ai,
            models=models,
        )
        return fn

    return deco


def get_filter(filter_id: str) -> Filter:
    try:
        return REGISTRY[filter_id]
    except KeyError:
        raise FilterError(f"unknown filter '{filter_id}'") from None


# Shared helpers used by filter implementations --------------------------------


def clip01(img: np.ndarray) -> np.ndarray:
    return np.clip(img, 0.0, 1.0, out=img if img.flags.writeable else None).astype(np.float32, copy=False)


def u8(img: np.ndarray) -> np.ndarray:
    return np.clip(np.rint(img * 255.0), 0, 255).astype(np.uint8)


def from_u8(img: np.ndarray) -> np.ndarray:
    return img.astype(np.float32) / 255.0


def luminance(img: np.ndarray) -> np.ndarray:
    """Rec. 601 luma, the weighting most CCTV and JPEG pipelines use."""
    return (0.299 * img[..., 0] + 0.587 * img[..., 1] + 0.114 * img[..., 2]).astype(np.float32)
