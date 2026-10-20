"""The only AstrBot registration module in this independent plugin."""
from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest

from .runtime import VideoInputFilter, VideoRuntime


class Main(VideoRuntime, star.Star):
    @filter.custom_filter(VideoInputFilter, priority=100)
    async def remember_video_input(self, event: AstrMessageEvent):
        await self.remember_video(event)

    @filter.on_llm_request()
    async def video_inputs(self, event: AstrMessageEvent, req: ProviderRequest):
        await self.prepare_request(event, req)

    @filter.llm_tool(name="analyze_video")
    async def analyze_video(self, event: AstrMessageEvent, question: str, source_ref: str = "latest",
                            start_seconds: float | None = None, end_seconds: float | None = None) -> str:
        '''Analyze the current, quoted, or recent video directly; no extra user authorization needed.

        Args:
            source_ref(string): Use latest for the current/quoted/recent video; or a video reference/platform URL.
            question(string): User question about the video.
            start_seconds(number): Optional original-video clip start; supply together with end.
            end_seconds(number): Optional original-video clip end; supply together with start.
        '''
        return await self.analyze(event, source_ref, question, start_seconds, end_seconds)

    @filter.llm_tool(name="get_video_analysis")
    async def get_video_analysis(self, event: AstrMessageEvent, job_id: str) -> str:
        '''Read this user's video job; queued work does not imply completion.

        Args:
            job_id(string): Opaque job ID returned by analyze_video.
        '''
        return await self.operation(event, job_id, "get")

    @filter.llm_tool(name="cancel_video_analysis")
    async def cancel_video_analysis(self, event: AstrMessageEvent, job_id: str) -> str:
        '''Stop local work. A provider request already received may still be billed.

        Args:
            job_id(string): Opaque job ID returned by analyze_video.
        '''
        return await self.operation(event, job_id, "cancel")

    @filter.llm_tool(name="delete_video_analysis")
    async def delete_video_analysis(self, event: AstrMessageEvent, job_id: str) -> str:
        '''Delete local video and result data. Provider retention is separate.

        Args:
            job_id(string): Opaque job ID returned by analyze_video.
        '''
        return await self.operation(event, job_id, "delete")

    async def terminate(self):
        await super().terminate()
