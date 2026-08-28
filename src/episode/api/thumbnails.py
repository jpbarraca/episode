from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import subprocess
import uuid
from collections import defaultdict
from pathlib import Path

from episode.domain.models import Evidence

logger = logging.getLogger(__name__)

_THUMBNAIL_PROFILE = "jpeg-480x320-v1"
_THUMBNAIL_WIDTH = 480
_THUMBNAIL_HEIGHT = 320
_RENDER_TIMEOUT_SECONDS = 20


class ThumbnailCache:
    """Build disposable presentation thumbnails from immutable Evidence."""

    def __init__(self, cache_dir: str | Path) -> None:
        self._cache_dir = Path(cache_dir)
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._generation_slots = asyncio.Semaphore(2)

    async def get_or_create(self, evidence: Evidence) -> str | None:
        source = Path(evidence.file_path)
        if not self._is_supported(evidence) or not source.is_file():
            return None

        key = self._cache_key(evidence)
        cache_path = self._cache_dir / f"{key}.jpg"
        async with self._locks[key]:
            if self._is_cached(cache_path):
                return str(cache_path)
            return await self._generate(source, cache_path)

    async def discard(self, evidence: Evidence) -> None:
        key = self._cache_key(evidence)
        async with self._locks[key]:
            try:
                (self._cache_dir / f"{key}.jpg").unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove thumbnail for Evidence %s", evidence.id)
                raise

    @staticmethod
    def _is_supported(evidence: Evidence) -> bool:
        return evidence.mime_type.startswith(("image/", "video/")) or (
            evidence.evidence_type == "recording" and evidence.metadata.get("format") == "hls-fmp4"
        )

    @staticmethod
    def _cache_key(evidence: Evidence) -> str:
        identity = evidence.sha256 or evidence.id
        value = f"{_THUMBNAIL_PROFILE}:{identity}"
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def _is_cached(path: Path) -> bool:
        try:
            return path.is_file() and path.stat().st_size > 0
        except OSError:
            return False

    async def _generate(self, source: Path, cache_path: Path) -> str | None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        temporary_path = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.part")
        try:
            async with self._generation_slots:
                rendered = await self._render(source, temporary_path)
            if not rendered or not self._is_cached(temporary_path):
                return None
            os.replace(temporary_path, cache_path)
            return str(cache_path)
        finally:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove temporary thumbnail %s", temporary_path)

    @staticmethod
    async def _render(source: Path, destination: Path) -> bool:
        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                (
                    f"scale={_THUMBNAIL_WIDTH}:{_THUMBNAIL_HEIGHT}:"
                    "force_original_aspect_ratio=decrease"
                ),
                "-q:v",
                "4",
                "-f",
                "image2",
                str(destination),
                stdout=subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("FFmpeg is unavailable; cannot generate Evidence thumbnails")
            return False

        try:
            _, stderr = await asyncio.wait_for(
                process.communicate(), timeout=_RENDER_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            logger.warning("Thumbnail generation timed out for %s", source)
            return False
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise

        if process.returncode != 0:
            detail = stderr.decode(errors="replace").strip()[:500]
            logger.warning("Thumbnail generation failed for %s: %s", source, detail)
            return False
        return True
