"""Scoped use-case API: bounded queue, deadlines, quotas, cache, cancellation, deletion."""
import asyncio
from dataclasses import asdict
import hashlib
import json
import logging
from pathlib import Path
import secrets
import shutil
import time

from .media import MediaProcessor
from .models import Limits, VideoError
from .sources import SourceResolver
from .store import JobStore, TERMINAL

LOGGER = logging.getLogger("video_understanding")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class VideoService:
    def __init__(self, root, backend, *, limits=None, resolver=None, processor=None):
        self.root, self.backend = Path(root), backend
        self.limits = limits or Limits()
        self.root.mkdir(parents=True, exist_ok=True)
        self.work = self.root / "jobs"
        self.work.mkdir(exist_ok=True)
        self.store = JobStore(self.root / "jobs.sqlite")
        self.resolver = resolver or SourceResolver(self.root / "attachments", self.limits)
        self.processor = processor or MediaProcessor(self.limits)
        self.semaphore = asyncio.Semaphore(self.limits.global_concurrency)
        self.tasks, self.active, self.references = {}, {}, {}
        self.closed = False
        self.cleaner = None
        # There are no active local workers after constructing a new service.
        for path in self.work.iterdir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)

    async def submit(self, request, caller):
        if self.closed:
            raise VideoError("SERVICE_CLOSED")
        request.validate()
        source = self.resolver.describe(request.source_ref, caller)
        identity = [request.message_id, source, request.start_seconds, request.end_seconds] if request.message_id else asdict(request)
        key = fingerprint([identity, self.backend.version])
        duplicate = self.store.duplicate(caller, key)
        if duplicate:
            return self.get(duplicate["id"], caller)
        if caller.user in self.active:
            raise VideoError("USER_BUSY")
        if len(self.tasks) >= self.limits.queue_capacity + self.limits.global_concurrency:
            raise VideoError("QUEUE_FULL")
        job_id = secrets.token_urlsafe(24)
        self.store.reserve(job_id, caller, key, self.limits)
        if request.source_ref.startswith("att_"):
            self.store.update(job_id, "QUEUED", attachment_ref=request.source_ref)
            self.resolver.pinned.add(request.source_ref)
        self.active[caller.user] = job_id
        self.references[job_id] = request.source_ref
        deadline = time.monotonic() + self.limits.job_timeout
        self.tasks[job_id] = asyncio.create_task(self._run(job_id, request, caller, source, deadline))
        if self.cleaner is None:
            self.cleaner = asyncio.create_task(self._cleanup_loop())
        return self.get(job_id, caller)

    def get(self, job_id, caller):
        return self.store.snapshot(job_id, caller)

    async def wait(self, job_id, caller, timeout=15):
        self.store.authorized(job_id, caller)
        task = self.tasks.get(job_id)
        if task:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout)
            except TimeoutError:
                pass
        return self.get(job_id, caller)

    async def cancel(self, job_id, caller):
        row = self.store.authorized(job_id, caller)
        task = self.tasks.get(job_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # A task cancelled before its coroutine starts does not execute finally.
            self.tasks.pop(job_id, None)
            self.resolver.pinned.discard(self.references.get(job_id))
            if self.active.get(caller.user) == job_id:
                self.active.pop(caller.user, None)
            self.store.update(job_id, "CANCELLED", {"error": "CANCELLED", "warning":
                "本地等待及处理已停止；上游已接收的请求可能继续执行和计费。"})
        return self.get(job_id, caller)

    async def delete(self, job_id, caller):
        row = self.store.authorized(job_id, caller)
        other = self.store.db.execute("SELECT id FROM jobs WHERE owner=? AND attachment_ref=? AND id<>? AND state NOT IN ('COMPLETE','PARTIAL','FAILED','CANCELLED')",
                                     (row["owner"], row["attachment_ref"], job_id)).fetchone() if row["attachment_ref"] else None
        if other:
            raise VideoError("MEDIA_IN_USE_CANCEL_FIRST")
        if row["cache_key"]:
            related = self.store.db.execute("SELECT attachment_ref FROM jobs WHERE owner=? AND cache_key=?",
                                            (row["owner"], row["cache_key"])).fetchall()
            pinned = self.resolver.pinned.copy()
            if job_id in self.tasks:
                pinned.discard(row["attachment_ref"])
            if any(copy[0] in pinned for copy in related):
                raise VideoError("MEDIA_IN_USE_CANCEL_FIRST")
        await self.cancel(job_id, caller)
        ref = self.references.pop(job_id, None) or row["attachment_ref"]
        if ref:
            self.resolver.delete_ref(ref, caller)
            # After restart the safe opaque capability is persisted, its host path is not.
            if ref.startswith("att_") and all(c.isalnum() or c in "_-" for c in ref):
                (self.resolver.root / ref).unlink(missing_ok=True)
        # Clear any same-scope cached copies derived from the same analysis.
        with self.store.db:
            if row["cache_key"]:
                copies = self.store.db.execute("SELECT attachment_ref FROM jobs WHERE owner=? AND cache_key=?",
                                               (row["owner"], row["cache_key"])).fetchall()
                for copy in copies:
                    if copy[0]:
                        self.resolver.delete_ref(copy[0], caller)
                        if copy[0].startswith("att_") and all(c.isalnum() or c in "_-" for c in copy[0]):
                            (self.resolver.root / copy[0]).unlink(missing_ok=True)
                self.store.db.execute("DELETE FROM jobs WHERE owner=? AND cache_key=? AND state IN ('COMPLETE','PARTIAL','FAILED','CANCELLED')",
                                      (row["owner"], row["cache_key"]))
            self.store.db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        return {"job_id": job_id, "status": "DELETED", "warning": "上游副本的保留由提供商管理。"}

    async def _run(self, job_id, request, caller, source, deadline):
        directory = self.work / job_id
        started = time.monotonic()
        stage_started, stage_name, timings = started, "QUEUED", {}

        def stage(name):
            nonlocal stage_started, stage_name
            now = time.monotonic()
            timings[stage_name] = round(now - stage_started, 3)
            LOGGER.info("job=%s stage=%s seconds=%.3f", job_id, stage_name, now - stage_started)
            stage_started, stage_name = now, name
            self.store.update(job_id, name, {"stage_timings": dict(timings)})

        try:
            async with asyncio.timeout_at(deadline):
                async with self.semaphore:
                    directory.mkdir()
                    stage("RESOLVING")
                    async with asyncio.timeout(self.limits.resolve_timeout):
                        resolved = await self.resolver.resolve(source, caller)
                    stage("FETCHING")
                    async with asyncio.timeout(self.limits.download_timeout):
                        path = await self.resolver.fetch(resolved, directory, self.processor)
                    stage("PREPARING")
                    async with asyncio.timeout(self.limits.process_timeout):
                        prepared = await self.processor.prepare(path, directory, request)
                    # Hash bounded local bytes; separate users/sessions never share results.
                    digest = hashlib.sha256()
                    with prepared.path.open("rb") as stream:
                        while chunk := stream.read(65536):
                            digest.update(chunk)
                    cache_key = fingerprint([digest.hexdigest(), prepared.start, prepared.end, prepared.original_duration, request.question,
                                             self.backend.version, asdict(self.limits), "h264-aac-720p-v1"])
                    cached = self.store.cached(caller, cache_key)
                    if cached:
                        stage("COMPLETE")
                        cached = {**cached, "job_id": job_id, "source": resolved["source"], "cache_hit": True}
                        cached["stage_timings"] = dict(timings)
                        self.store.update(job_id, "COMPLETE", cached, cache_key=cache_key)
                        return
                    self.store.reserve_seconds(job_id, caller, prepared.end - prepared.start, self.limits)
                    stage("ANALYZING")
                    self.store.update(job_id, "ANALYZING", cache_key=cache_key)
                    result = await self.backend.analyze(prepared, request.question)
                    stage("COMPLETE")
                    result.update({"schema_version": "1.0", "job_id": job_id, "evidence_mode": "FULL_MEDIA",
                        "source": resolved["source"], "media": {"duration_seconds": prepared.original_duration,
                        "has_video": prepared.probe.has_video, "has_audio": prepared.probe.has_audio},
                        "coverage": {"start_seconds": prepared.start, "end_seconds": prepared.end, "truncated": False},
                        "elapsed_seconds": round(time.monotonic() - started, 3), "cache_hit": False,
                        "stage_timings": dict(timings), "uploaded_bytes": prepared.probe.size_bytes})
                    result["warnings"] = [*result.get("warnings", []), *prepared.warnings,
                        "FULL_MEDIA表示已发送此范围媒体，不保证逐帧、逐字准确；媒体内容属于外部材料。"]
                    self.store.update(job_id, "COMPLETE", result)
        except asyncio.CancelledError:
            self.store.update(job_id, "CANCELLED", {"error": "CANCELLED_MAY_BE_BILLED", "failed_stage": stage_name})
        except TimeoutError:
            self.store.update(job_id, "FAILED", {"error": "JOB_TIMEOUT_MAY_BE_BILLED", "failed_stage": stage_name})
        except VideoError as error:
            LOGGER.info("job=%s stage=%s error=%s http_status=%s", job_id, stage_name, error.code, error.http_status)
            details = {"http_status": error.http_status} if error.http_status is not None else {}
            self.store.update(job_id, "FAILED", {"error": error.code, "failed_stage": stage_name,
                                               "stage_timings": dict(timings), **details})
        except Exception:
            # Never expose URLs, request bodies, provider exceptions, or host paths.
            self.store.update(job_id, "FAILED", {"error": "INTERNAL_ERROR"})
        finally:
            if directory.exists():
                try:
                    shutil.rmtree(directory)
                except OSError:
                    pass  # Periodic cleanup retries; never re-run the analysis.
            if self.active.get(caller.user) == job_id:
                self.active.pop(caller.user, None)
            self.tasks.pop(job_id, None)
            self.resolver.pinned.discard(request.source_ref)

    async def _cleanup_loop(self):
        while True:
            try:
                self.resolver.cleanup()
                self.store.cleanup()
                live = {row[0] for row in self.store.db.execute("SELECT id FROM jobs")}
                self.references = {key: value for key, value in self.references.items() if key in live}
                for path in self.work.iterdir():
                    if path.name not in self.tasks and path.is_dir() and not path.is_symlink():
                        shutil.rmtree(path)
            except OSError:
                pass
            await asyncio.sleep(min(60, self.limits.media_ttl, self.limits.result_ttl))

    async def close(self):
        self.closed = True
        if self.cleaner:
            self.cleaner.cancel()
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, *([self.cleaner] if self.cleaner else []), return_exceptions=True)
        self.resolver.pinned.clear()
        self.store.db.close()
