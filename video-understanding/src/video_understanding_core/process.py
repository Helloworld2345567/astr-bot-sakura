"""Bounded subprocess execution; no shell, inherited input, or raw error logs."""
import asyncio
import os

from .models import VideoError


async def run_process(args, timeout, *, output_limit=2 * 1024**2, env=None):
    options = {"creationflags": 0x08000000} if os.name == "nt" else {}
    try:
        proc = await asyncio.create_subprocess_exec(
            *map(str, args), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=env, **options,
        )
    except FileNotFoundError:
        raise VideoError("DEPENDENCY_UNAVAILABLE") from None

    async def read():
        chunks, count = [], 0
        while chunk := await proc.stdout.read(65536):
            count += len(chunk)
            if count > output_limit:
                raise VideoError("PROCESS_OUTPUT_TOO_LARGE")
            chunks.append(chunk)
        await proc.wait()
        if proc.returncode:
            raise VideoError("MEDIA_PROCESS_FAILED")
        return b"".join(chunks)

    try:
        async with asyncio.timeout(timeout):
            return await read()
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
