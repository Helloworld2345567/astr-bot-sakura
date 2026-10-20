"""Public web tools must not inherit host-command permission checks."""
import unittest
from types import SimpleNamespace

from test_sakura_runtime_context import Event, permissions
from support import TEST_ADMIN_ID
from astrbot.core.tools.web_search_tools import TavilyExtractWebPageTool, TavilyWebSearchTool


class WebPermissionTests(unittest.TestCase):
    def manager(self):
        from pathlib import Path
        return permissions.PermissionManager(config={"admin_user_ids": [TEST_ADMIN_ID]})

    def cases(self):
        return (
            (TavilyWebSearchTool(), {"query": "PowerShell API key 密钥和注册表教程"}),
            (TavilyExtractWebPageTool(), {"url": "https://example.com/download/api-key"}),
        )

    def test_web_search_topics_and_urls_are_not_host_actions(self):
        for tool, args in self.cases():
            with self.subTest(tool=tool.name):
                self.assertFalse(self.manager().tool_is_high_risk(tool, args))

    def test_web_tools_work_for_members_and_do_not_consume_host_confirmation(self):
        for private in (True, False):
            for tool, args in self.cases():
                for confirmed in (False, True):
                    with self.subTest(private=private, tool=tool.name, confirmed=confirmed):
                        event = Event("搜索网页", private=private)
                        manager = self.manager()
                        event.set_extra(manager.HIGH_RISK_CONFIRMED_EXTRA, confirmed)
                        manager.enforce_tool(event, tool, args)
                        self.assertEqual(event.role, "member")
                        self.assertFalse(event.get_extra(manager.HIGH_RISK_BLOCKED_EXTRA, False))
                        self.assertEqual(event.get_extra(manager.HIGH_RISK_CONFIRMED_EXTRA), confirmed)

    def test_tool_name_alone_does_not_bypass_command_checks(self):
        tool = SimpleNamespace(name="web_search_tavily")
        self.assertTrue(self.manager().tool_is_high_risk(tool, {"command": "powershell"}))

    def test_shell_still_needs_its_own_authorization(self):
        event = Event("搜索网页")
        manager = self.manager()
        manager.enforce_tool(event, SimpleNamespace(name="astrbot_execute_shell"), {"command": "curl example.com"})
        self.assertEqual(event.role, "member")
        self.assertTrue(event.get_extra(manager.HIGH_RISK_BLOCKED_EXTRA))


if __name__ == "__main__":
    unittest.main()
