import asyncio
import base64
from dataclasses import replace
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from support import result_payload
from video_understanding_core.backend import BackendConfig, ChatCompletionsGemini
from video_understanding_core.media import MediaProcessor
from video_understanding_core.models import AnalysisRequest, Limits, VideoError
from video_understanding_core.network import download
from video_understanding_core.process import run_process


class Chunks:
    def __init__(self, chunks): self.chunks = chunks
    async def iter_chunked(self, _):
        for chunk in self.chunks:
            yield chunk


class Response:
    def __init__(self, body=b"", *, status=200, headers=None):
        self.status, self.headers = status, headers or {}
        self.content = Chunks([body] if isinstance(body, bytes) else body)
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass


class Session:
    def __init__(self, responses):
        self.responses, self.sent = list(responses), []
    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    def post(self, url, **kwargs):
        self.sent.append((url, kwargs))
        return self.responses.pop(0)
    def get(self, url, **kwargs):
        self.sent.append((url, kwargs))
        return self.responses.pop(0)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
class RealMediaTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.voiced, cls.silent = cls.root / "voiced.mp4", cls.root / "silent.bin"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        "color=c=blue:size=160x120:rate=10:duration=3", "-f", "lavfi", "-i",
                        "sine=frequency=800:sample_rate=44100:duration=3", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(cls.voiced)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(cls.voiced), "-an",
                        "-c:v", "copy", "-f", "mp4", str(cls.silent)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    async def asyncSetUp(self):
        self.directory = Path(tempfile.mkdtemp(dir=self.root))
        self.limits = Limits()
        self.processor = MediaProcessor(self.limits)

    async def test_probe_real_container_and_audio_without_extension(self):
        voiced, silent = await self.processor.probe(self.voiced), await self.processor.probe(self.silent)
        self.assertTrue(voiced.has_audio)
        self.assertFalse(silent.has_audio)
        self.assertTrue(silent.has_video)

    async def test_unchanged_mp4_is_not_reencoded(self):
        media = await self.processor.prepare(self.voiced, self.directory, AnalysisRequest("att", "q"))
        self.assertEqual(media.path, self.voiced)
        self.assertTrue(media.probe.has_audio)

    async def test_range_transcode_retains_audio_and_original_time(self):
        media = await self.processor.prepare(self.voiced, self.directory, AnalysisRequest("att", "q", 1, 2))
        self.assertTrue(media.probe.has_audio)
        self.assertAlmostEqual(media.probe.duration_seconds, 1, delta=.1)
        self.assertEqual((media.start, media.end), (1, 2))
        self.assertAlmostEqual(media.original_duration, 3, delta=.1)
        self.assertTrue(media.warnings)

    async def test_long_and_invalid_ranges_fail_without_upload(self):
        self.processor.limits = replace(self.limits, max_analysis_seconds=1)
        with self.assertRaisesRegex(VideoError, "RANGE_REQUIRED"):
            await self.processor.prepare(self.voiced, self.directory, AnalysisRequest("att", "q"))
        with self.assertRaisesRegex(VideoError, "INVALID_RANGE"):
            await self.processor.prepare(self.voiced, self.directory, AnalysisRequest("att", "q", 0, 100))

    async def test_too_small_budget_fails_without_dropping_audio(self):
        self.processor.limits = replace(self.limits, max_inline_bytes=1000)
        with self.assertRaisesRegex(VideoError, "MEDIA_TOO_LARGE"):
            await self.processor.prepare(self.voiced, self.directory, AnalysisRequest("att", "q"))

    async def test_separate_video_audio_merge_keeps_duration_and_both_streams(self):
        audio = self.directory / "audio.m4a"
        await run_process(["ffmpeg", "-nostdin", "-v", "error", "-i", self.voiced,
                           "-vn", "-c:a", "copy", audio], 10)
        merged = await self.processor.merge(self.silent, audio, self.directory)
        probe = await self.processor.probe(merged)
        self.assertTrue(probe.has_audio)
        self.assertTrue(probe.has_video)
        self.assertAlmostEqual(probe.duration_seconds, 3, delta=.1)

    async def test_playlist_container_cannot_reference_other_host_files(self):
        playlist = self.directory / "fake-video.mp4"
        playlist.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:3\n#EXTINF:3,\n" + str(self.voiced) + "\n#EXT-X-ENDLIST\n", encoding="utf-8")
        with self.assertRaises(VideoError):
            await self.processor.probe(playlist)

    async def test_actual_outbound_contract_carries_entire_mp4_and_audio(self):
        media = await self.processor.prepare(self.voiced, self.directory, AnalysisRequest("att", "q"))
        backend = ChatCompletionsGemini(BackendConfig("https://provider.example.org/v1", "test-secret", "test-gemini"), self.limits)
        reply = json.dumps({"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps(result_payload())}}]}).encode()
        session = Session([Response(reply)])
        with patch("video_understanding_core.backend.aiohttp.ClientSession", return_value=session):
            result = await backend.analyze(media, "声音与画面？")
        url, sent = session.sent[0]
        self.assertEqual(url, "https://provider.example.org/v1/chat/completions")
        payload = json.loads(sent["data"])
        contents = payload["messages"][0]["content"]
        self.assertEqual(contents[1]["type"], "image_url")
        encoded = contents[1]["image_url"]["url"]
        self.assertTrue(encoded.startswith("data:video/mp4;base64,"))
        self.assertEqual(base64.b64decode(encoded.split(",", 1)[1]), self.voiced.read_bytes())
        self.assertIn("音轨=True", contents[0]["text"])
        self.assertNotIn("response_format", payload)
        self.assertFalse(sent["allow_redirects"])
        self.assertIsNone(result["usage"]["input_tokens"])

    async def test_response_errors_and_no_retry(self):
        media = await self.processor.prepare(self.voiced, self.directory, AnalysisRequest("att", "q"))
        backend = ChatCompletionsGemini(BackendConfig("https://provider.example.org/v1", "test-secret", "m"), self.limits)
        cases = [(Response(status=429), "PROVIDER_RATE_LIMITED"),
                 (Response(status=401), "PROVIDER_AUTH_FAILED"),
                 (Response(b"not-json"), "INVALID_MODEL_RESULT"),
                 (Response(json.dumps({"choices": [{"finish_reason": "length"}]}).encode()), "MODEL_OUTPUT_TRUNCATED"),
                 (Response(json.dumps({"choices": [{"finish_reason": "stop", "message": {
                     "content": '{"received_media": false}'}}]}).encode()), "MEDIA_NOT_RECEIVED")]
        for response, code in cases:
            session = Session([response])
            with self.subTest(code=code), patch("video_understanding_core.backend.aiohttp.ClientSession", return_value=session), self.assertRaisesRegex(VideoError, code):
                await backend.analyze(media, "q")
            self.assertEqual(len(session.sent), 1)

    async def test_base64_request_budget_checked_before_http(self):
        media = await self.processor.prepare(self.voiced, self.directory, AnalysisRequest("att", "q"))
        backend = ChatCompletionsGemini(BackendConfig("https://provider.example.org/v1", "test-secret", "m"),
                                        replace(self.limits, max_request_bytes=100))
        with self.assertRaisesRegex(VideoError, "MEDIA_TOO_LARGE"):
            backend.payload(media, "q")


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.target = Path(self.temp.name) / "file"
    async def asyncTearDown(self): self.temp.cleanup()

    async def test_actual_bytes_enforce_budget_despite_false_content_length(self):
        session = Session([Response([b"123", b"456"], headers={"Content-Length": "1"})])
        with patch("video_understanding_core.network.aiohttp.ClientSession", return_value=session), self.assertRaisesRegex(VideoError, "MEDIA_TOO_LARGE"):
            await download("https://media.example.org/a", self.target, 5, 1)
        self.assertFalse(self.target.exists())

    async def test_redirect_to_loopback_never_connects(self):
        session = Session([Response(status=302, headers={"Location": "https://127.0.0.1/private"})])
        with patch("video_understanding_core.network.aiohttp.ClientSession", return_value=session), self.assertRaisesRegex(VideoError, "UNSAFE_ADDRESS"):
            await download("https://media.example.org/a", self.target, 50, 1)
        self.assertEqual(len(session.sent), 1)

    async def test_cookie_and_authorization_not_forwarded_to_download(self):
        session = Session([Response(b"ok")])
        with patch("video_understanding_core.network.aiohttp.ClientSession", return_value=session):
            await download("https://media.example.org/a", self.target, 5, 1,
                           {"Cookie": "secret", "Authorization": "secret", "Referer": "https://example.org"})
        self.assertEqual(session.sent[0][1]["headers"], {"Referer": "https://example.org"})

    async def test_bounded_process_timeout_and_output(self):
        import sys
        with self.assertRaises(TimeoutError):
            await run_process([sys.executable, "-c", "import time; time.sleep(5)"], .05)
        with self.assertRaisesRegex(VideoError, "PROCESS_OUTPUT_TOO_LARGE"):
            await run_process([sys.executable, "-c", "print('x' * 10000)"], 5, output_limit=10)

    async def test_extractor_socket_pins_public_ip_and_keeps_hostname_for_tls(self):
        from video_understanding_core.extract_worker import PinnedHTTPS
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("8.8.8.8", 443))]
        with patch("video_understanding_core.extract_worker.public_addresses", return_value=answers), patch(
                "video_understanding_core.extract_worker.socket.socket") as sock, patch("ssl.create_default_context") as context:
            connection = PinnedHTTPS("media.example.org", context=context())
            connection.connect()
            sock.return_value.connect.assert_called_once_with(("8.8.8.8", 443))
            context.return_value.wrap_socket.assert_called_once_with(sock.return_value, server_hostname="media.example.org")


import socket
