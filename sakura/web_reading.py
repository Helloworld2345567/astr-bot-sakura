"""Prepare supported public article/video-page URLs for Tavily extraction."""
import asyncio
import re
from urllib.parse import urljoin, urlsplit

import aiohttp


def _allowed_douyin_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in {"v.douyin.com", "www.douyin.com", "douyin.com", "www.iesdouyin.com", "iesdouyin.com"}
            and parsed.port in {None, 443}
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


async def _resolve_douyin_short_url(url: str) -> str:
    current = url
    # A short-link request must not turn into arbitrary local/network browsing.
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=8), trust_env=True,
        headers={"User-Agent": "Mozilla/5.0"},
    ) as session:
        for _ in range(4):
            if not _allowed_douyin_url(current):
                return url
            parsed = urlsplit(current)
            match = re.fullmatch(r"/(?:share/)?video/(\d+)/?", parsed.path)
            if parsed.hostname in {"www.douyin.com", "douyin.com", "www.iesdouyin.com", "iesdouyin.com"} and match:
                return f"https://www.douyin.com/video/{match.group(1)}"
            async with session.get(current, allow_redirects=False) as response:
                if response.status not in {301, 302, 303, 307, 308}:
                    return url
                location = response.headers.get("Location")
                if not location:
                    return url
                current = urljoin(current, location)
    return url


async def prepare_extract_args(args: dict) -> None:
    url = str(args.get("url", "")).strip()
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return
    if host == "zhihu.com" or host.endswith(".zhihu.com"):
        args["extract_depth"] = "advanced"
    elif host == "douyin.com" or host.endswith(".douyin.com") or host in {"www.iesdouyin.com", "iesdouyin.com"}:
        args["extract_depth"] = "advanced"
        if host in {"v.douyin.com", "www.iesdouyin.com", "iesdouyin.com"} and _allowed_douyin_url(url):
            try:
                # Bound the entire redirect chain, not just one HTTP request.
                args["url"] = await asyncio.wait_for(_resolve_douyin_short_url(url), 9)
            except (TimeoutError, aiohttp.ClientError, ValueError):
                # Preserve the URL for Tavily to attempt its own resolution.
                args["url"] = url
