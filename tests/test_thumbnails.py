from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from episode.api.routes import create_api
from episode.api.thumbnails import ThumbnailCache
from episode.domain.models import Evidence


@pytest.mark.asyncio
async def test_thumbnail_cache_generates_once_without_modifying_evidence(tmp_path, monkeypatch):
    source = tmp_path / "evidence.jpg"
    original = b"immutable-evidence"
    source.write_bytes(original)
    evidence = Evidence(
        id="evidence-1",
        file_path=str(source),
        mime_type="image/jpeg",
        sha256="a" * 64,
    )
    cache = ThumbnailCache(tmp_path / "cache")
    render_count = 0

    async def render(source_path: Path, destination: Path) -> bool:
        nonlocal render_count
        render_count += 1
        assert source_path == source
        await asyncio.sleep(0.01)
        destination.write_bytes(b"derived-thumbnail")
        return True

    monkeypatch.setattr(cache, "_render", render)

    first, second = await asyncio.gather(
        cache.get_or_create(evidence),
        cache.get_or_create(evidence),
    )
    third = await cache.get_or_create(evidence)

    assert first == second == third
    assert render_count == 1
    assert Path(first).read_bytes() == b"derived-thumbnail"
    assert source.read_bytes() == original
    assert list((tmp_path / "cache").iterdir()) == [Path(first)]


@pytest.mark.asyncio
async def test_thumbnail_cache_ignores_non_media_evidence(tmp_path, monkeypatch):
    source = tmp_path / "payload.txt"
    source.write_text("preserved payload", encoding="utf-8")
    evidence = Evidence(id="payload", file_path=str(source), mime_type="text/plain")
    cache_dir = tmp_path / "cache"
    cache = ThumbnailCache(cache_dir)

    async def unexpected_render(source_path: Path, destination: Path) -> bool:
        raise AssertionError("non-media Evidence must not be rendered")

    monkeypatch.setattr(cache, "_render", unexpected_render)

    assert await cache.get_or_create(evidence) is None
    assert not cache_dir.exists()
    assert source.read_text(encoding="utf-8") == "preserved payload"


@pytest.mark.asyncio
async def test_thumbnail_endpoint_serves_only_the_derived_cache(tmp_path, monkeypatch):
    source = tmp_path / "recording.mp4"
    source.write_bytes(b"immutable-video")
    evidence = Evidence(
        id="recording-1",
        file_path=str(source),
        mime_type="video/mp4",
        sha256="b" * 64,
    )
    render_count = 0

    class RepositoryStub:
        async def get_evidence(self, evidence_id: str):
            return evidence if evidence_id == evidence.id else None

    async def render(source_path: Path, destination: Path) -> bool:
        nonlocal render_count
        render_count += 1
        assert source_path == source
        destination.write_bytes(b"jpeg-thumbnail")
        return True

    monkeypatch.setattr(ThumbnailCache, "_render", staticmethod(render))
    app = create_api(RepositoryStub(), str(tmp_path))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.get(f"/api/v1/evidence/{evidence.id}/thumbnail")
        second = await client.get(f"/api/v1/evidence/{evidence.id}/thumbnail")
        missing = await client.get("/api/v1/evidence/missing/thumbnail")

    assert first.status_code == second.status_code == 200
    assert first.content == second.content == b"jpeg-thumbnail"
    assert first.headers["content-type"].startswith("image/jpeg")
    assert first.headers["cache-control"] == "private, max-age=86400"
    assert missing.status_code == 404
    assert render_count == 1
    assert source.read_bytes() == b"immutable-video"
    assert len(list((tmp_path / "cache" / "thumbnails").glob("*.jpg"))) == 1
