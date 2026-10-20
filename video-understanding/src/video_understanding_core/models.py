"""Public contracts. Secrets and host paths never appear in job snapshots."""
from dataclasses import asdict, dataclass
import math


class VideoError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CallerContext:
    bot: str
    user: str
    session: str

    @property
    def scope(self) -> tuple[str, str, str]:
        if not all((self.bot, self.user, self.session)):
            raise VideoError("INVALID_IDENTITY")
        return self.bot, self.user, self.session


@dataclass(frozen=True)
class AnalysisRequest:
    source_ref: str
    question: str
    start_seconds: float | None = None
    end_seconds: float | None = None
    message_id: str = ""

    def validate(self):
        if not isinstance(self.source_ref, str) or not 1 <= len(self.source_ref) <= 2048:
            raise VideoError("UNSUPPORTED_SOURCE")
        if not isinstance(self.question, str) or not self.question.strip() or len(self.question) > 4000:
            raise VideoError("INVALID_QUESTION")
        if (self.start_seconds is None) != (self.end_seconds is None):
            raise VideoError("INVALID_RANGE")
        if self.start_seconds is not None:
            if not all(type(x) in (int, float) and math.isfinite(x) for x in (self.start_seconds, self.end_seconds)):
                raise VideoError("INVALID_RANGE")
            if not 0 <= self.start_seconds < self.end_seconds:
                raise VideoError("INVALID_RANGE")


@dataclass(frozen=True)
class Limits:
    max_download_bytes: int = 50 * 1024**2
    max_inline_bytes: int = 8 * 1024**2
    max_request_bytes: int = 12 * 1024**2
    max_storage_bytes: int = 512 * 1024**2
    max_analysis_seconds: float = 300
    queue_capacity: int = 10
    global_concurrency: int = 1
    daily_jobs_per_user: int = 10
    daily_seconds_per_user: float = 1800
    daily_total_seconds: float = 18000
    daily_total_jobs: int = 100
    resolve_timeout: float = 20
    download_timeout: float = 90
    process_timeout: float = 90
    model_timeout: float = 120
    job_timeout: float = 360
    media_ttl: float = 3600
    result_ttl: float = 86400

    def __post_init__(self):
        if any(not math.isfinite(v) or v <= 0 for v in asdict(self).values()):
            raise ValueError("limits must be positive and finite")
        if self.max_inline_bytes > self.max_download_bytes:
            raise ValueError("inline limit exceeds download limit")
        for name in ("queue_capacity", "global_concurrency", "daily_jobs_per_user", "daily_total_jobs"):
            if type(getattr(self, name)) is not int:
                raise ValueError("counts must be integers")


@dataclass(frozen=True)
class MediaProbe:
    duration_seconds: float
    has_video: bool
    has_audio: bool
    size_bytes: int
    video_codec: str = ""
    audio_codec: str = ""
    container: str = ""


@dataclass(frozen=True)
class PreparedMedia:
    path: object  # Internal only; never serialized to the model or caller.
    probe: MediaProbe
    original_duration: float
    start: float
    end: float
    warnings: tuple[str, ...] = ()
