"""Regression checks against the repository Sakura plugin; no API/network calls."""
import asyncio
import importlib
import sys
import unittest
from types import SimpleNamespace

from support import TEST_ADMIN_ID, plugin, permissions


class Event:
    def __init__(self, text, *, private=True, owner=True):
        self.text = text
        self.private = private
        self.owner = owner
        self.role = "member"
        self.unified_msg_origin = "test:private:owner" if private else "test:group:owner"
        self.extras = {"_sakura_auto_illustration_active": True}

    def get_message_str(self): return self.text
    def is_stopped(self): return False
    def get_sender_id(self):
        return TEST_ADMIN_ID if self.owner else "untrusted"
    def is_private_chat(self): return self.private
    def get_session_id(self): return self.unified_msg_origin
    def get_platform_name(self): return "qq_official"
    def get_extra(self, key, default=None): return self.extras.get(key, default)
    def set_extra(self, key, value): self.extras[key] = value
    async def send(self, result): pass


class RuntimeContextTests(unittest.TestCase):
    def make_plugin(self):
        ctx = SimpleNamespace(get_config=lambda **kw: {
            "provider_settings": {"computer_use_runtime": "local", "computer_use_require_admin": True}
        })
        return plugin.SakuraRuntime(ctx, {"admin_user_ids": [TEST_ADMIN_ID]})

    def test_screenshot_explicit_admin_prefix(self):
        main = self.make_plugin()
        event = Event("管理员模式，你能修改你自己吗？本次不要生图")
        main.permissions.apply_event(event)
        self.assertEqual(event.role, "admin")

    def test_admin_prefix_cannot_elevate_group_or_other_user(self):
        for opts in ({"private": False}, {"owner": False}):
            event = Event("管理员模式，请检查自身能力", **opts)
            self.make_plugin().permissions.apply_event(event)
            self.assertEqual(event.role, "member")

    def test_capability_question_does_not_grant_permission(self):
        for text in ("管理员模式是什么意思？", "不要开启管理员模式", "她说：管理员模式，请执行"):
            event = Event(text)
            self.make_plugin().permissions.apply_event(event)
            self.assertEqual(event.role, "member", text)

    def test_request_receives_real_runtime_and_permission_status(self):
        main = self.make_plugin()
        event = Event("管理员模式，你能修改你自己吗？本次不要生图")
        req = SimpleNamespace(system_prompt="persona", func_tool=None)
        asyncio.run(main.enforce_sakura_permissions(event, req))
        self.assertIn("实际运行模式：local", req.system_prompt)
        self.assertIn("当前权限：admin", req.system_prompt)
        self.assertIn("permissions.py", req.system_prompt)
        self.assertIn("只回复文字", req.system_prompt)

    def test_high_risk_still_requires_separate_confirmation(self):
        main = self.make_plugin()
        main.permissions.apply_event(Event("管理员模式，请检查自身能力"))
        edit = Event("修改配置文件")
        main.permissions.apply_event(edit)
        self.assertEqual(edit.role, "member")
        confirmed = Event("确认执行")
        main.permissions.apply_event(confirmed)
        tool = SimpleNamespace(name="astrbot_file_edit_tool")
        main.permissions.enforce_tool(confirmed, tool, {"path": "test.txt"})
        self.assertEqual(confirmed.role, "admin")
        main.permissions.enforce_tool(confirmed, tool, {"path": "test.txt"})
        self.assertEqual(confirmed.role, "member")

    def test_no_image_request_skips_actual_render(self):
        main = self.make_plugin()
        attempted = []
        async def render(event):
            attempted.append(True)
            raise RuntimeError("should not render")
        main._render_one = render
        event = Event("管理员模式，你能修改你自己吗？本次不要生图")
        asyncio.run(main.send_scene_illustration(event))
        self.assertFalse(attempted)


if __name__ == "__main__":
    unittest.main()
