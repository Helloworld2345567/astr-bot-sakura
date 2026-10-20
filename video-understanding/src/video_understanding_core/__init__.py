"""Framework-independent video understanding. No AstrBot imports."""
from .models import AnalysisRequest, CallerContext, Limits, VideoError
from .service import VideoService

__all__ = ["VideoService", "AnalysisRequest", "CallerContext", "Limits", "VideoError"]
