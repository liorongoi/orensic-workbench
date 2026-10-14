"""Importing this package registers every built-in analysis."""

from . import camera, compression, double_jpeg, faces, metadata, synthetic, video  # noqa: F401
from .base import ANALYSES, Analysis, AnalysisContext, AnalysisResult, get_analysis

__all__ = ["ANALYSES", "Analysis", "AnalysisContext", "AnalysisResult", "get_analysis"]
