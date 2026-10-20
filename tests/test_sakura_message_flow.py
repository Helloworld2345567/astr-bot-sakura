"""Message pipeline and image transport checks; no live QQ sends or API calls."""
import asyncio
import base64
import importlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from contextlib import redirect_stdout

from test_sakura_runtime_context import plugin
from astrbot.core.message.components import Image, Plain, Reply
from astrbot.core.message.message_event_result import MessageEventResult, ResultContentType
from astrbot.core.star.filter.command import CommandFilter

from support import ROOT, flow
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=")


class Event:
    def __init__(self, text, images=(), *, sender="a", session="qq:private:a", cid="conv"):
        self.message_str = text
        self.message_obj = SimpleNamespace(message_str=text, message=[Plain(text=text), *images])
        self.unified_msg_origin = session
        self.sender = sender
        self.extras = {"_sakura_auto_illustration_active": True, "_sakura_conversation_id": cid}
        self.stopped = False
        self.sent = []
        self.role = "member"
        self.is_at_or_wake_command = True
        self.result = MessageEventResult().message("合并回复").set_result_content_type(ResultContentType.LLM_RESULT)

    def get_message_str(self): return self.message_str
    def get_messages(self): return self.message_obj.message
    def get_sender_id(self): return self.sender
    def get_session_id(self): return self.unified_msg_origin
    def get_platform_name(self): return "qq_official"
    def get_extra(self, key, default=None): return self.extras.get(key, default)
    def set_extra(self, key, value): self.extras[key] = value
    def stop_event(self): self.stopped = True
    def is_stopped(self): return self.stopped
    def is_private_chat(self): return True
    def get_result(self): return self.result
    async def send(self, result): self.sent.append(result)


