"""Importing this package registers every built-in analysis."""

from . import camera, clone, compression, dimples, double_jpeg, faces, fusion, gradient, metadata, noise, qtdb, synthetic, video  # noqa: F401
from .base import ANALYSES, Analysis, AnalysisContext, AnalysisResult, get_analysis

__all__ = ["ANALYSES", "Analysis", "AnalysisContext", "AnalysisResult", "get_analysis"]
