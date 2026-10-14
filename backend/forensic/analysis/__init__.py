"""Importing this package registers every built-in analysis."""

from . import compression, metadata  # noqa: F401
from .base import ANALYSES, Analysis, AnalysisContext, AnalysisResult, get_analysis

__all__ = ["ANALYSES", "Analysis", "AnalysisContext", "AnalysisResult", "get_analysis"]