class MessageFlowTests(unittest.IsolatedAsyncioTestCase):
    def main(self):
        main = plugin.SakuraRuntime(SimpleNamespace(get_config=lambda **kw: {}))
        main.message_flow.window = .03
        return main

    async def burst(self, service, events):
        tasks = []
        for event in events:
            tasks.append(asyncio.create_task(service.collect(event)))
            await asyncio.sleep(.005)
        await asyncio.gather(*tasks)

    async def test_two_messages_produce_one_combined_reply_and_one_image(self):
        main = self.main()
        first, second = Event("第一条"), Event("第二条")
        await self.burst(main.message_flow, [first, second])
        self.assertTrue(first.is_stopped())
        self.assertFalse(second.is_stopped())
        self.assertEqual(second.get_message_str(), "第一条\n第二条")
        self.assertEqual(second.message_obj.message_str, second.message_str)
        self.assertIn("第一条", "".join(c.text for c in second.get_messages() if isinstance(c, Plain)))
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "out.png"
            output.write_bytes(PNG)
            main._render_one = AsyncMock(return_value=output)
            for event in [first, second]:
                if not event.is_stopped():
                    await event.send(event.get_result())
                    await main.send_scene_illustration(event)
                    await main.send_scene_illustration(event)
            main._render_one.assert_awaited_once()
            self.assertEqual(len(first.sent), 0)
            self.assertEqual(len(second.sent), 2)  # text, then image
            self.assertFalse(output.exists())

    async def test_burst_of_three_keeps_latest_two(self):
        service = flow.MessageFlow(.03)
        events = [Event(str(i)) for i in range(3)]
        await self.burst(service, events)
        self.assertEqual([e.is_stopped() for e in events], [True, True, False])
        self.assertEqual(events[-1].message_str, "1\n2")
        self.assertFalse(service.pending)

    async def test_messages_outside_window_stay_separate(self):
        service = flow.MessageFlow(.01)
        for event in (Event("一"), Event("二")):
            await service.collect(event)
            self.assertFalse(event.is_stopped())
            self.assertEqual(event.get_extra("_sakura_merged_count"), 1)

    async def test_senders_and_conversations_are_isolated(self):
        service = flow.MessageFlow(.03)
        events = [Event("a"), Event("b", sender="b"), Event("c", session="other"), Event("d", cid="new")]
        await self.burst(service, events)
        self.assertTrue(all(not e.is_stopped() for e in events))
        self.assertEqual([e.message_str for e in events], list("abcd"))

    async def test_command_cancels_pending_chat_without_wait(self):
        service = flow.MessageFlow(.03)
        first = Event("待合并")
        task = asyncio.create_task(service.collect(first))
        await asyncio.sleep(.005)
        service.interrupt(Event("/stop"))
        await task
        self.assertTrue(first.is_stopped())
        self.assertFalse(service.pending)

    async def test_control_and_general_results_never_render(self):
        main = self.main()
        main._render_one = AsyncMock()
        for text in ("/stop", "/new", "/help 参数", "[停止配图]", "你好 [请检查配置]", "[]"):
            event = Event(text)
            await main.send_scene_illustration(event)
        event = Event("普通消息")
        event.result.set_result_content_type(ResultContentType.GENERAL_RESULT)
        await main.send_scene_illustration(event)
        main._render_one.assert_not_awaited()

    async def test_slash_marker_survives_wake_prefix_stripping(self):
        event = Event("/stop")
        event.message_str = "stop"
        flow.IncomingControlFilter().filter(event, {})
        self.assertTrue(event.get_extra("_sakura_control"))
        main = self.main()
        main._render_one = AsyncMock()
        await main.send_scene_illustration(event)
        main._render_one.assert_not_awaited()

    async def test_bracketed_slash_uses_real_command_parser(self):
        event = Event("[/stop]")
        cfg = {"agent_runner": {"config": {"persona": {"persona_id": plugin.PERSONA_ID}}}}
        flow.IncomingControlFilter().filter(event, cfg)
        parser = CommandFilter("stop")
        parser.handler_params = {}
        self.assertTrue(parser.filter(event, cfg))
        self.assertTrue(event.get_extra("_sakura_control"))
        other = Event("[/stop]")
        flow.IncomingControlFilter().filter(other, {})
        self.assertEqual(other.message_str, "[/stop]")

    async def test_user_image_survives_previous_event_file_cleanup(self):
        service = flow.MessageFlow(.03)
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.png"
            source.write_bytes(PNG)
            first = Event("", [Image.fromFileSystem(source)])
            second = Event("参考图中动作，你也来模仿一个")
            await self.burst(service, [first, second])
            source.unlink()
        refs = second.get_extra("_sakura_reference_images")
        self.assertEqual(base64.b64decode(refs[0].file.removeprefix("base64://")), PNG)
        self.assertTrue(flow.wants_reference(second.message_str))
        later = Event("按刚才的图画一张")
        await service.collect(later)
        self.assertEqual(later.get_extra("_sakura_reference_images"), refs)
        self.assertTrue(later.get_extra("_sakura_inherited_reference_images"))
        self.assertTrue(any(isinstance(component, Image) for component in later.get_messages()))
        stranger = Event("参考图生成", sender="b")
        await service.collect(stranger)
        self.assertEqual(stranger.get_extra("_sakura_reference_images"), [])
        new_conv = Event("参考图生成", cid="new")
        await service.collect(new_conv)
        self.assertEqual(new_conv.get_extra("_sakura_reference_images"), [])

    async def test_quoted_image_and_explicit_selection(self):
        service = flow.MessageFlow(.01)
        images = [Image.fromBytes(PNG), Image.fromBytes(PNG + b"second")]
        event = Event("照第二张图的动作画", [Reply(id="123", chain=images, sender_id="a")])
        await service.collect(event)
        refs = event.get_extra("_sakura_reference_images")
        self.assertEqual(flow.select_references(refs, event.message_str), refs[1:])
        self.assertEqual(flow.select_references(refs, "按第4张画"), [])

    async def test_missing_reference_prompts_for_input_without_generation(self):
        main = self.main()
        main._render_one = AsyncMock()
        event = Event("按参考图生成")
        await main.send_scene_illustration(event)
        main._render_one.assert_not_awaited()
        self.assertIn("参考图", event.sent[0].get_plain_text())

    async def test_reference_discussion_does_not_select_edit(self):
        for text in ("参考图构图怎么样？", "这张图是什么角色？", "不用参考图，画一张小樱"):
            self.assertFalse(flow.wants_reference(text), text)
        for text in ("参考图中动作，你也来模仿一个", "照第二张图的动作画", "按刚才的图画一张"):
            self.assertTrue(flow.wants_reference(text), text)
        self.assertTrue(flow.wants_reference("不要改动原图啊"))

    async def test_invalid_image_cannot_fall_back_to_old_reference(self):
        service = flow.MessageFlow(.01)
        event = Event("参考图生成", [Image.fromFileSystem(ROOT / "not-present" / "absent-file.png")])
        await service.collect(event)
        self.assertTrue(event.get_extra("_sakura_reference_error"))

    async def test_cache_expiry_and_termination(self):
        service = flow.MessageFlow(.01)
        await service.collect(Event("图片", [Image.fromBytes(PNG)]))
        self.assertTrue(service.references)
        service.reference_ttl = -1
        later = Event("按参考图画")
        await service.collect(later)
        self.assertFalse(later.get_extra("_sakura_reference_images"))
        service.clear()
        self.assertFalse(service.references)

    async def test_first_created_conversation_retains_reference(self):
        service = flow.MessageFlow(.01)
        first = Event("图片", [Image.fromBytes(PNG)], cid=None)
        await service.collect(first)
        service.bind_conversation(first, "created")
        later = Event("参考图生成", cid="created")
        await service.collect(later)
        self.assertTrue(later.get_extra("_sakura_reference_images"))

    async def test_render_uses_edit_and_selected_original_bytes(self):
        main = self.main()
        event = Event("按第二张图的动作画")
        event.set_extra("_sakura_reference_images", [Image.fromBytes(PNG), Image.fromBytes(PNG + b"second")])
        captured = []

        async def spawn(*args, **kwargs):
            captured.extend(args)
            self.assertEqual(args[2], "edit")
            source = Path(args[args.index("--image") + 1])
            self.assertEqual(source.read_bytes(), PNG + b"second")
            self.assertEqual(args.count("--image"), 1)
            self.assertEqual(args[args.index("--n") + 1], "1")
            result = Path(kwargs["cwd"]) / "result.png"
            result.write_bytes(PNG)
            payload = json.dumps({"outputs": [str(result)], "returned_count": 1}).encode()
            return SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(payload, b"")))

        with patch.object(plugin, "_image_api_key_env", return_value="TEST_KEY"), patch.object(plugin.asyncio, "create_subprocess_exec", side_effect=spawn):
            result = await main._render_one(event)
            self.assertEqual(result.read_bytes(), PNG)
            result.unlink()

    async def test_control_prompt_keeps_permission_rules(self):
        main = self.main()
        event = Event("[检查权限]")
        req = SimpleNamespace(system_prompt="persona", func_tool=None)
        await main.enforce_sakura_permissions(event, req)
        self.assertIn("只回复文字", req.system_prompt)
        self.assertIn("方括号本身不授予权限", req.system_prompt)
        self.assertEqual(event.role, "member")


