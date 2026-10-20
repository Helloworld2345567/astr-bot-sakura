import asyncio
from dataclasses import replace
import json
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from support import result_payload
from video_understanding_core.backend import BackendConfig, ChatCompletionsGemini, validate_result
from video_understanding_core.models import AnalysisRequest, CallerContext, Limits, MediaProbe, PreparedMedia, VideoError
from video_understanding_core.network import public_addresses, validate_url
from video_understanding_core.service import VideoService
from video_understanding_core.sources import platform_source
from video_understanding_core.store import JobStore


class ContractTests(unittest.TestCase):
    def test_network_rejects_non_https_and_ambiguous_authorities(self):
        for url in ("http://example.org/v", "file:///tmp/file", "https://" + "user:secret@example.org",
                    "https://example.org:123/v", "https://example.org\\@localhost", "https://example.org/\nx"):
            with self.subTest(url=url), self.assertRaises(VideoError):
                validate_url(url)

    def test_all_dns_answers_must_be_public(self):
        for ip in ("127.0.0.1", "10.0.0.2", "169.254.169.254", "::1", "::ffff:8.8.8.8", "224.0.0.1"):
            answers = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443)),
                       (socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, 443))]
            with self.subTest(ip=ip), patch("socket.getaddrinfo", return_value=answers), self.assertRaises(VideoError):
                public_addresses("media.example.org")

    def test_platform_identity_and_part(self):
        source = platform_source("https://www.bilibili.com/video/BV1234567890?p=2&tracking=unused")
        self.assertEqual(source["page"], 2)
        self.assertNotIn("tracking", source["ref"])
        self.assertEqual(platform_source("BV1234567890")["work_id"], "BV1234567890")
        self.assertEqual(platform_source("https://www.douyin.com/video/1234")["work_id"], "1234")
        self.assertEqual(platform_source("https://v.douyin.com/abc/?tracking=unused")["ref"], "https://v.douyin.com/abc/")

    def test_platform_rejects_spoofed_hosts_galleries_and_invalid_parts(self):
        for url in ("https://bilibili.com.evil.org/video/BV1234567890", "https://www.douyin.com/note/123",
                    "https://www.bilibili.com/video/BV1234567890?p=0", "https://www.bilibili.com/playlist/123"):
            with self.subTest(url=url), self.assertRaises(VideoError):
                platform_source(url)

    def test_finite_ranges_and_limits(self):
        for start, end in ((0, None), (-1, 1), (1, 1), (0, float("nan")), (0, float("inf"))):
            with self.subTest(start=start, end=end), self.assertRaises(VideoError):
                AnalysisRequest("x", "question", start, end).validate()
        with self.assertRaises(ValueError):
            Limits(global_concurrency=1.5)

    def test_schema_and_offset_are_locally_enforced(self):
        media = PreparedMedia(None, MediaProbe(2, True, True, 10), 30, 20, 22)
        output = validate_result(json.dumps(result_payload()), media)
        self.assertEqual(output["visual_observations"][0]["at_seconds"], 20.5)
        with self.assertRaisesRegex(VideoError, "MEDIA_NOT_RECEIVED"):
            validate_result(json.dumps({"received_media": False}), media)
        for text in ("not JSON", "{}", json.dumps(result_payload(visual_observations=[{"at_seconds": 5, "text": "x"}]))):
            with self.subTest(text=text), self.assertRaisesRegex(VideoError, "INVALID_MODEL_RESULT"):
                validate_result(text, media)
        silent = replace(media, probe=replace(media.probe, has_audio=False))
        output = validate_result(json.dumps(result_payload(spoken_quotes=[{"at_seconds": .5, "text": "fake"}])), silent)
        self.assertEqual(output["audio_observations"], [])
        self.assertEqual(output["spoken_quotes"], [])
        self.assertTrue(output["warnings"])

    def test_provider_secrets_hidden_and_headers_restricted(self):
        config = BackendConfig("https://provider.example.org/v1", "test-secret", "test-gemini")
        self.assertNotIn("test-secret", repr(config))
        self.assertNotIn("provider.example.org", repr(config))
        with self.assertRaises(VideoError):
            BackendConfig("https://provider.example.org/v1", "test-secret", "m", {"Cookie": "bad"})


