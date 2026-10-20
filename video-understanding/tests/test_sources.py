import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from support import ROOT
from video_understanding_core.extract_worker import QuietLogger, extract
from video_understanding_core.models import AnalysisRequest, CallerContext, Limits, VideoError
from video_understanding_core.service import VideoService
from video_understanding_core.sources import SourceResolver


class ExtractorTests(unittest.TestCase):
    def metadata(self, url, info, warn=None):
        import yt_dlp
        from yt_dlp.cookies import YoutubeDLCookieJar
        from yt_dlp.networking.common import RequestDirector
        logger = QuietLogger()
        director = RequestDirector(logger)
        fake = SimpleNamespace(_request_director=director, cookiejar=YoutubeDLCookieJar())
        def extract_info(source, download):
            self.assertFalse(download)
            self.assertEqual(set(director.handlers), {"Guarded"})
            self.assertEqual(director.preferences, set())
            if warn:
                config[0]["logger"].warning(warn)
            return info
        fake.extract_info = extract_info
        class Context:
            def __enter__(self): return fake
            def __exit__(self, *args): director.close()
        config = []
        def factory(options):
            config.append(options)
            self.assertEqual(options["js_runtimes"], {})
            self.assertEqual(options["remote_components"], [])
            return Context()
        with patch.object(yt_dlp, "YoutubeDL", side_effect=factory):
            return extract(url)

    def info(self, **overrides):
        return {"id": "BV1234567890_p2", "protocol": "https", "url": "https://media.example.org/video",
                "http_headers": {"Cookie": "private", "Referer": "https://www.bilibili.com", "X-Unknown": "no"}, **overrides}

    def test_exact_bilibili_part_and_only_safe_download_headers(self):
        output = self.metadata("https://www.bilibili.com/video/BV1234567890?p=2", self.info())
        self.assertTrue(output["source"].endswith("?p=2"))
        self.assertEqual(output["streams"][0]["headers"], {"Referer": "https://www.bilibili.com"})

    def test_wrong_work_or_part_is_never_replaced(self):
        for video_id, page in (("BV1234567899_p2", 2), ("BV1234567890_p3", 2), ("BV1234567890_p2", 1)):
            with self.subTest(video_id=video_id, page=page), self.assertRaisesRegex(VideoError, "SOURCE_IDENTITY_MISMATCH"):
                self.metadata(f"https://www.bilibili.com/video/BV1234567890?p={page}", self.info(id=video_id))

    def test_preview_is_not_full_media_success(self):
        with self.assertRaisesRegex(VideoError, "SOURCE_LOGIN_REQUIRED"):
            self.metadata("https://www.bilibili.com/video/BV1234567890?p=2", self.info(), warn="Only preview will be extracted")

    def test_wrong_douyin_work_and_gallery(self):
        with self.assertRaisesRegex(VideoError, "SOURCE_IDENTITY_MISMATCH"):
            self.metadata("https://www.douyin.com/video/123", self.info(id="456"))
        with self.assertRaisesRegex(VideoError, "GALLERY_NOT_VIDEO"):
            self.metadata("https://www.douyin.com/note/123", self.info())

    def test_live_playlist_and_segmented_formats_are_explicitly_rejected(self):
        for overrides in ({"is_live": True}, {"_type": "playlist"}, {"protocol": "m3u8_native"}, {"fragments": [{"url": "x"}]}):
            with self.subTest(overrides=overrides), self.assertRaises(VideoError):
                self.metadata("https://www.bilibili.com/video/BV1234567890?p=2", self.info(**overrides))


class SourceBudgetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.caller = CallerContext("bot-a", "user-a", "session-a")
    async def asyncTearDown(self): self.temp.cleanup()

    async def test_disk_budget_checked_before_registering_remote_asset(self):
        resolver = SourceResolver(self.root / "assets", Limits(max_storage_bytes=1))
        with self.assertRaisesRegex(VideoError, "STORAGE_BUDGET_EXCEEDED"):
            resolver.register_remote("https://media.example.org/a", self.caller)

    async def test_attachment_cap_checked_even_for_zero_byte_remote_records(self):
        resolver = SourceResolver(self.root / "assets", Limits(daily_jobs_per_user=1))
        resolver.register_remote("https://media.example.org/a", self.caller)
        with self.assertRaisesRegex(VideoError, "ATTACHMENT_LIMIT"):
            resolver.register_remote("https://media.example.org/b", self.caller)

    async def test_queue_capacity_and_global_concurrency_have_no_extra_uploads(self):
        from test_core import FakeBackend, FakeProcessor
        backend = FakeBackend()
        backend.release.clear()
        service = VideoService(self.root / "store", backend, limits=Limits(queue_capacity=1), processor=FakeProcessor())
        asset = self.root / "asset"
        asset.write_bytes(b"synthetic")
        try:
            first = service.resolver.register_file(asset, self.caller)
            await service.submit(AnalysisRequest(first, "q"), self.caller)
            await backend.started.wait()
            second_caller = replace(self.caller, user="user-b")
            second = service.resolver.register_file(asset, second_caller)
            await service.submit(AnalysisRequest(second, "q"), second_caller)
            third_caller = replace(self.caller, user="user-c")
            third = service.resolver.register_file(asset, third_caller)
            with self.assertRaisesRegex(VideoError, "QUEUE_FULL"):
                await service.submit(AnalysisRequest(third, "q"), third_caller)
            self.assertEqual(backend.calls, 1)
        finally:
            await service.close()
