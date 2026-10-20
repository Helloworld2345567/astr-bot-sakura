"""HTTPS-only media downloads with DNS pinning and per-hop address validation."""
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from .models import VideoError


def validate_url(url):
    try:
        part = urlsplit(url)
        if (part.scheme != "https" or not part.hostname or part.username or part.password
                or part.port not in (None, 443) or "\\" in url or any(ord(c) < 33 for c in url)):
            raise ValueError()
    except (ValueError, TypeError):
        raise VideoError("UNSAFE_URL") from None
    return part


def public_addresses(host, port=443):
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not addresses:
        raise VideoError("UNSAFE_ADDRESS")
    for family, _, _, _, sockaddr in addresses:
        ip = ipaddress.ip_address(sockaddr[0])
        if not ip.is_global or ip.is_multicast or (ip.version == 6 and ip.ipv4_mapped is not None):
            raise VideoError("UNSAFE_ADDRESS")
    return addresses


class PublicResolver(AbstractResolver):
    async def resolve(self, host, port=443, family=socket.AF_UNSPEC):
        import asyncio
        addresses = await asyncio.to_thread(public_addresses, host, port)
        return [{"hostname": host, "host": address[0], "port": port,
                 "family": af, "proto": 0, "flags": socket.AI_NUMERICHOST}
                for af, _, _, _, address in addresses]

    async def close(self):
        pass


async def download(url, target: Path, budget: int, timeout: float, headers=None):
    """Count bytes actually received, including redirects; never trust length headers."""
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    # IP literals bypass aiohttp's resolver, so explicitly validate every URL too.
    safe_headers = {k: v for k, v in (headers or {}).items()
                    if k.lower() in ("user-agent", "referer")}
    count = 0
    try:
        async with aiohttp.ClientSession(connector=connector, trust_env=False,
                                        auto_decompress=False,
                                        timeout=aiohttp.ClientTimeout(total=timeout)) as client:
            for _ in range(6):
                part = validate_url(url)
                try:
                    ip = ipaddress.ip_address(part.hostname)
                except ValueError:
                    pass
                else:
                    if not ip.is_global or ip.is_multicast or (ip.version == 6 and ip.ipv4_mapped):
                        raise VideoError("UNSAFE_ADDRESS")
                async with client.get(url, headers=safe_headers, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        from urllib.parse import urljoin
                        url = urljoin(url, response.headers.get("Location", ""))
                        continue
                    if response.status != 200:
                        raise VideoError("DOWNLOAD_DENIED" if response.status in (401, 403) else "DOWNLOAD_FAILED")
                    with target.open("xb") as output:
                        async for chunk in response.content.iter_chunked(65536):
                            count += len(chunk)
                            if count > budget:
                                raise VideoError("MEDIA_TOO_LARGE")
                            output.write(chunk)
                    return count
            raise VideoError("TOO_MANY_REDIRECTS")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
