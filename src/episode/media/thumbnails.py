"""On-demand thumbnail generation service.

Thumbnails are stored separately from evidence files in a dedicated cache
directory. They are never stored alongside evidence files on disk, keeping
event and episode data directories untouched.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from pathlib import Path

from episode.config import ThumbnailConfig

logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tiff", ".gif"}
VIDEO_EXTENSIONS = {
    ".mp4",
    ".mkv",
    ".avi",
    ".mov",
    ".wmv",
    ".flv",
    ".webm",
    ".m4v",
    ".mpeg",
    ".mpg",
    ".3gp",
}


class ThumbnailService:
    """Generate and cache image thumbnails on demand."""

    def __init__(self, config: ThumbnailConfig) -> None:
        self._config = config
        self._cache_dir = Path(config.cache_dir)
        os.makedirs(self._cache_dir, exist_ok=True)

    def _cache_path(self, evidence_path: str, width: int, height: int) -> Path:
        hash_key = hashlib.sha256(evidence_path.encode()).hexdigest()[:16]
        return self._cache_dir / f"{hash_key}.thumb.{width}x{height}.jpg"

    def _is_image(self, file_path: str) -> bool:
        return Path(file_path).suffix.lower() in IMAGE_EXTENSIONS

    def _is_video(self, file_path: str) -> bool:
        return Path(file_path).suffix.lower() in VIDEO_EXTENSIONS

    def get_or_create(self, evidence_path: str, width: int, height: int) -> str | None:
        """Get cached thumbnail or generate it on demand.

        Returns the path to the thumbnail file, or None if generation fails.
        """
        # Cap dimensions at configured maximums
        width = min(width, self._config.max_width)
        height = min(height, self._config.max_height)

        if not self._config.enabled:
            return None
        if not evidence_path or not os.path.isfile(evidence_path):
            return None
        if not self._is_image(evidence_path) and not self._is_video(evidence_path):
            return None

        cached_path = self._cache_path(evidence_path, width, height)
        if cached_path.exists() and os.path.getmtime(cached_path) >= os.path.getmtime(
            evidence_path
        ):
            return str(cached_path)

        return self._generate(evidence_path, width, height, str(cached_path))

    def _generate(self, source_path: str, width: int, height: int, dest_path: str) -> str | None:
        """Generate a thumbnail and save it to the cache directory."""
        try:
            from PIL import Image
        except ImportError:
            logger.error("Pillow is required for thumbnail generation")
            return None

        try:
            with Image.open(source_path) as img:
                if img.format and img.format.upper() in ("JPEG", "JPG"):
                    img = img.copy()
                    if img.mode == "RGBA":
                        background = Image.new("RGB", img.size, (255, 255, 255))
                        background.paste(img, mask=img.split()[3])
                        img = background
                    elif img.mode != "RGB":
                        img = img.convert("RGB")
                elif img.mode != "RGB":
                    img = img.convert("RGB")

                img.thumbnail((width, height), Image.LANCZOS)
                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                img.save(dest_path, format="JPEG", quality=self._config.quality)
                logger.debug("Generated thumbnail: %s -> %s", source_path, dest_path)
                return dest_path
        except Exception:
            pass

        # PIL failed — likely a video file. Try ffmpeg.
        return self._generate_with_ffmpeg(source_path, width, height, dest_path)

    def _generate_with_ffmpeg(
        self, source_path: str, width: int, height: int, dest_path: str
    ) -> str | None:
        """Use ffmpeg to extract the first frame from a video and resize it."""
        if not os.path.isfile(source_path):
            return None

        dest_dir = os.path.dirname(dest_path)
        if dest_dir:
            os.makedirs(dest_dir, exist_ok=True)

        try:
            loop = asyncio.new_event_loop()
            try:
                proc = loop.run_until_complete(
                    asyncio.create_subprocess_exec(
                        "ffmpeg",
                        "-y",
                        "-i",
                        source_path,
                        "-vframes",
                        "1",
                        "-vf",
                        f"scale={width}:{height}:force_original_aspect_ratio=decrease",
                        "-q:v",
                        "2",
                        dest_path,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                )
                _, stderr = loop.run_until_complete(proc.communicate())
                if proc.returncode != 0:
                    logger.warning(
                        "ffmpeg failed for %s (exit %d): %s",
                        source_path,
                        proc.returncode,
                        stderr.decode(errors="replace"),
                    )
                    return None
                if not os.path.isfile(dest_path):
                    logger.warning("ffmpeg completed but output file missing: %s", dest_path)
                    return None
                logger.debug("Generated thumbnail via ffmpeg: %s -> %s", source_path, dest_path)
                return dest_path
            finally:
                loop.close()
        except FileNotFoundError:
            logger.error("ffmpeg not found. Install ffmpeg for video thumbnail generation.")
            return None
        except Exception as error:
            logger.warning("ffmpeg thumbnail generation failed for %s: %s", source_path, error)
            return None

    def delete(self, evidence_path: str) -> None:
        """Remove cached thumbnails for an evidence file.

        This is a fire-and-forget operation — thumbnail cleanup failure
        does not affect the primary evidence deletion.
        """
        try:
            if not self._config.enabled:
                return
            cache_dir = Path(self._cache_dir)
            if not cache_dir.exists():
                return

            hash_key = hashlib.sha256(evidence_path.encode()).hexdigest()[:16]
            for thumbnail_file in cache_dir.glob(f"{hash_key}.thumb.*"):
                try:
                    thumbnail_file.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Could not remove thumbnail %s", thumbnail_file)
        except Exception as error:
            logger.warning("Thumbnail cleanup failed for %s: %s", evidence_path, error)
