from __future__ import annotations

import asyncio
import glob
import logging
import os
import re
import subprocess
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from episode.domain.models import Device, EpisodeState, EventState, Evidence
from episode.engine.bus import EventBus, Message
from episode.engine.engine import CanonicalEventResult
from episode.recording.targets import AreaRecordingTargetResolver, RecordingTargetResolver

if TYPE_CHECKING:
    from episode.storage.repository import Repository

logger = logging.getLogger(__name__)

_RECORDING_PART = re.compile(
    r"^rec_(?P<device>.+)_(?P<started>\d{8}_\d{6}_\d{6})_"
    r"(?P<session>[0-9a-f]{12})_(?P<index>\d{6})\.mp4\.part$"
)


@dataclass
class _EpisodeRecording:
    episode_id: str
    device_id: str
    area_id: str
    output_path: str
    working_path: str
    session_id: str
    start_time: datetime | None = None
    proc: asyncio.subprocess.Process | None = None
    task: asyncio.Task | None = None
    next_segment_index: int = 0
    segment_started_at: dict[int, datetime] = field(default_factory=dict)
    stop_reason: str | None = None


class RecordingEngine:
    def __init__(
        self,
        repo: Repository,
        bus: EventBus,
        data_dir: str,
        segment_seconds: int = 600,
        media=None,
        target_resolver: RecordingTargetResolver | None = None,
    ):
        self._repo = repo
        self._bus = bus
        self._data_dir = data_dir
        if segment_seconds <= 0:
            raise ValueError("segment_seconds must be greater than zero")
        self._segment_seconds = segment_seconds
        self._media = media
        self._target_resolver = target_resolver or AreaRecordingTargetResolver(repo)
        self._active_tasks: set[asyncio.Task] = set()
        self._recordings: dict[tuple[str, str], _EpisodeRecording] = {}
        self._running = False

    def _rec_key(self, episode_id: str, device_id: str) -> tuple[str, str]:
        return (episode_id, device_id)

    @staticmethod
    def _safe_device_id(device_id: str) -> str:
        value = re.sub(r"[^A-Za-z0-9._-]+", "-", device_id).strip("._-")
        return (value or "device")[:64]

    def active_device_ids(self, episode_id: str) -> tuple[str, ...]:
        """Return Devices currently recording one Episode without exposing stream details."""
        return tuple(
            sorted(
                recording.device_id
                for recording in self._recordings.values()
                if recording.episode_id == episode_id
            )
        )

    def active_file_paths(self) -> set[str]:
        paths: set[str] = set()
        for recording in self._recordings.values():
            paths.update(path for _index, path in self._segment_entries(recording))
        return paths

    async def start(self):
        self._running = True
        self._bus.subscribe("event.canonicalized", self._on_event)
        self._bus.subscribe("episode.updated", self._on_episode_updated)

    async def recover_interrupted_recordings(self) -> None:
        devices = await self._repo.list_devices(include_disabled=True)
        devices_by_safe_id: dict[str, list[Device]] = {}
        for device in devices:
            devices_by_safe_id.setdefault(self._safe_device_id(device.id), []).append(device)

        pattern = os.path.join(
            self._data_dir,
            "episodes",
            "*",
            "recordings",
            "*.mp4.part",
        )
        for working_path in sorted(glob.glob(pattern)):
            filename = os.path.basename(working_path)
            match = _RECORDING_PART.fullmatch(filename)
            if not match:
                logger.warning("Could not identify interrupted recording %s", filename)
                continue

            episode_id = os.path.basename(os.path.dirname(os.path.dirname(working_path)))
            candidates = devices_by_safe_id.get(match.group("device"), [])
            if len(candidates) != 1:
                logger.warning(
                    "Could not resolve Device for interrupted recording %s",
                    filename,
                )
                await self._repo.append_episode_journal(
                    episode_id,
                    "recording.incomplete",
                    {
                        "filename": filename,
                        "reason": "device_identity_unresolved",
                    },
                )
                continue

            device = candidates[0]
            started_at = datetime.strptime(
                match.group("started"),
                "%Y%m%d_%H%M%S_%f",
            ).replace(tzinfo=timezone.utc)
            ended_at = datetime.fromtimestamp(
                os.path.getmtime(working_path),
                tz=timezone.utc,
            )
            index = int(match.group("index"))
            rec = _EpisodeRecording(
                episode_id=episode_id,
                device_id=device.id,
                area_id=device.area_id,
                output_path=working_path.removesuffix(".part"),
                working_path=working_path,
                session_id=match.group("session"),
                start_time=started_at,
                segment_started_at={index: started_at},
                stop_reason="startup_recovery",
            )
            await self._finalize_segment(
                rec,
                index,
                working_path,
                started_at=started_at,
                ended_at=ended_at,
            )

    async def resume_active_episodes(self) -> None:
        now = datetime.now(tz=timezone.utc)
        episodes = [
            *await self._repo.list_episodes(state=EpisodeState.ACTIVE, limit=10000),
            *await self._repo.list_episodes(state=EpisodeState.QUIESCENT, limit=10000),
        ]
        for episode in episodes:
            if not episode.minimum_end_at or episode.minimum_end_at <= now:
                continue
            events = await self._repo.list_events(episode_id=episode.id, limit=10000)
            targets: dict[str, Device] = {}
            for event in events:
                if event.event_state != EventState.ACTIVE:
                    continue
                for device in await self._target_resolver.resolve(event):
                    targets[device.id] = device

            for device in targets.values():
                stream_url = self._stream_url(device)
                if not stream_url:
                    logger.warning(
                        "Could not resume recording for episode %s camera %s: no stream URL",
                        episode.id[:8],
                        device.id,
                    )
                    continue
                await self._start_recording(episode.id, device, stream_url)
                recording = self._recordings[self._rec_key(episode.id, device.id)]
                await self._repo.append_episode_journal(
                    episode.id,
                    "recording.resumed",
                    {
                        "device_id": device.id,
                        "recording_session_id": recording.session_id,
                        "minimum_end_at": episode.minimum_end_at.isoformat(),
                    },
                )

    async def stop(self):
        self._running = False
        self._bus.unsubscribe("event.canonicalized", self._on_event)
        self._bus.unsubscribe("episode.updated", self._on_episode_updated)
        await self._stop_recordings(
            list(self._recordings.values()),
            reason="application_shutdown",
        )
        if self._active_tasks:
            _, pending = await asyncio.wait(self._active_tasks, timeout=10)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _on_event(self, msg: Message):
        result = msg.data.get("result")
        if not isinstance(result, CanonicalEventResult) or not result.created:
            return
        event = result.event
        if event.event_state.value != "active":
            return
        episode_id = event.episode_id or ""
        if not episode_id:
            return

        for device in await self._target_resolver.resolve(event):
            key = self._rec_key(episode_id, device.id)
            if key in self._recordings:
                continue
            try:
                url = self._stream_url(device)
                if not url:
                    logger.warning(
                        "Skipping recording for episode %s camera %s: no stream URL",
                        episode_id[:8],
                        device.id,
                    )
                    continue
                await self._start_recording(episode_id, device, url)
            except Exception:
                logger.exception(
                    "Could not start recording for episode %s camera %s",
                    episode_id[:8],
                    device.id,
                )

    def _stream_url(self, device: Device) -> str:
        discovered = self._media.get(device.id) if self._media else None
        if discovered:
            return discovered.authenticated_stream_uri()
        video = device.get_config("video")
        return video.build_url(device.ip_address, device.username, device.password) if video else ""

    async def _start_recording(self, episode_id: str, device: Device, rtsp_url: str):
        key = self._rec_key(episode_id, device.id)
        if key in self._recordings:
            return

        started_at = datetime.now(tz=timezone.utc)
        ts = started_at.strftime("%Y%m%d_%H%M%S_%f")
        safe_device_id = self._safe_device_id(device.id)
        session_id = uuid.uuid4().hex[:12]
        filename = f"rec_{safe_device_id}_{ts}_{session_id}_%06d.mp4"
        output = os.path.join(self._data_dir, "episodes", episode_id, "recordings", filename)
        working = f"{output}.part"

        rec = _EpisodeRecording(
            episode_id=episode_id,
            device_id=device.id,
            area_id=device.area_id,
            output_path=output,
            working_path=working,
            session_id=session_id,
            start_time=started_at,
            segment_started_at={0: started_at},
        )
        self._recordings[key] = rec

        rec.task = asyncio.create_task(self._record_episode(rec, rtsp_url))
        self._active_tasks.add(rec.task)
        rec.task.add_done_callback(self._active_tasks.discard)

        logger.info(
            "Started recording %s for episode %s camera %s",
            filename.replace("%06d", "*"),
            episode_id[:8],
            device.id,
        )

    async def _on_episode_updated(self, msg: Message):
        episode_id = msg.data.get("episode_id", "")
        state = msg.data.get("state", "")
        if state == "closed":
            recordings = [
                recording
                for recording in self._recordings.values()
                if recording.episode_id == episode_id
            ]
            await asyncio.gather(*(self._stop_recording(recording) for recording in recordings))

    async def _stop_recording(
        self,
        rec: _EpisodeRecording,
        *,
        reason: str | None = None,
    ) -> None:
        await self._stop_recordings([rec], reason=reason)

    async def _stop_recordings(
        self,
        recordings: list[_EpisodeRecording],
        *,
        reason: str | None = None,
    ) -> None:
        if not recordings:
            return

        for rec in recordings:
            self._recordings.pop(self._rec_key(rec.episode_id, rec.device_id), None)
            rec.stop_reason = reason
            self._signal_process(rec)

        if reason:
            await asyncio.gather(
                *(self._journal_interruption(rec, reason) for rec in recordings),
                return_exceptions=True,
            )
        await asyncio.gather(
            *(self._finish_stop(rec) for rec in recordings),
            return_exceptions=True,
        )

    @staticmethod
    def _signal_process(rec: _EpisodeRecording) -> None:
        if rec.proc and rec.proc.returncode is None:
            try:
                rec.proc.terminate()
            except ProcessLookupError:
                pass

    async def _finish_stop(self, rec: _EpisodeRecording) -> None:
        await self._await_process_stop(rec)
        if rec.task and rec.task is not asyncio.current_task():
            await asyncio.gather(rec.task, return_exceptions=True)

    async def _terminate_process(self, rec: _EpisodeRecording) -> None:
        self._signal_process(rec)
        await self._await_process_stop(rec)

    @staticmethod
    async def _await_process_stop(rec: _EpisodeRecording) -> None:
        process = rec.proc
        if process and process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    async def _journal_interruption(self, rec: _EpisodeRecording, reason: str) -> None:
        try:
            await self._repo.append_episode_journal(
                rec.episode_id,
                "recording.interrupted",
                {
                    "device_id": rec.device_id,
                    "recording_session_id": rec.session_id,
                    "reason": reason,
                },
            )
        except Exception:
            logger.exception(
                "Could not journal recording interruption for episode %s camera %s",
                rec.episode_id[:8],
                rec.device_id,
            )

    async def _record_episode(self, rec: _EpisodeRecording, rtsp_url: str, _retries: int = 0):
        key = self._rec_key(rec.episode_id, rec.device_id)
        os.makedirs(os.path.dirname(rec.output_path), exist_ok=True)
        segments_before = rec.next_segment_index
        wait_task = None
        returncode = -1

        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-n",
                "-rtsp_transport",
                "tcp",
                "-i",
                rtsp_url,
                "-map",
                "0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-f",
                "segment",
                "-segment_time",
                str(self._segment_seconds),
                "-reset_timestamps",
                "1",
                "-segment_format",
                "mp4",
                "-segment_start_number",
                str(rec.next_segment_index),
                rec.working_path,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            rec.proc = proc
            wait_task = asyncio.create_task(proc.wait())
            while not wait_task.done():
                done, _ = await asyncio.wait({wait_task}, timeout=1)
                await self._finalize_ready_segments(rec, include_latest=bool(done))
            returncode = await wait_task
        except asyncio.CancelledError:
            await self._terminate_process(rec)
            if wait_task:
                await asyncio.gather(wait_task, return_exceptions=True)
            await self._finalize_ready_segments(rec, include_latest=True)
            self._recordings.pop(key, None)
            await self._preserve_working_segments(rec, reason="recording_task_cancelled")
            raise
        except Exception:
            logger.exception(
                "Recording process failed for episode %s camera %s",
                rec.episode_id[:8],
                rec.device_id,
            )
        finally:
            rec.proc = None

        await self._finalize_ready_segments(rec, include_latest=True)

        if self._recordings.get(key) is not rec:
            return
        episode = await self._repo.get_episode(rec.episode_id)
        if not self._running or not episode or episode.state == EpisodeState.CLOSED:
            self._recordings.pop(key, None)
            return

        retry = 0 if rec.next_segment_index > segments_before else _retries + 1
        if retry > 3:
            self._recordings.pop(key, None)
            await self._preserve_working_segments(rec, reason="retry_limit_exceeded")
            logger.error(
                "Recording failed for episode %s camera %s after 3 retries",
                rec.episode_id[:8],
                rec.device_id,
            )
            return

        logger.warning(
            "Recording process ended for episode %s camera %s "
            "(ffmpeg exit %s), reconnecting (%d/3)",
            rec.episode_id[:8],
            rec.device_id,
            returncode,
            retry,
        )
        await asyncio.sleep(2)
        if self._recordings.get(key) is not rec:
            return
        episode = await self._repo.get_episode(rec.episode_id)
        if not episode or episode.state == EpisodeState.CLOSED:
            self._recordings.pop(key, None)
            return
        await self._record_episode(rec, rtsp_url, retry)

    def _segment_entries(self, rec: _EpisodeRecording) -> list[tuple[int, str]]:
        entries = []
        pattern = rec.working_path.replace("%06d", "*")
        for path in sorted(glob.glob(pattern)):
            index_text = path.removesuffix(".mp4.part").rsplit("_", 1)[-1]
            if index_text.isdigit():
                entries.append((int(index_text), path))
        return entries

    async def _finalize_ready_segments(
        self, rec: _EpisodeRecording, *, include_latest: bool
    ) -> None:
        entries = self._segment_entries(rec)
        if not entries:
            return

        observed_at = datetime.now(tz=timezone.utc)
        for index, _ in entries:
            if index == 0 and rec.start_time:
                rec.segment_started_at.setdefault(index, rec.start_time)
            else:
                rec.segment_started_at.setdefault(index, observed_at)

        ready = entries if include_latest else entries[:-1]
        for index, working_path in ready:
            started_at = rec.segment_started_at.get(index, observed_at)
            ended_at = rec.segment_started_at.get(index + 1, observed_at)
            await self._finalize_segment(
                rec,
                index,
                working_path,
                started_at=started_at,
                ended_at=ended_at,
            )
            rec.next_segment_index = max(rec.next_segment_index, index + 1)

    async def _finalize_segment(
        self,
        rec: _EpisodeRecording,
        index: int,
        working_path: str,
        *,
        started_at: datetime,
        ended_at: datetime,
    ) -> None:
        if not os.path.exists(working_path):
            return
        if os.path.getsize(working_path) < 4096 or not await self._has_video_stream(working_path):
            logger.warning(
                "Recording segment invalid for episode %s camera %s, preserving as incomplete",
                rec.episode_id[:8],
                rec.device_id,
            )
            await self._preserve_incomplete_segment(
                rec,
                index,
                working_path,
                reason=rec.stop_reason or "recording_process_failed",
                started_at=started_at,
                ended_at=ended_at,
            )
            return

        output_path = working_path.removesuffix(".part")
        os.replace(working_path, output_path)
        byte_size = os.path.getsize(output_path)
        duration = max(0, int((ended_at - started_at).total_seconds()))
        evidence = Evidence(
            device_id=rec.device_id,
            area_id=rec.area_id,
            timestamp=started_at,
            evidence_type="recording",
            file_path=output_path,
            mime_type="video/mp4",
            episode_id=rec.episode_id,
            metadata={
                "origin": "recording",
                "recording_session_id": rec.session_id,
                "segment_index": index,
                "segment_seconds": self._segment_seconds,
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "duration_seconds": duration,
            },
        )
        await self._bus.publish(
            Message(type="evidence.received", data={"evidence": asdict(evidence)})
        )
        if rec.stop_reason:
            await self._repo.append_episode_journal(
                rec.episode_id,
                "recording.recovered",
                {
                    "device_id": rec.device_id,
                    "recording_session_id": rec.session_id,
                    "segment_index": index,
                    "recovery": "graceful_finalize",
                },
            )
        logger.info(
            "Recording segment %d complete for episode %s camera %s: %s (%.1f KB)",
            index,
            rec.episode_id[:8],
            rec.device_id,
            os.path.basename(output_path),
            byte_size / 1024,
        )

    async def _preserve_working_segments(
        self,
        rec: _EpisodeRecording,
        *,
        reason: str,
    ) -> None:
        observed_at = datetime.now(tz=timezone.utc)
        for index, path in self._segment_entries(rec):
            await self._preserve_incomplete_segment(
                rec,
                index,
                path,
                reason=reason,
                started_at=rec.segment_started_at.get(index, rec.start_time or observed_at),
                ended_at=observed_at,
            )

    async def _preserve_incomplete_segment(
        self,
        rec: _EpisodeRecording,
        index: int,
        working_path: str,
        *,
        reason: str,
        started_at: datetime,
        ended_at: datetime,
    ) -> None:
        if not os.path.exists(working_path):
            return
        evidence = Evidence(
            device_id=rec.device_id,
            area_id=rec.area_id,
            timestamp=started_at,
            evidence_type="incomplete_recording",
            file_path=working_path,
            mime_type="application/octet-stream",
            original_filename=os.path.basename(working_path),
            episode_id=rec.episode_id,
            metadata={
                "origin": "recording",
                "status": "incomplete",
                "reason": reason,
                "recording_session_id": rec.session_id,
                "segment_index": index,
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
            },
        )
        await self._bus.publish(
            Message(type="evidence.received", data={"evidence": asdict(evidence)})
        )
        await self._repo.append_episode_journal(
            rec.episode_id,
            "recording.incomplete",
            {
                "device_id": rec.device_id,
                "recording_session_id": rec.session_id,
                "segment_index": index,
                "reason": reason,
            },
        )

    async def _has_video_stream(self, path: str) -> bool:
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
                stdout=asyncio.subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            logger.warning("Could not validate recording %s", os.path.basename(path))
            return False

        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
            return proc.returncode == 0 and stdout.strip() == b"video"
        except asyncio.TimeoutError:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            logger.warning("Could not validate recording %s", os.path.basename(path))
            return False

    @staticmethod
    def _remove_file(path: str):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass

    def status(self) -> dict:
        cameras = len(set(k[1] for k in self._recordings))
        return {
            "running": self._running,
            "active_recordings": len(self._recordings),
            "cameras": cameras,
            "segment_seconds": self._segment_seconds,
        }
