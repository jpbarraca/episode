from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from episode.api.routes import create_api
from episode.config import EpisodeConfig
from episode.domain.models import Area, Device, Episode, EpisodeState, Event, Evidence
from episode.storage.repository import Repository


@pytest.mark.asyncio
async def test_closest_event_treats_no_match_as_an_empty_association(tmp_path):
    repository = Repository(EpisodeConfig(data_dir=str(tmp_path)))
    await repository.initialize()
    await repository.upsert_area(Area(id="driveway", name="Driveway"))
    await repository.upsert_device(
        Device(
            id="driveway-camera",
            name="Driveway camera",
            device_type="camera",
            area_id="driveway",
        )
    )
    observed_at = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
    episode = Episode(
        id="episode-with-ftp-snapshots",
        primary_area_id="driveway",
        start_time=observed_at,
        last_event_time=observed_at,
        end_time=observed_at + timedelta(seconds=30),
        state=EpisodeState.CLOSED,
    )
    await repository.create_episode(episode)
    event = await repository.create_event(
        Event(
            id="motion-event",
            device_id="driveway-camera",
            area_id="driveway",
            timestamp=observed_at,
            event_type="human_detection",
            source="onvif:events",
            episode_id=episode.id,
            metadata={
                "bounding_box": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
                "target_type": "human",
            },
        )
    )
    matched = await repository.create_evidence(
        Evidence(
            id="matched-snapshot",
            device_id="driveway-camera",
            area_id="driveway",
            timestamp=observed_at,
            evidence_type="snapshot",
            file_path=str(tmp_path / "matched.jpg"),
            mime_type="image/jpeg",
            episode_id=episode.id,
            metadata={"origin": "ftp"},
        )
    )
    unmatched = await repository.create_evidence(
        Evidence(
            id="unmatched-snapshot",
            device_id="driveway-camera",
            area_id="driveway",
            timestamp=observed_at + timedelta(seconds=10),
            evidence_type="snapshot",
            file_path=str(tmp_path / "unmatched.jpg"),
            mime_type="image/jpeg",
            episode_id=episode.id,
            metadata={"origin": "ftp"},
        )
    )

    try:
        transport = httpx.ASGITransport(app=create_api(repository, str(tmp_path)))
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            matched_response = await client.get(f"/api/v1/evidence/{matched.id}/closest-event")
            unmatched_response = await client.get(f"/api/v1/evidence/{unmatched.id}/closest-event")
            missing_response = await client.get("/api/v1/evidence/missing/closest-event")

        assert matched_response.status_code == 200
        assert matched_response.json()["event"]["id"] == event.id
        assert matched_response.json()["bounding_box"] == {
            "x": 0.1,
            "y": 0.2,
            "width": 0.3,
            "height": 0.4,
        }
        assert matched_response.json()["target_type"] == "human"
        assert unmatched_response.status_code == 200
        assert unmatched_response.json() == {
            "event": None,
            "bounding_box": None,
            "target_type": None,
        }
        assert missing_response.status_code == 404
    finally:
        await repository.close()