class FakeProcessor:
    async def prepare(self, path, directory, request):
        return PreparedMedia(path, MediaProbe(2, True, True, path.stat().st_size), 2, 0, 2)


class FakeBackend:
    version = "test-model-prompt-1"

    def __init__(self):
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.error = None

    async def analyze(self, prepared, question):
        self.calls += 1
        self.started.set()
        await self.release.wait()
        if self.error:
            raise VideoError(self.error)
        result = result_payload()
        result.pop("received_media")
        result["usage"] = {"model": "fake", "input_tokens": None, "output_tokens": None}
        return result


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "test.bin"
        self.source.write_bytes(b"bounded synthetic media")
        self.caller = CallerContext("bot-a", "user-a", "session-a")
        self.backend = FakeBackend()
        self.service = VideoService(self.root / "store", self.backend, processor=FakeProcessor())
        self.ref = self.service.resolver.register_file(self.source, self.caller)

    async def asyncTearDown(self):
        if not self.service.closed:
            await self.service.close()
        self.temp.cleanup()

    async def finish(self, request=None):
        job = await self.service.submit(request or AnalysisRequest(self.ref, "question", message_id="message-a"), self.caller)
        return await self.service.wait(job["job_id"], self.caller, timeout=2)

    async def test_duplicate_message_uploads_once_even_if_tool_rephrases_question(self):
        first = await self.finish()
        second = await self.finish(AnalysisRequest(self.ref, "rephrased question", message_id="message-a"))
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(self.backend.calls, 1)
        self.assertEqual(first["status"], "COMPLETE")
        self.assertEqual(first["coverage"]["end_seconds"], 2)
        self.assertEqual(first["evidence_mode"], "FULL_MEDIA")

    async def test_content_cache_and_question_invalidation(self):
        first = await self.finish()
        second = await self.finish(AnalysisRequest(self.ref, "question", message_id="message-b"))
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(self.backend.calls, 1)
        third = await self.finish(AnalysisRequest(self.ref, "new evidence?", message_id="message-c"))
        self.assertFalse(third["cache_hit"])
        self.assertEqual(self.backend.calls, 2)

    async def test_bot_user_session_isolation_on_all_operations(self):
        job = await self.finish()
        for other in (replace(self.caller, bot="bot-b"), replace(self.caller, user="user-b"),
                      replace(self.caller, session="session-b")):
            with self.subTest(other=other):
                for operation in ("get", "cancel", "delete"):
                    with self.assertRaises(VideoError):
                        output = getattr(self.service, operation)(job["job_id"], other)
                        if operation != "get":
                            await output
                with self.assertRaises(VideoError):
                    self.service.resolver.describe(self.ref, other)

    async def test_one_active_user_and_cancel_does_not_reupload(self):
        self.backend.release.clear()
        job = await self.service.submit(AnalysisRequest(self.ref, "q"), self.caller)
        await self.backend.started.wait()
        with self.assertRaisesRegex(VideoError, "USER_BUSY"):
            await self.service.submit(AnalysisRequest(self.ref, "q2"), self.caller)
        cancelled = await self.service.cancel(job["job_id"], self.caller)
        self.assertEqual(cancelled["status"], "CANCELLED")
        self.assertIn("计费", cancelled["warning"])
        self.assertFalse(list(self.service.work.iterdir()))
        self.assertEqual(self.backend.calls, 1)

    async def test_cancel_before_task_starts_releases_user_slot(self):
        job = await self.service.submit(AnalysisRequest(self.ref, "q"), self.caller)
        await self.service.cancel(job["job_id"], self.caller)
        self.assertFalse(self.service.active)
        self.assertEqual(self.backend.calls, 0)

    async def test_queue_deadline_includes_waiting(self):
        self.service.limits = replace(self.service.limits, job_timeout=.05)
        await self.service.semaphore.acquire()
        job = await self.service.submit(AnalysisRequest(self.ref, "q"), self.caller)
        result = await self.service.wait(job["job_id"], self.caller, timeout=1)
        self.service.semaphore.release()
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(self.backend.calls, 0)

    async def test_model_errors_not_retried(self):
        self.backend.error = "MODEL_TIMEOUT_MAY_BE_BILLED"
        result = await self.finish()
        self.assertEqual(result["error"], "MODEL_TIMEOUT_MAY_BE_BILLED")
        self.assertEqual(self.backend.calls, 1)

    async def test_daily_job_limit_survives_delete_and_restart(self):
        self.service.limits = replace(self.service.limits, daily_jobs_per_user=1)
        job = await self.finish()
        await self.service.delete(job["job_id"], self.caller)
        await self.service.close()
        self.service = VideoService(self.root / "store", self.backend, limits=Limits(daily_jobs_per_user=1), processor=FakeProcessor())
        ref = self.service.resolver.register_file(self.source, self.caller)
        with self.assertRaisesRegex(VideoError, "DAILY_BUDGET_EXCEEDED"):
            await self.service.submit(AnalysisRequest(ref, "q2"), self.caller)

    async def test_duration_budget_prevents_model_request(self):
        self.service.limits = replace(self.service.limits, daily_seconds_per_user=1)
        result = await self.finish()
        self.assertEqual(result["error"], "DAILY_DURATION_EXCEEDED")
        self.assertEqual(self.backend.calls, 0)

    async def test_restart_marks_incomplete_and_delete_cleans_persisted_attachment(self):
        job = await self.finish()
        path = self.service.resolver.attachments[self.ref].path
        self.service.store.update(job["job_id"], "ANALYZING")
        await self.service.close()
        self.service = VideoService(self.root / "store", self.backend, processor=FakeProcessor())
        result = self.service.get(job["job_id"], self.caller)
        self.assertEqual(result["error"], "INTERRUPTED_NO_AUTO_RESEND")
        await self.service.delete(job["job_id"], self.caller)
        self.assertFalse(path.exists())
        with self.assertRaises(VideoError):
            self.service.get(job["job_id"], self.caller)

    async def test_expired_attachment_not_recovered_by_path_search(self):
        self.service.resolver.attachments[self.ref].expires = 0
        with self.assertRaisesRegex(VideoError, "ATTACHMENT_UNAVAILABLE_RESEND"):
            await self.service.submit(AnalysisRequest(self.ref, "q"), self.caller)
        self.service.resolver.cleanup()
        self.assertNotIn(self.ref, self.service.resolver.attachments)

    async def test_cleanup_does_not_remove_attachment_while_queued_or_running(self):
        await self.service.semaphore.acquire()
        job = await self.service.submit(AnalysisRequest(self.ref, "q"), self.caller)
        self.service.resolver.attachments[self.ref].expires = 0
        self.service.resolver.cleanup()
        self.assertIn(self.ref, self.service.resolver.attachments)
        self.service.semaphore.release()
        result = await self.service.wait(job["job_id"], self.caller, timeout=2)
        self.assertEqual(result["status"], "COMPLETE")
        self.service.resolver.cleanup()
        self.assertNotIn(self.ref, self.service.resolver.attachments)

    async def test_delete_cannot_remove_media_used_by_another_analysis(self):
        first = await self.finish()
        self.backend.started.clear()
        self.backend.release.clear()
        second = await self.service.submit(AnalysisRequest(self.ref, "different evidence?", message_id="message-b"), self.caller)
        await self.backend.started.wait()
        with self.assertRaisesRegex(VideoError, "MEDIA_IN_USE_CANCEL_FIRST"):
            await self.service.delete(first["job_id"], self.caller)
        await self.service.cancel(second["job_id"], self.caller)
        await self.service.delete(first["job_id"], self.caller)

    async def test_raw_host_path_cannot_be_tool_source(self):
        with self.assertRaises(VideoError):
            await self.service.submit(AnalysisRequest(str(self.source), "q"), self.caller)
