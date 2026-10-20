"""Plugin installation contract and publishable repository defaults."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from support import ROOT, entry, plugin
from astrbot.api import AstrBotConfig


class DeploymentTests(unittest.IsolatedAsyncioTestCase):
    async def test_astrbot_constructs_plugin_from_schema_without_personal_config(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as temp:
            config = AstrBotConfig(config_path=str(Path(temp) / "plugin.json"), schema=schema)
            instance = entry.Main(SimpleNamespace(), config)
            self.assertFalse(instance.permissions.admin_user_ids)
            self.assertEqual(instance.message_flow.window, 2)
            self.assertEqual(instance.settings.image_base_url, "https://api.openai.com/v1")
            await instance.terminate()

    async def test_import_is_this_repository_and_client_is_bundled(self):
        self.assertEqual(Path(entry.__file__).resolve(), ROOT / "main.py")
        self.assertEqual(Path(plugin.__file__).resolve(), ROOT / "sakura" / "runtime.py")
        self.assertTrue((ROOT / "sakura" / "image_api.py").is_file())

    async def test_other_personas_keep_their_permission_state(self):
        instance = entry.Main(SimpleNamespace())
        event = SimpleNamespace(get_extra=lambda *args: False, role="admin")
        instance.permissions.apply_event = lambda *args: self.fail("unrelated persona permissions were changed")
        instance.permissions.enforce_tool = lambda *args: self.fail("unrelated persona tools were changed")
        await instance.enforce_sakura_permissions(event, SimpleNamespace())
        await instance.enforce_sakura_tool_permissions(event, SimpleNamespace(), {})
        self.assertEqual(event.role, "admin")

    async def test_configured_persona_resolves(self):
        manager = SimpleNamespace(resolve_selected_persona=AsyncMock(return_value=("测试人格", None, None, False)))
        instance = entry.Main(SimpleNamespace(persona_manager=manager), {"persona_id": "测试人格"})
        event = SimpleNamespace(unified_msg_origin="test", get_platform_name=lambda: "test")
        self.assertTrue(await instance._uses_sakura_persona(event))
        entry.Main(SimpleNamespace())  # restore the single-plugin filter setting

    async def test_handlers_register_only_under_astrbot_entry_point(self):
        from astrbot.core.star.star import star_map
        from astrbot.core.star.star_handler import EventType, star_handlers_registry

        handlers = [
            handler
            for event_type in EventType
            for handler in star_handlers_registry.get_handlers_by_event_type(event_type)
            if handler.handler_module_path.startswith("sakura_plugin_under_test.")
        ]
        self.assertEqual(len(handlers), 5)
        self.assertTrue(all(handler.handler_module_path == entry.__name__ for handler in handlers))
        self.assertIs(star_map[entry.__name__].star_cls_type, entry.Main)
        self.assertNotIn(plugin.__name__, star_map)
        self.assertIn("terminate", entry.Main.__dict__)


class ReleaseScanTests(unittest.TestCase):
    def test_release_sources_are_portable(self):
        spec = importlib.util.spec_from_file_location("release_scan_under_test", ROOT / "scripts" / "check_release.py")
        scan = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(scan)
        for name in scan.RELEASE_FILES:
            with self.subTest(file=name):
                self.assertFalse(scan.findings(name, (ROOT / name).read_text(encoding="utf-8")))
