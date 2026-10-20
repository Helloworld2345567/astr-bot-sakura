"""Single-use yt-dlp metadata worker; replaces *all* request handlers.

No downloader, shell, remote JS, plugins, proxy environment, or external helpers.
HTTPS connections validate and pin public addresses at socket connection time.
Only sanitized stable identities leave storage; URLs below are transient fetch data.
"""
import http.client
import io
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import urllib.error
import urllib.request

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from video_understanding_core.models import VideoError
from video_understanding_core.network import public_addresses, validate_url
from video_understanding_core.sources import platform_source


class PinnedHTTPS(http.client.HTTPSConnection):
    def connect(self):
        validate_url("https://" + self.host)
        addresses = public_addresses(self.host, self.port)
        for family, kind, proto, _, address in addresses:
            raw = socket.socket(family, kind, proto)
            try:
                raw.settimeout(self.timeout)
                raw.connect(address)
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
                return
            except OSError:
                raw.close()
        raise VideoError("RESOLVE_CONNECTION_FAILED")


class SafeHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, request):
        validate_url(request.full_url)
        return self.do_open(PinnedHTTPS, request, context=ssl.create_default_context())


class RejectHandler(urllib.request.BaseHandler):
    def default_open(self, request):
        validate_url(request.full_url)


class QuietLogger:
    restricted = False
    def debug(self, *args, **kwargs): pass
    def warning(self, message, *args, **kwargs):
        if any(term in str(message).lower() for term in ("preview", "试看", "supporter-only", "drm", "paywall")):
            self.restricted = True
    def error(self, *args, **kwargs): pass
    def stdout(self, *args, **kwargs): pass


def extract(url, cookie_file=""):
    os.environ["YTDLP_NO_PLUGINS"] = "1"
    import yt_dlp
    from yt_dlp.globals import plugin_dirs
    plugin_dirs.value = []
    from yt_dlp.networking.common import RequestHandler, Response
    from yt_dlp.networking.exceptions import HTTPError

    class GuardedRH(RequestHandler):
        _SUPPORTED_URL_SCHEMES = ("https",)

        def _check_extensions(self, extensions):
            super()._check_extensions(extensions)
            for name in ("cookiejar", "timeout"):
                extensions.pop(name, None)

        def _send(self, request):
            validate_url(request.url)
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({}), RejectHandler(), SafeHTTPSHandler(),
                urllib.request.HTTPCookieProcessor(self._get_cookiejar(request)),
            )
            headers = self._get_headers(request)
            # Keep metadata reads bounded; extractors do not need compressed bodies.
            headers["Accept-Encoding"] = "identity"
            req = urllib.request.Request(request.url, data=request.data,
                                         headers=headers, method=request.method)
            try:
                reply = opener.open(req, timeout=12)
            except urllib.error.HTTPError as error:
                reply = error
            with reply:
                body = reply.read(8 * 1024**2 + 1)
                if len(body) > 8 * 1024**2:
                    raise VideoError("RESOLVE_RESPONSE_TOO_LARGE")
                response = Response(io.BytesIO(body), reply.url, dict(reply.headers), reply.status)
                if reply.status >= 400:
                    raise HTTPError(response)
                return response

    initial = platform_source(url)
    logger = QuietLogger()
    with yt_dlp.YoutubeDL({
        "quiet": True, "no_warnings": False, "logger": logger, "proxy": "",
        "noplaylist": True, "skip_download": True, "cachedir": False,
        "cookiefile": cookie_file or None, "extractor_retries": 0, "retries": 0,
        "js_runtimes": {}, "remote_components": [], "extract_flat": False,
        "allowed_extractors": ["BiliBili", "BiliBiliShortURL", "Douyin"],
        "format": "bestvideo[protocol=https][height<=720]+bestaudio[protocol=https]/best[protocol=https][height<=720]",
        "writesubtitles": False, "writeautomaticsub": False,
    }) as ydl:
        director = ydl._request_director
        director.close()
        director.preferences.clear()
        director.add_handler(GuardedRH(logger=QuietLogger(), cookiejar=ydl.cookiejar, timeout=12))
        # Expand share links through the same pinned transport, then choose the exact work.
        if "work_id" not in initial:
            from yt_dlp.networking import Request
            response = director.send(Request(initial["ref"]))
            initial = platform_source(response.url)
            response.close()
        info = ydl.extract_info(initial["ref"], download=False)
        if info.get("_type") in ("playlist", "multi_video") or info.get("is_live"):
            raise VideoError("PLAYLIST_OR_LIVE_UNSUPPORTED")
        if logger.restricted or info.get("availability") in ("premium_only", "subscriber_only", "needs_auth", "private"):
            raise VideoError("SOURCE_LOGIN_REQUIRED")
        if initial["kind"] == "bilibili":
            # BiliBiliIE's verified result ID is BV... or BV..._pN.
            work = initial["work_id"]
            if not str(info.get("id", "")).split("_", 1)[0] == work:
                raise VideoError("SOURCE_IDENTITY_MISMATCH")
            page = initial.get("page", 1)
            valid_ids = {f"{work}_p{page}"} | ({work} if page == 1 else set())
            if str(info.get("id")) not in valid_ids:
                raise VideoError("SOURCE_IDENTITY_MISMATCH")
        elif str(info.get("id")) != initial["work_id"]:
            raise VideoError("SOURCE_IDENTITY_MISMATCH")
        streams = info.get("requested_formats") or [info]
        if not 1 <= len(streams) <= 2:
            raise VideoError("UNSUPPORTED_MEDIA")
        selected = []
        for stream in streams:
            validate_url(stream["url"])
            if stream.get("protocol") != "https" or stream.get("fragments"):
                raise VideoError("SEGMENTED_MEDIA_UNSUPPORTED")
            selected.append({"url": stream["url"], "headers": {
                k: v for k, v in stream.get("http_headers", {}).items()
                if k.lower() in ("user-agent", "referer")}})
        return {"source": initial["ref"], "streams": selected}


def main():
    try:
        cookies = sys.argv[3] if len(sys.argv) == 4 and sys.argv[2] == "--cookies" else ""
        result = extract(sys.argv[1], cookies)
    except VideoError as error:
        result = {"error": error.code}
    except ImportError:
        result = {"error": "YTDLP_UNAVAILABLE"}
    except Exception as error:
        # Classify common extractor failures without emitting signed URLs/Cookies.
        text = str(error).lower()
        code = "RESOLVE_FAILED"
        for terms, candidate in [(("cookie", "login", "sign in"), "SOURCE_LOGIN_REQUIRED"),
                                  (("geo", "region"), "SOURCE_REGION_RESTRICTED"),
                                  (("404", "removed", "not found"), "SOURCE_UNAVAILABLE"),
                                  (("403", "429", "blocked", "captcha"), "SOURCE_ACCESS_RESTRICTED")]:
            if any(term in text for term in terms):
                code = candidate
                break
        result = {"error": code}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
