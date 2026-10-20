#!/usr/bin/env python3
"""Call the configured OpenAI-compatible image API directly.

Credentials are read from an environment variable. Edit requests may repeat
--image to send multiple multipart fields with the same name.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import io
import json
import mimetypes
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from PIL import Image, ImageOps

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-image-1"
MAX_IMAGE_BYTES = 24 * 1024 * 1024
MAX_MULTIPART_BYTES = 22 * 1024 * 1024
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
TRANSIENT_STATUSES = {502, 503}
SUPPORTED_INPUT_TYPES = {"image/png", "image/jpeg", "image/webp"}
EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


def normalize_base_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("base URL must be an HTTPS URL without credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("base URL must not contain a query or fragment")
    return value if parsed.path.rstrip("/").endswith("/v1") else f"{value}/v1"


def read_api_key(env_name: str) -> str:
    key = os.environ.get(env_name, "").strip()
    if key:
        return key
    key = getpass.getpass(f"{env_name} (hidden input): ").strip()
    if not key:
        raise ValueError(f"{env_name} is required")
    return key


def redact(value: object, secret: str) -> str:
    return str(value).replace(secret, "[REDACTED]") if secret else str(value)


def detect_content_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("response is not PNG, JPEG, or WebP")


def input_content_type(path: Path) -> str:
    if path.stat().st_size == 0 or path.stat().st_size > MAX_UPLOAD_BYTES:
        raise ValueError(f"input image must be between 1 byte and {MAX_UPLOAD_BYTES} bytes: {path}")
    content_type = mimetypes.guess_type(path.name)[0] or ""
    if content_type not in SUPPORTED_INPUT_TYPES:
        content_type = detect_content_type(path.read_bytes())
    if content_type not in SUPPORTED_INPUT_TYPES:
        raise ValueError(f"unsupported input image type: {path}")
    detect_content_type(path.read_bytes())
    return content_type


def safe_slug(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return value or "image"


def decode_item(item: object, session: requests.Session) -> tuple[bytes, str]:
    if not isinstance(item, dict):
        raise ValueError("image item is not an object")
    encoded = item.get("b64_json")
    if isinstance(encoded, str) and encoded:
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("invalid b64_json image") from exc
    else:
        image_url = item.get("url")
        if not isinstance(image_url, str) or not image_url:
            raise ValueError("image item has neither b64_json nor url")
        parsed = urlparse(image_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("returned image URL must be HTTPS")
        response = session.get(image_url, timeout=60, allow_redirects=False, stream=True)
        if response.status_code != 200:
            raise ValueError(f"image download returned HTTP {response.status_code}")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            total += len(chunk)
            if total > MAX_IMAGE_BYTES:
                raise ValueError("downloaded image exceeds the 24 MiB limit")
            chunks.append(chunk)
        data = b"".join(chunks)
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("image size is invalid")
    return data, detect_content_type(data)


def response_items(response: requests.Response, session: requests.Session) -> list[tuple[bytes, str]]:
    content_type = (response.headers.get("content-type") or "").split(";", 1)[0].lower()
    if content_type in SUPPORTED_INPUT_TYPES or response.content[:12].startswith((b"\x89PNG", b"\xff\xd8\xff", b"RIFF")):
        data = response.content
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("response image exceeds the 24 MiB limit")
        return [(data, detect_content_type(data))]
    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError("successful response is not JSON or an image") from exc
    items = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        raise ValueError("response contains no image data")
    return [decode_item(item, session) for item in items]


def request_with_retry(session: requests.Session, method: str, url: str, *, headers: dict[str, str], timeout: float, **kwargs: object) -> requests.Response:
    files = kwargs.get("files") or []
    streams = [(part[1][1], part[1][1].tell()) for part in files]
    for attempt in range(2):
        if attempt:
            for handle, offset in streams:
                handle.seek(offset)
        response = session.request(method, url, headers=headers, timeout=timeout, allow_redirects=False, **kwargs)
        if response.status_code not in TRANSIENT_STATUSES or attempt == 1:
            return response
        response.close()
        time.sleep(0.25)
    raise AssertionError("unreachable")


def _as_jpeg(data: bytes, quality: int) -> bytes:
    """Normalize a provider response to a real JPEG, even if it returned PNG."""
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        image = ImageOps.exif_transpose(source)
        if "A" in image.getbands() or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")
        output = io.BytesIO()
        image.save(output, format="JPEG", quality=quality, optimize=True)
        return output.getvalue()


def save_images(
    images: list[tuple[bytes, str]],
    output_dir: Path,
    stem: str,
    *,
    output_format: str = "jpeg",
    output_compression: int | None = None,
) -> list[Path]:
    if sum(len(data) for data, _ in images) > MAX_IMAGE_BYTES:
        raise ValueError("combined output images exceed the 24 MiB limit")
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    paths: list[Path] = []
    for index, (data, content_type) in enumerate(images, 1):
        if output_format == "jpeg":
            data = _as_jpeg(data, output_compression if output_compression is not None else 96)
            content_type = "image/jpeg"
        suffix = EXTENSIONS[content_type]
        path = output_dir / f"{safe_slug(stem)}-{stamp}-{index}{suffix}"
        path.write_bytes(data)
        paths.append(path)
    return paths


def common_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prompt", required=True, help="English image prompt")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--api-key-env", default="SAKURA_IMAGE_API_KEY", help="environment variable containing the image provider API key")
    parser.add_argument("--output-dir", type=Path, default=Path("assets"))
    parser.add_argument("--output-format", choices=("png", "jpeg", "webp"), default="jpeg")
    parser.add_argument("--output-compression", type=int)
    parser.add_argument("--background", choices=("auto", "opaque", "transparent"))
    parser.add_argument("--n", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--record", type=Path, help="optional JSON request/result record")
    parser.add_argument("--dry-run", action="store_true")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Direct OpenAI-compatible image generation and editing")
    subparsers = parser.add_subparsers(dest="operation", required=True)
    generate = subparsers.add_parser("generate")
    common_parser(generate)
    edit = subparsers.add_parser("edit")
    common_parser(edit)
    edit.add_argument("--image", action="append", required=True, type=Path, help="repeat for multiple image fields")
    edit.add_argument("--mask", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.n <= 10:
        print("n must be between 1 and 10", file=sys.stderr)
        return 2
    if args.output_compression is not None and not 0 <= args.output_compression <= 100:
        print("output-compression must be between 0 and 100", file=sys.stderr)
        return 2
    if args.output_format == "png" and args.output_compression is not None:
        print("output-compression is supported only for JPEG or WebP", file=sys.stderr)
        return 2
    try:
        base_url = normalize_base_url(args.base_url)
        api_key = read_api_key(args.api_key_env) if not args.dry_run else "[DRY-RUN]"
        if args.operation == "edit":
            for path in args.image:
                input_content_type(path)
            if args.mask:
                if input_content_type(args.mask) != "image/png":
                    raise ValueError("mask must be PNG")
            total_input_bytes = sum(path.stat().st_size for path in args.image)
            if args.mask:
                total_input_bytes += args.mask.stat().st_size
            if total_input_bytes > MAX_MULTIPART_BYTES:
                raise ValueError("combined multipart inputs exceed the 22 MiB compatibility limit")
        if args.background == "transparent" and args.output_format == "jpeg":
            raise ValueError("transparent background requires PNG or WebP")
    except (OSError, ValueError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    body = {"model": args.model, "prompt": args.prompt, "n": args.n}
    for key in ("output_format", "background"):
        value = getattr(args, key)
        if value is not None:
            body[key] = value
    if args.output_format in ("jpeg", "webp"):
        body["output_compression"] = (
            args.output_compression if args.output_compression is not None else 96
        )
    endpoint = f"{base_url}/images/generations" if args.operation == "generate" else f"{base_url}/images/edits"
    record: dict[str, object] = {
        "operation": args.operation,
        "endpoint": endpoint,
        "api_key_env": args.api_key_env,
        "model": args.model,
        "prompt": args.prompt,
        "options": body,
        "requested_count": args.n,
        "images": [str(path) for path in getattr(args, "image", [])],
        "mask": str(args.mask) if getattr(args, "mask", None) else None,
    }
    if args.dry_run:
        if args.operation == "edit":
            record["multipart_image_fields"] = len(args.image)
        dry_record = {"verdict": "PASS", "dry_run": True, **record}
        if args.record:
            args.record.parent.mkdir(parents=True, exist_ok=True)
            args.record.write_text(json.dumps(dry_record, indent=2), encoding="utf-8")
        print(json.dumps(dry_record, indent=2))
        return 0

    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    session = requests.Session()
    opened = []
    try:
        if args.operation == "generate":
            response = request_with_retry(session, "POST", endpoint, headers={**headers, "Content-Type": "application/json"}, timeout=args.timeout, json=body)
        else:
            data = {key: str(value) for key, value in body.items()}
            opened = [(path.open("rb"), path) for path in args.image]
            multipart = [("image", (path.name, handle, input_content_type(path))) for handle, path in opened]
            if args.mask:
                mask_handle = args.mask.open("rb")
                opened.append((mask_handle, args.mask))
                multipart.append(("mask", (args.mask.name, mask_handle, "image/png")))
            response = request_with_retry(session, "POST", endpoint, headers=headers, timeout=args.timeout, data=data, files=multipart)
        record["http_status"] = response.status_code
        if response.status_code != 200:
            try:
                error_payload = response.json()
                record["error"] = redact(error_payload, api_key)
            except ValueError:
                record["error_bytes"] = len(response.content)
            record["verdict"] = "BLOCKED" if response.status_code >= 500 or response.status_code in (401, 429, 504) else "FAIL"
            print(json.dumps(record, indent=2))
            return 1
        images = response_items(response, session)
        paths = save_images(
            images,
            args.output_dir,
            args.model,
            output_format=args.output_format,
            output_compression=args.output_compression,
        )
        record["outputs"] = [str(path.resolve()) for path in paths]
        record["returned_count"] = len(paths)
        record["verdict"] = "PASS"
        print(json.dumps(record, indent=2))
        return 0
    except (OSError, ValueError, requests.RequestException) as exc:
        record["verdict"] = "BLOCKED" if isinstance(exc, requests.RequestException) else "FAIL"
        record["error"] = redact(f"{type(exc).__name__}: {exc}", api_key)
        print(json.dumps(record, indent=2))
        return 1
    finally:
        for handle, _ in opened:
            handle.close()
        if args.record:
            args.record.parent.mkdir(parents=True, exist_ok=True)
            args.record.write_text(json.dumps(record, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