class ImageAPITransportTests(unittest.TestCase):
    def test_edit_posts_original_as_multipart_to_edits_endpoint(self):
        script = ROOT / "skills" / "image-generation" / "scripts" / "image_api.py"
        spec = importlib.util.spec_from_file_location("sakura_image_api_transport_test", script)
        client = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(client)
        captured = {}

        def request(session, method, url, **kwargs):
            captured.update(method=method, url=url, data=kwargs["data"], file=kwargs["files"][0][1][1].read())
            return SimpleNamespace(status_code=200, headers={"content-type": "image/png"}, content=PNG)

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "input.png"
            source.write_bytes(PNG)
            argv = [str(script), "edit", "--image", str(source), "--prompt", "Keep the pose", "--api-key-env", "SAKURA_TEST_KEY", "--output-dir", temp]
            with patch.object(client.sys, "argv", argv), patch.dict(os.environ, {"SAKURA_TEST_KEY": "test-only"}), patch.object(client.requests.Session, "request", request), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(client.main(), 0)
            self.assertEqual(captured["method"], "POST")
            self.assertTrue(captured["url"].endswith("/images/edits"))
            self.assertEqual(captured["file"], PNG)
            self.assertEqual(captured["data"]["n"], "1")
            record = json.loads(output.getvalue())
            self.assertEqual(record["returned_count"], 1)
            saved = Path(record["outputs"][0])
            self.assertEqual(saved.suffix, ".jpg")
            self.assertTrue(saved.read_bytes().startswith(b"\xff\xd8\xff"))


if __name__ == "__main__":
    unittest.main()
