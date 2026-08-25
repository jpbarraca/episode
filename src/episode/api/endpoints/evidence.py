from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from episode.api.context import ApiContext
from episode.api.errors import PUBLIC_ERROR_RESPONSES
from episode.api.pagination import DEFAULT_LIMIT, PageLimit, PageOffset
from episode.api.projections import event_annotations, public_evidence
from episode.api.schemas import ClosestEventResponse, EvidenceResponse


def evidence_router(context: ApiContext) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1",
        tags=["evidence"],
        responses=PUBLIC_ERROR_RESPONSES,
    )
    repo = context.repository

    @router.get("/evidence", response_model=list[EvidenceResponse])
    async def list_evidence(
        episode_id: str | None = None,
        event_id: str | None = None,
        device_id: str | None = None,
        area_id: str | None = None,
        evidence_type: str | None = None,
        has_episode: bool | None = None,
        limit: PageLimit = DEFAULT_LIMIT,
        offset: PageOffset = 0,
    ):
        evidence = await repo.list_evidence(
            episode_id,
            event_id,
            device_id,
            limit,
            offset,
            area_id=area_id,
            evidence_type=evidence_type,
            has_episode=has_episode,
        )
        return [public_evidence(item) for item in evidence]

    @router.get("/covers", response_model=dict[str, str])
    async def covers(ids: str = ""):
        if not ids:
            return {}
        episode_ids = [value.strip() for value in ids.split(",") if value.strip()]
        if not episode_ids:
            return {}
        return await repo.episode_covers(episode_ids)

    @router.get("/evidence/{evidence_id}", response_model=EvidenceResponse)
    async def get_evidence(evidence_id: str):
        evidence = await repo.get_evidence(evidence_id)
        if not evidence:
            raise HTTPException(404, "Evidence not found")
        return public_evidence(evidence)

    @router.get("/evidence/{evidence_id}/closest-event", response_model=ClosestEventResponse)
    async def evidence_closest_event(evidence_id: str):
        evidence = await repo.get_evidence(evidence_id)
        if not evidence:
            raise HTTPException(404, "Evidence not found")
        if evidence.availability == "expired":
            return {"event": None, "bounding_box": None, "target_type": None}
        no_match = {"event": None, "bounding_box": None, "target_type": None}
        if not evidence.episode_id:
            return no_match

        events = await repo.list_events(
            episode_id=evidence.episode_id,
            device_id=evidence.device_id,
        )
        events = [event for event in events if event.timestamp <= evidence.timestamp]
        if not events:
            return no_match

        closest = min(events, key=lambda event: abs(event.timestamp - evidence.timestamp))
        if (
            context.snapshot_window
            and abs((closest.timestamp - evidence.timestamp).total_seconds())
            > context.snapshot_window
        ):
            return no_match

        bounding_box, target_type = event_annotations(closest)
        return {
            "event": await context.public_event(closest),
            "bounding_box": bounding_box,
            "target_type": target_type,
        }

    @router.get("/evidence/{evidence_id}/file")
    async def serve_evidence_file(evidence_id: str):
        evidence = await repo.get_evidence(evidence_id)
        if not evidence:
            raise HTTPException(404, "Evidence not found")
        if evidence.availability == "expired":
            raise HTTPException(410, "Evidence expired under the retention policy")
        if not os.path.exists(evidence.file_path):
            raise HTTPException(404, "File not found on disk")
        return FileResponse(evidence.file_path, media_type=evidence.mime_type)

    @router.get("/evidence/{evidence_id}/thumbnail")
    async def serve_evidence_thumbnail(evidence_id: str):
        evidence = await repo.get_evidence(evidence_id)
        if not evidence:
            raise HTTPException(404, "Evidence not found")
        if evidence.availability == "expired":
            raise HTTPException(410, "Evidence expired under the retention policy")
        if not context.thumbnails:
            raise HTTPException(404, "Thumbnail not available")

        thumbnail_path = await context.thumbnails.get_or_create(evidence)
        if not thumbnail_path:
            raise HTTPException(404, "Thumbnail not available")
        return FileResponse(
            thumbnail_path,
            media_type="image/jpeg",
            headers={"Cache-Control": "private, max-age=86400"},
        )

    return router
