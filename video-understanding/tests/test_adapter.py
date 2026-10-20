import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from support import ROOT, TEMP_ROOT


def load_build_script():
    spec = importlib.util.spec_from_file_location("video_build_under_test", ROOT / "scripts" / "build_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event(*, user="user-a", bot="bot-a", session="umo-a", text="总结这个视频", components=(), mid="msg-a"):
    extras = {}
    return SimpleNamespace(
        unified_msg_origin=session, role="member", is_at_or_wake_command=False,
        get_sender_id=lambda: user, get_platform_id=lambda: bot, get_message_str=lambda: text,
        get_messages=lambda: list(components), is_private_chat=lambda: True,
        message_obj=SimpleNamespace(message_id=mid),
        get_extra=lambda key, default=None: extras.get(key, default),
        set_extra=lambda key, value: extras.__setitem__(key, value),
    )


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="video-clean-install-", dir=TEMP_ROOT)
        cls.root = Path(cls.temp.name)
        archive = load_build_script().build(cls.root / "plugin.zip")
        cls.install = cls.root / "installed"
        cls.install.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(cls.install)
        cls.name = "video_plugin_under_test"
        spec = importlib.util.spec_from_file_location(cls.name, cls.install / "__init__.py",
                                                    submodule_search_locations=[str(cls.install)])
        package = importlib.util.module_from_spec(spec)
        sys.modules[cls.name] = package
        spec.loader.exec_module(package)
        cls.entry = __import__(cls.name + ".main", fromlist=["Main"])
        cls.runtime = __import__(cls.name + ".runtime", fromlist=["VideoRuntime"])

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    async def asyncSetUp(self):
        self.directory = Path(tempfile.mkdtemp(dir=self.root))
        self.asset = self.directory / "attachment.media"
        self.asset.write_bytes(b"synthetic attachment - mock processor only")
        self.provider = SimpleNamespace(provider_config={"type": "openai_chat_completion", "api_base": "https://provider.example.org/v1"},
            request_headers={"Cookie": "private", "OpenAI-Project": "allowed"},
            get_current_key=lambda: "test-secret", get_model=lambda: "test-gemini")
        self.context = SimpleNamespace(get_provider_by_id=lambda _: self.provider)
        self.config = {"allowed_scopes": ["umo-a"], "analysis_provider_ref": "test-provider"}
        self.plugin = self.entry.Main(self.context, self.config)
        self.data_patch = patch.object(self.runtime.StarTools, "get_data_dir", return_value=self.directory / "plugin-data")
        self.data_patch.start()

    async def asyncTearDown(self):
        await self.plugin.terminate()
        self.data_patch.stop()

    async def test_clean_package_registers_only_entry_and_no_personal_install(self):
        from astrbot.core.star.star import star_map
        from astrbot.core.star.star_handler import EventType, star_handlers_registry
        handlers = [h for kind in EventType for h in star_handlers_registry.get_handlers_by_event_type(kind)
                    if h.handler_module_path.startswith(self.name + ".")]
        self.assertEqual(len(handlers), 5)
        self.assertTrue(all(h.handler_module_path == self.entry.__name__ for h in handlers))
        self.assertNotIn(self.runtime.__name__, star_map)
        self.assertEqual(Path(self.entry.__file__).resolve(), self.install / "main.py")
        self.assertTrue((self.install / "video_understanding_core" / "service.py").is_file())
        self.assertTrue((self.install / "docs" / "acceptance.md").is_file())

    async def test_empty_scope_defaults_inactive_and_no_provider_lookup(self):
        from astrbot.api import AstrBotConfig
        schema = json.loads((self.install / "_conf_schema.json").read_text(encoding="utf-8"))
        config = AstrBotConfig(config_path=str(self.directory / "config.json"), schema=schema)
        plugin = self.entry.Main(SimpleNamespace(), config)
        req = SimpleNamespace(system_prompt="original")
        await plugin.prepare_request(event(), req)
        self.assertEqual(req.system_prompt, "original")
        self.assertIsNone(plugin.service)
        await plugin.terminate()

    async def test_disabled_scope_hides_only_video_tools_without_mutating_other_requests(self):
        from astrbot.core.agent.tool import FunctionTool, ToolSet
        tools = ToolSet(tools=[FunctionTool(name=name, description="test", parameters={})
                               for name in ("analyze_video", "other_tool", "get_video_analysis")])
        req = SimpleNamespace(system_prompt="original", func_tool=tools)
        await self.plugin.prepare_request(event(session="disabled"), req)
        self.assertEqual([tool.name for tool in req.func_tool.tools], ["other_tool"])
        self.assertEqual(len(tools.tools), 3)

    async def test_provider_type_checked_without_changing_host(self):
        self.provider.provider_config["type"] = "other"
        req = SimpleNamespace(system_prompt="original")
        await self.plugin.prepare_request(event(), req)
        self.assertIn("CHAT_COMPLETIONS_PROVIDER_REQUIRED", req.system_prompt)
        self.assertIsNone(self.plugin.service)

    async def test_video_file_and_quote_are_scoped_and_no_duplicate_import(self):
        from astrbot.api.message_components import File, Reply, Video
        for index, component in enumerate((Video.fromFileSystem(self.asset), File(name="unusual.bin", file=str(self.asset)))):
            ev = event(components=[component], mid=f"msg-{index}")
            req = SimpleNamespace(system_prompt="original-persona")
            await self.plugin.prepare_request(ev, req)
            sources = ev.get_extra("_video_sources")
            self.assertEqual(len(sources), 1)
            self.assertTrue(sources[0].startswith("att_"))
            self.assertTrue(req.system_prompt.startswith("original-persona"))
            self.assertNotIn(str(self.asset), req.system_prompt)
            self.assertNotIn("test-secret", req.system_prompt)
            await self.plugin.prepare_request(ev, req)
            self.assertEqual(ev.get_extra("_video_sources"), sources)
            quote = event(components=[Reply(id=f"msg-{index}")], mid=f"quote-{index}")
            await self.plugin.prepare_request(quote, SimpleNamespace(system_prompt=""))
            self.assertEqual(quote.get_extra("_video_sources"), sources)
        self.assertNotIn("Cookie", self.plugin.service.backend.config.headers)

    async def test_private_video_without_request_not_ingested(self):
        from astrbot.api.message_components import Video
        ev = event(text="", components=[Video.fromFileSystem(self.asset)])
        req = SimpleNamespace(system_prompt="original")
        await self.plugin.prepare_request(ev, req)
        self.assertEqual(req.system_prompt, "original")
        self.assertIsNone(self.plugin.service)

    async def test_remote_attachment_is_not_fetched_until_job(self):
        from astrbot.api.message_components import Video
        ev = event(components=[Video.fromURL("https://media.example.org/signed.mp4?temporary=secret")])
        req = SimpleNamespace(system_prompt="original")
        await self.plugin.prepare_request(ev, req)
        self.assertNotIn("temporary", req.system_prompt)
        self.assertNotIn("secret", req.system_prompt)
        ref = ev.get_extra("_video_sources")[0]
        self.assertFalse(self.plugin.service.resolver.attachments[ref].path.exists())

    async def test_quote_from_other_user_cannot_recover_index(self):
        from astrbot.api.message_components import Reply, Video
        ev = event(components=[Video.fromFileSystem(self.asset)])
        await self.plugin.prepare_request(ev, SimpleNamespace(system_prompt=""))
        other = event(user="user-b", components=[Reply(id="msg-a")])
        req = SimpleNamespace(system_prompt="")
        await self.plugin.prepare_request(other, req)
        self.assertIn("ATTACHMENT_UNAVAILABLE_RESEND", req.system_prompt)
        self.assertFalse(other.get_extra("_video_sources"))

    async def test_multiple_files_require_selection_without_ingest(self):
        from astrbot.api.message_components import Video
        ev = event(components=[Video.fromFileSystem(self.asset), Video.fromFileSystem(self.asset)])
        req = SimpleNamespace(system_prompt="")
        await self.plugin.prepare_request(ev, req)
        self.assertIn("SELECT_ONE_VIDEO", req.system_prompt)
        self.assertFalse(self.plugin.service.resolver.attachments)

    async def test_model_cannot_supply_arbitrary_source_even_in_enabled_scope(self):
        ev = event()
        output = json.loads(await self.plugin.analyze(ev, str(self.asset), "read host"))
        self.assertEqual(output["error"], "SOURCE_NOT_AUTHORIZED_FOR_EVENT")
        self.assertIsNone(self.plugin.service)

    async def test_native_tool_wait_uses_one_reply_owner(self):
        from astrbot.api.message_components import Video
        ev = event(components=[Video.fromFileSystem(self.asset)])
        await self.plugin.prepare_request(ev, SimpleNamespace(system_prompt=""))
        service = self.plugin.service
        service.submit = AsyncMock(return_value={"job_id": "job-a"})
        service.wait = AsyncMock(return_value={"job_id": "job-a", "status": "ANALYZING"})
        result = await self.plugin.analyze(ev, ev.get_extra("_video_sources")[0], "q")
        self.assertEqual(json.loads(result)["status"], "ANALYZING")
        self.assertFalse(hasattr(self.context, "send_message"))
        self.assertFalse(hasattr(ev, "send"))

    async def test_second_bot_cannot_receive_first_bot_references(self):
        from astrbot.api.message_components import Video
        ev = event(components=[Video.fromFileSystem(self.asset)])
        await self.plugin.prepare_request(ev, SimpleNamespace(system_prompt=""))
        other = event(bot="bot-b", mid="other-message")
        await self.plugin.prepare_request(other, SimpleNamespace(system_prompt=""))
        self.assertFalse(other.get_extra("_video_sources"))

    async def test_followup_can_request_new_evidence_using_same_scoped_ref(self):
        from astrbot.api.message_components import Video
        ev = event(components=[Video.fromFileSystem(self.asset)])
        await self.plugin.prepare_request(ev, SimpleNamespace(system_prompt=""))
        followup = event(mid="follow-up", text="他穿蓝色的吗")
        await self.plugin.prepare_request(followup, SimpleNamespace(system_prompt=""))
        self.assertEqual(followup.get_extra("_video_sources"), ev.get_extra("_video_sources"))
