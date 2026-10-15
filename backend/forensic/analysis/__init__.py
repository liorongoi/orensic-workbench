"""Importing this package registers every built-in analysis."""

from . import (  # noqa: F401
    camera, clone, compression, dimples, double_jpeg, faces, fusion, generated, geometry, gradient, metadata, noise, qtdb,
    synthetic, video,
)
from .base import ANALYSES, Analysis, AnalysisContext, AnalysisResult, get_analysis

__all__ = ["ANALYSES", "Analysis", "AnalysisContext", "AnalysisResult", "get_analysis"]
