"""Stable platform identities, scoped opaque attachment capabilities, bounded fetching."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
from urllib.parse import parse_qs, urlsplit

from .models import VideoError
from .network import download, validate_url
from .process import run_process


@dataclass
class Attachment:
    owner: tuple
    path: Path
    expires: float
    digest: str
    url: str = ""


def platform_source(text):
    """Only recognize one work; ordinary web URLs are not media sources."""
    text = text.strip()
    if re.fullmatch(r"BV[0-9A-Za-z]{10}(?:\?p=[1-9][0-9]*)?", text):
        text = "https://www.bilibili.com/video/" + text
    parsed = validate_url(text)
    host = parsed.hostname.lower()
    if host in ("b23.tv", "v.douyin.com"):
        if not re.fullmatch(r"/[A-Za-z0-9_-]+/?", parsed.path):
            raise VideoError("UNSUPPORTED_SOURCE")
        return {"kind": "bilibili" if host == "b23.tv" else "douyin",
                "ref": f"https://{host}{parsed.path}"}
    if host in ("www.bilibili.com", "bilibili.com"):
        match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10}|av[0-9]+)/?", parsed.path)
        if not match:
            raise VideoError("UNSUPPORTED_SOURCE")
        query = parse_qs(parsed.query)
        try:
            page = int(query.get("p", ["1"])[0])
            if not 1 <= page <= 10000:
                raise ValueError()
        except ValueError:
            raise VideoError("INVALID_PAGE") from None
        return {"kind": "bilibili", "ref": f"https://www.bilibili.com/video/{match[1]}?p={page}",
                "work_id": match[1], "page": page}
    if host in ("www.douyin.com", "douyin.com", "www.iesdouyin.com"):
        match = re.fullmatch(r"/(?:video|share/video)/(\d+)/?", parsed.path)
        if match:
            return {"kind": "douyin", "ref": f"https://www.douyin.com/video/{match[1]}", "work_id": match[1]}
        if "/note/" in parsed.path:
            raise VideoError("GALLERY_NOT_VIDEO")
    raise VideoError("UNSUPPORTED_SOURCE")


class SourceResolver:
    def __init__(self, root, limits, allowed_sources=("attachment", "bilibili", "douyin"), cookie_file=""):
        self.root, self.limits = Path(root), limits
        self.root.mkdir(parents=True, exist_ok=True)
        self.attachments = {}
        self.pinned = set()
        self.allowed_sources = set(allowed_sources)
        self.cookie_file = cookie_file

    def describe(self, source_ref, caller):
        if source_ref.startswith("att_"):
            record = self.attachments.get(source_ref)
            if record is None or record.owner != caller.scope or (record.expires <= time.time() and source_ref not in self.pinned):
                raise VideoError("ATTACHMENT_UNAVAILABLE_RESEND")
            source = {"kind": "attachment", "ref": source_ref}
        else:
            source = platform_source(source_ref)
        if source["kind"] not in self.allowed_sources:
            raise VideoError("SOURCE_DISABLED")
        return source

    def register_file(self, trusted_path, caller):
        """Adapter-only API. Never expose trusted_path as a tool argument.

        Copy immediately from a verified host asset; reject symlinks/reparse points.
        Callers must establish that this path came from their platform attachment API.
        """
        self.check_storage(caller)
        path = Path(trusted_path)
        for parent in (path, *path.parents):
            attributes = getattr(parent.lstat(), "st_file_attributes", 0)
            if parent.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise VideoError("UNSAFE_ATTACHMENT_PATH")
        resolved = path.resolve(strict=True)
        before = resolved.stat(follow_symlinks=False)
        if not resolved.is_file() or before.st_size > self.limits.max_download_bytes:
            raise VideoError("MEDIA_TOO_LARGE")
        ref = "att_" + secrets.token_urlsafe(24)
        target = self.root / ref
        digest, count = hashlib.sha256(), 0
        try:
            with resolved.open("rb") as source, target.open("xb") as output:
                # Detect path substitution between validation and opening.
                opened = os.fstat(source.fileno())
                if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                    raise VideoError("UNSAFE_ATTACHMENT_PATH")
                while chunk := source.read(65536):
                    count += len(chunk)
                    if count > self.limits.max_download_bytes:
                        raise VideoError("MEDIA_TOO_LARGE")
                    output.write(chunk)
                    digest.update(chunk)
            self.attachments[ref] = Attachment(caller.scope, target, time.time() + self.limits.media_ttl,
                                               digest.hexdigest())
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return ref

    def check_storage(self, caller):
        self.cleanup()
        if sum(a.owner == caller.scope for a in self.attachments.values()) >= self.limits.daily_jobs_per_user:
            raise VideoError("ATTACHMENT_LIMIT")
        if len(self.attachments) >= self.limits.daily_total_jobs + self.limits.queue_capacity:
            raise VideoError("ATTACHMENT_LIMIT")
        used = sum(path.stat().st_size for path in self.root.iterdir() if path.is_file())
        if used + self.limits.max_download_bytes > self.limits.max_storage_bytes:
            raise VideoError("STORAGE_BUDGET_EXCEEDED")

    def register_remote(self, url, caller):
        validate_url(url)
        self.check_storage(caller)
        ref = "att_" + secrets.token_urlsafe(24)
        self.attachments[ref] = Attachment(caller.scope, self.root / ref,
            time.time() + self.limits.media_ttl, "", url)
        return ref

    async def register_url(self, url, caller):
        temporary = self.root / ("incoming_" + secrets.token_urlsafe(24))
        try:
            await download(url, temporary, self.limits.max_download_bytes, self.limits.download_timeout)
            return self.register_file(temporary, caller)
        finally:
            temporary.unlink(missing_ok=True)

    def delete_ref(self, ref, caller):
        record = self.attachments.get(ref)
        if record and record.owner == caller.scope:
            record.path.unlink(missing_ok=True)
            del self.attachments[ref]

    def cleanup(self):
        now = time.time()
        for ref, record in list(self.attachments.items()):
            if record.expires <= now and ref not in self.pinned:
                record.path.unlink(missing_ok=True)
                del self.attachments[ref]
        owned = {record.path for record in self.attachments.values()}
        # Restarted processes have no live attachment capabilities. Remove old assets.
        for path in self.root.iterdir():
            if path.is_file() and path not in owned and path.stat().st_mtime + self.limits.media_ttl <= now:
                path.unlink(missing_ok=True)

    async def resolve(self, source, caller):
        self.describe(source["ref"], caller)
        if source["kind"] == "attachment":
            record = self.attachments[source["ref"]]
            return {"paths": [record.path], "attachment": record, "source": source}
        # All extractor networking is replaced by the guarded HTTPS handler in the worker.
        worker = Path(__file__).with_name("extract_worker.py")
        args = [sys.executable, worker, source["ref"]]
        if self.cookie_file:
            args.extend(["--cookies", self.cookie_file])
        env = {k: v for k, v in os.environ.items() if k.upper() in (
            "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR")}
        # Desktop hosts may install plugin dependencies into a private site-packages
        # directory added to sys.path, rather than the interpreter's default site.
        env["PYTHONPATH"] = os.pathsep.join(str(Path(entry).resolve()) for entry in sys.path
                                          if entry and Path(entry).exists())
        raw = await run_process(args, self.limits.resolve_timeout, env=env)
        try:
            result = json.loads(raw)
            if result.get("error"):
                raise VideoError(result["error"])
            stable = platform_source(result["source"])
            if stable["kind"] != source["kind"] or (source.get("work_id") and
                    source["work_id"] != stable.get("work_id")) or (
                    source.get("page") and source["page"] != stable.get("page")):
                raise VideoError("SOURCE_IDENTITY_MISMATCH")
            return {"source": stable, "streams": result["streams"]}
        except (KeyError, TypeError, ValueError):
            raise VideoError("RESOLVE_FAILED") from None

    async def fetch(self, resolved, directory, processor):
        if "paths" in resolved:
            record = resolved["attachment"]
            if not record.path.exists() and record.url:
                await download(record.url, record.path, self.limits.max_download_bytes, self.limits.download_timeout)
            return resolved["paths"][0]
        streams, remaining, paths = resolved["streams"], self.limits.max_download_bytes, []
        if not 1 <= len(streams) <= 2:
            raise VideoError("UNSUPPORTED_MEDIA")
        for index, stream in enumerate(streams):
            target = directory / f"stream-{index}.media"
            used = await download(stream["url"], target, remaining, self.limits.download_timeout,
                                  stream.get("headers"))
            remaining -= used
            paths.append(target)
        return await processor.merge(*paths, directory) if len(paths) == 2 else paths[0]
