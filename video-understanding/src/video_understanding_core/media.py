"""Probe actual streams and preserve audio while normalizing a bounded clip."""
import json
import math
import shutil
from pathlib import Path

from .models import Limits, MediaProbe, PreparedMedia, VideoError
from .process import run_process

DEMUXERS = "mov,matroska,webm,avi,mpegts,flv,ogg,mpeg"


class MediaProcessor:
    def __init__(self, limits: Limits, ffmpeg="ffmpeg", ffprobe="ffprobe"):
        self.limits, self.ffmpeg, self.ffprobe = limits, ffmpeg, ffprobe

    def check_dependencies(self):
        if not shutil.which(self.ffmpeg) or not shutil.which(self.ffprobe):
            raise VideoError("FFMPEG_UNAVAILABLE")

    async def probe(self, path, *, require_video=True):
        self.check_dependencies()
        try:
            raw = await run_process([self.ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", DEMUXERS,
                                     "-show_format", "-show_streams", "-of", "json", path],
                                    self.limits.process_timeout)
        except VideoError as error:
            if error.code == "MEDIA_PROCESS_FAILED":
                raise VideoError("NOT_A_VIDEO") from None
            raise
        try:
            data = json.loads(raw)
            video = next((s for s in data["streams"] if s["codec_type"] == "video"
                          and not s.get("disposition", {}).get("attached_pic")), None)
            audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
            duration = float(data["format"]["duration"])
            if not math.isfinite(duration) or duration <= 0 or (video is None and (require_video or audio is None)):
                raise ValueError()
            return MediaProbe(duration, video is not None, audio is not None, Path(path).stat().st_size,
                              (video or {}).get("codec_name", ""), (audio or {}).get("codec_name", ""),
                              data["format"].get("format_name", ""))
        except (KeyError, TypeError, ValueError, StopIteration):
            raise VideoError("NOT_A_VIDEO") from None

    async def merge(self, video, audio, directory):
        source_video = await self.probe(video)
        if not (await self.probe(audio, require_video=False)).has_audio:
            raise VideoError("AUDIO_LOST")
        target = directory / "merged.mp4"
        await run_process([self.ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", DEMUXERS,
                           "-i", video, "-protocol_whitelist", "file,pipe", "-format_whitelist", DEMUXERS, "-i", audio,
                           "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-fs",
                           self.limits.max_download_bytes, target], self.limits.process_timeout)
        merged = await self.probe(target)
        if not merged.has_audio:
            raise VideoError("AUDIO_LOST")
        if merged.size_bytes > self.limits.max_download_bytes:
            raise VideoError("MEDIA_TOO_LARGE")
        if abs(merged.duration_seconds - source_video.duration_seconds) > max(.25, source_video.duration_seconds * .01):
            raise VideoError("MEDIA_TRUNCATED")
        return target

    async def prepare(self, path, directory, request):
        request.validate()
        probe = await self.probe(path)
        start = request.start_seconds if request.start_seconds is not None else 0.0
        end = request.end_seconds if request.end_seconds is not None else probe.duration_seconds
        if end > probe.duration_seconds + 0.05:
            raise VideoError("INVALID_RANGE")
        end = min(end, probe.duration_seconds)
        duration = end - start
        if duration > self.limits.max_analysis_seconds:
            raise VideoError("RANGE_REQUIRED")
        untouched = (start == 0 and end == probe.duration_seconds and probe.video_codec == "h264"
                     and (not probe.has_audio or probe.audio_codec == "aac")
                     and "mp4" in probe.container and probe.size_bytes <= self.limits.max_inline_bytes)
        if untouched:
            return PreparedMedia(path, probe, probe.duration_seconds, start, end)
        target = directory / "normalized.mp4"
        # Leave mux overhead headroom. Never silently remove audio to reach the budget.
        rate = int(self.limits.max_inline_bytes * 8 * 0.90 / duration)
        audio_rate = 96000 if probe.has_audio else 0
        if rate - audio_rate < 64000:
            raise VideoError("MEDIA_TOO_LARGE")
        command = [self.ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe", "-format_whitelist", DEMUXERS,
                   "-ss", start, "-i", path, "-t", duration, "-map", "0:v:0", "-map", "0:a:0?",
                   "-vf", "scale=w='min(1280,iw)':h=-2:force_original_aspect_ratio=decrease,pad=ceil(iw/2)*2:ceil(ih/2)*2",
                   "-c:v", "libx264", "-preset", "fast", "-b:v", rate - audio_rate,
                   "-maxrate", rate - audio_rate, "-bufsize", (rate - audio_rate) * 2,
                   "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-ac", "2",
                   "-movflags", "+faststart", "-fs", self.limits.max_inline_bytes + 65536, target]
        await run_process(command, self.limits.process_timeout)
        normalized = await self.probe(target)
        if probe.has_audio and not normalized.has_audio:
            raise VideoError("AUDIO_LOST")
        if normalized.size_bytes > self.limits.max_inline_bytes:
            raise VideoError("MEDIA_TOO_LARGE")
        if abs(normalized.duration_seconds - duration) > max(0.25, duration * .01):
            raise VideoError("MEDIA_TRUNCATED")
        return PreparedMedia(target, normalized, probe.duration_seconds, start, end,
                             ("媒体已转码；时间点为近似定位，画面分辨率可能调整。",))
