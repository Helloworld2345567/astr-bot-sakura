"""AstrBot entry point. Implementation lives in the sakura package."""
import sys
from typing import Any

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest

from .sakura.message_flow import IncomingControlFilter
from .sakura.runtime import SakuraRuntime


class Main(SakuraRuntime, star.Star):
    # AstrBot registers handlers by their defining module, so these entry
    # methods stay here while the runtime itself is framework-independent
    # with respect to plugin registration.
    @filter.on_llm_request(priority=sys.maxsize)
    async def enforce_sakura_permissions(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        await super().enforce_sakura_permissions(event, req)

    @filter.on_using_llm_tool(priority=sys.maxsize)
    async def enforce_sakura_tool_permissions(
        self, event: AstrMessageEvent, tool: Any, tool_args: dict[str, Any] | None
    ) -> None:
        await super().enforce_sakura_tool_permissions(event, tool, tool_args)

    @filter.custom_filter(IncomingControlFilter, priority=sys.maxsize)
    async def select_sakura_before_pipeline(self, event: AstrMessageEvent) -> None:
        await super().select_sakura_before_pipeline(event)

    @filter.on_llm_request()
    async def select_sakura_flow(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        await super().select_sakura_flow(event, req)

    @filter.after_message_sent()
    async def send_scene_illustration(self, event: AstrMessageEvent) -> None:
        await super().send_scene_illustration(event)

    async def terminate(self) -> None:
        # AstrBot checks this class's __dict__ when unloading a plugin.
        await super().terminate()
