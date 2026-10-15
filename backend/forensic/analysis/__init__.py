"""Importing this package registers every built-in analysis."""

from . import camera, clone, compression, double_jpeg, faces, generated, geometry, metadata, noise, synthetic, video  # noqa: F401
from .base import ANALYSES, Analysis, AnalysisContext, AnalysisResult, get_analysis

__all__ = ["ANALYSES", "Analysis", "AnalysisContext", "AnalysisResult", "get_analysis"]
