"""Reolink device validation via Baichuan binary protocol."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from episode.domain.models import Device
from episode.plugins.reolink.client import BaichuanApiClient, ReolinkError, ReolinkLoginError

logger = logging.getLogger(__name__)


def _ability_supports(ability: dict[str, Any], token: str) -> bool:
    """Return True if an ability-info dict indicates support for ``token``.

    The parsed ability XML is a nested dict; capability presence is detected
    by scanning the serialized JSON for the token. This is tolerant of
    firmware-specific XML layouts.
    """
    try:
        blob = json.dumps(ability, sort_keys=True)
    except (TypeError, ValueError):
        return False
    return token in blob


def _detect_events(ability: dict[str, Any] | None) -> bool:
    """Detect push-event support from the AbilityInfo response.

    Reolink push events (cmdId=33) rely on the alarm / IO / network
    subsystems, so we look for those tokens in the ability payload.
    """
    if not ability:
        return False
    return (
        _ability_supports(ability, "alarm")
        or _ability_supports(ability, "IO")
        or _ability_supports(ability, "network")
    )


async def validate_device(
    device: Device,
    checked_at: str,
    timeout: float,
) -> dict[str, Any]:
    """Validate a Reolink device over the Baichuan binary protocol.

    Performs a login handshake to verify connectivity and credentials, then
    probes media streams, push-event support and snapshots so the Web UI can
    report full capability support (mirroring ONVIF validation). Every probe
    degrades gracefully: a failure only omits that capability.
    """
    config = device.get_config("reolink")
    host = config.settings.get("host", "") if config else ""
    api_port = config.port if config and config.port else 9000
    target_host = host if host else device.ip_address

    logger.info(
        "Validating Reolink device: id=%s name=%s ip=%s host=%s port=%d timeout=%.1fs",
        device.id,
        device.name,
        device.ip_address,
        target_host,
        api_port,
        timeout,
    )

    client = BaichuanApiClient(
        host=target_host,
        username=device.username,
        password=device.password,
        api_port=api_port,
        timeout=min(timeout, 8),
    )

    try:
        info = await asyncio.wait_for(client.login(), timeout=timeout)
        capabilities = ["discovery"]
        details = {
            "mac_address": info.mac_address,
            "model": info.model,
            "firmware_version": info.firmware_version,
            "channel_count": info.channel_count,
        }

        # Probe streams, events and snapshots so the Web UI reports full
        # support (mirrors ONVIF validation). Each probe degrades gracefully:
        # a failure only omits that capability, never fails validation.
        probe_timeout = max(1.0, timeout - (timeout * 0.4))

        # Media / streams (cmdId=146)
        stream_info = None
        try:
            stream_info = await asyncio.wait_for(
                client.get_stream_url(channel=0), timeout=probe_timeout
            )
        except Exception as exc:
            logger.debug("Reolink validation: stream probe failed: %s", exc)
        stream_count = 0
        if stream_info and stream_info.success:
            stream_count = len(stream_info.streams or [])
            capabilities.append("media")
            details["streams"] = stream_count
            details["stream_supported"] = True

        # Events (cmdId=151 AbilityInfo)
        ability = None
        try:
            ability = await asyncio.wait_for(client.get_ability_info(), timeout=probe_timeout)
        except Exception as exc:
            logger.debug("Reolink validation: ability probe failed: %s", exc)
        events_supported = _detect_events(ability)
        if events_supported:
            capabilities.append("events")
        details["events_supported"] = events_supported

        # Snapshots (cmdId=109)
        snapshot_bytes = 0
        try:
            snapshot = await asyncio.wait_for(client.get_snapshot(channel=0), timeout=probe_timeout)
            if snapshot and snapshot[:2] == b"\xff\xd8":
                snapshot_bytes = len(snapshot)
                capabilities.append("snapshots")
        except Exception as exc:
            logger.debug("Reolink validation: snapshot probe failed: %s", exc)
        details["snapshot_bytes"] = snapshot_bytes

        summary = f"Reolink responded · {info.model or 'Device'}"
        if "media" in capabilities:
            summary += f" · {stream_count} stream{'s' if stream_count != 1 else ''}"
        if "snapshots" in capabilities:
            summary += " · snapshots"
        if "events" in capabilities:
            summary += " · events"
        logger.info(
            "Reolink validation successful: model=%s firmware=%s channels=%d capabilities=%s",
            info.model or "unknown",
            info.firmware_version or "unknown",
            info.channel_count,
            capabilities,
        )
        await client.close()
        return _result(
            "supported",
            summary,
            checked_at,
            capabilities=capabilities,
            details=details,
        )

    except asyncio.TimeoutError:
        logger.warning(
            "Reolink validation timed out for device %s (%s)",
            device.id,
            target_host,
        )
        return _result("unavailable", "Reolink did not respond", checked_at)
    except ReolinkLoginError:
        logger.warning(
            "Reolink login failed for device %s (%s)",
            device.id,
            target_host,
        )
        return _result(
            "authentication_failed",
            "Reolink rejected the configured credentials",
            checked_at,
        )
    except ConnectionRefusedError:
        logger.warning(
            "Reolink connection refused for device %s (%s)",
            device.id,
            target_host,
        )
        return _result(
            "unreachable",
            "Reolink endpoint could not be reached (connection refused)",
            checked_at,
        )
    except OSError as exc:
        logger.warning(
            "Reolink network error for device %s (%s): %s",
            device.id,
            target_host,
            exc,
        )
        return _result(
            "unreachable",
            f"Reolink endpoint unreachable: {exc}",
            checked_at,
        )
    except ReolinkError as exc:
        logger.warning(
            "Reolink validation error for device %s (%s): %s",
            device.id,
            target_host,
            exc,
        )
        return _result(
            "unavailable",
            f"Reolink validation failed: {str(exc)[:120]}",
            checked_at,
        )
    except Exception as exc:
        logger.error(
            "Reolink unexpected validation error for device %s (%s): %s",
            device.id,
            target_host,
            exc,
        )
        return _result(
            "unavailable",
            f"Reolink validation failed ({exc.__class__.__name__})",
            checked_at,
        )


def _result(
    status: str,
    summary: str,
    checked_at: str,
    *,
    capabilities: list[str] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a validation result dict from status, summary and optional
    capability/detail payloads."""
    return {
        "status": status,
        "summary": summary,
        "checked_at": checked_at,
        "capabilities": capabilities or [],
        "details": details or {},
    }
