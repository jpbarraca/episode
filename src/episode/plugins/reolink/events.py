"""Reolink event parsing from binary Baichuan protocol frames.

Handles parsing of events received as binary frames over the TCP connection.
The camera pushes event frames (cmdId=33 for alarm events) that contain
XML payloads describing motion, person, vehicle, doorbell, and other events.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from episode.plugins.reolink.client import (
    aes_decrypt_cfb,
    bc_decrypt,
    derive_aes_key,
)

logger = logging.getLogger(__name__)

EVENT_MAP = {
    "person_detection": ("human", "person", "people"),
    "vehicle_detection": ("vehicle", "car"),
    "pet_detection": ("pet", "animal"),
    "ladder_detection": ("ladder",),
    "face_detection": ("face",),
    "motion_detection": ("motion", "movement", "mov"),
    "doorbell": ("doorbell", "ring"),
    "audio_detection": ("audio", "sound"),
    "line_crossing": ("line", "cross"),
    "system": ("motion",),
}


def _iter_texts(value: Any) -> list[str]:
    """Recursively collect string values from dict/list/tuple structures.

    Reolink alarm payloads often carry the subtype as a list, e.g.
    ``type=["intrusion", "people"]`` or a nested ``_value`` array, so we walk
    the whole structure to find the discriminating keyword.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for v in value.values():
            out.extend(_iter_texts(v))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_iter_texts(item))
        return out
    return []


ACTIVE_STATES = {"true", "1", "active", "on", "start", "begin"}
INACTIVE_STATES = {"false", "0", "inactive", "off", "stop", "end"}


@dataclass(frozen=True)
class ReolinkEvent:
    """Normalized Reolink event derived from Baichuan protocol frame payload."""

    event_type: str
    event_state: str
    source: str = "reolink:events"
    timestamp: datetime | None = None
    metadata: dict[str, Any] = field(default_factory=dict)  # type: ignore[assignment]
    raw_payload: dict[str, Any] | None = None
    device_id: str = ""
    channel: int = 0
    event_id: str = ""

    def __post_init__(self):
        """Default the timestamp to now if not provided."""
        if self.timestamp is None:
            object.__setattr__(self, "timestamp", datetime.now(tz=timezone.utc))


def parse_alarm_event_frame(
    body: bytes,
    channel: int = 0,
    *,
    nonce: str = "",
    password: str = "",
    use_aes: bool = False,
) -> list[ReolinkEvent]:
    """Parse a Baichuan alarm event frame body (cmdId=33).

    The camera pushes binary frames containing XML event data.
    This method handles both encrypted and plain XML bodies.

    Args:
        body: Raw bytes from the TCP frame (may be encrypted)
        channel: Channel number used as the BC cipher offset (from the
            connection's negotiated host channel id).
        nonce: Nonce negotiated during login (required for AES decryption).
        password: Camera password (required for AES key derivation).
        use_aes: Whether the body is AES-128-CFB encrypted (vs. XOR BC).

    Returns:
        List of parsed ReolinkEvent objects
    """
    events: list[ReolinkEvent] = []

    # Try to decrypt first using the negotiated encryption mode
    decrypted_body = _maybe_decrypt(
        body,
        offset=channel,
        nonce=nonce,
        password=password,
        use_aes=use_aes,
    )

    # Parse XML
    root = _parse_xml(decrypted_body)
    if root is None:
        logger.warning("Failed to parse alarm event XML body: %s", decrypted_body[:200])
        return []

    # Extract events from various possible structures
    events.extend(_extract_events_from_root(root, channel))

    logger.debug("Parsed %d events from alarm frame", len(events))
    return events


def parse_battery_status_frame(
    body: bytes,
    channel: int = 0,
    *,
    nonce: str = "",
    password: str = "",
    use_aes: bool = False,
) -> ReolinkEvent | None:
    """Parse a battery status frame (cmdId=252).

    Args:
        body: Raw bytes from the TCP frame
        channel: Channel number used as the BC cipher offset.
        nonce: Nonce negotiated during login (required for AES decryption).
        password: Camera password (required for AES key derivation).
        use_aes: Whether the body is AES-128-CFB encrypted (vs. XOR BC).

    Returns:
        ReolinkEvent with battery info or None
    """
    decrypted_body = _maybe_decrypt(
        body,
        offset=channel,
        nonce=nonce,
        password=password,
        use_aes=use_aes,
    )
    root = _parse_xml(decrypted_body)
    if root is None:
        return None

    metadata: dict[str, Any] = {}
    battery_level = 0
    charging = False

    for elem in root:
        if elem.tag == "batteryPower":
            try:
                battery_level = int(elem.text or "0")
            except (ValueError, TypeError):
                pass
        elif elem.tag == "isCharging":
            charging = str(elem.text or "false").lower() in ("true", "1", "yes")
        elif elem.tag == "lowPowerAlarm":
            metadata["low_power"] = str(elem.text or "false")

    event_type = "battery_low" if battery_level < 20 else "battery_status"
    state = "charging" if charging else "discharging"

    return ReolinkEvent(
        event_type=event_type,
        event_state=state,
        timestamp=datetime.now(tz=timezone.utc),
        metadata={**metadata, "battery_level": battery_level, "charging": charging},
    )


def interpret_event(raw_payload: dict[str, Any]) -> ReolinkEvent:
    """Map a parsed Reolink event payload dict to a normalized event.

    This handles events that have already been parsed from XML into dicts.

    Args:
        raw_payload: Dict from _xml_to_dict() for a single event

    Returns:
        Normalized ReolinkEvent
    """
    # Extract timestamp
    timestamp = _extract_timestamp(raw_payload)

    # Map event type
    event_type = _map_event_type(raw_payload)
    event_state = _map_event_state(raw_payload)

    metadata = {
        "integration": "reolink",
        "raw_payload": raw_payload,
    }

    # Extract common fields
    event_id = raw_payload.get("eventID", "") or raw_payload.get("EventIndex", "")
    channel = raw_payload.get("channel", 0)
    if isinstance(channel, str):
        try:
            channel = int(channel)
        except (ValueError, TypeError):
            channel = 0

    if event_id:
        metadata["event_id"] = event_id
    if channel:
        metadata["channel"] = channel

    logger.debug(
        "Interpreted event: type=%s state=%s",
        event_type,
        event_state,
    )

    return ReolinkEvent(
        event_type=event_type,
        event_state=event_state,
        timestamp=timestamp,
        metadata=metadata,
        raw_payload=raw_payload,
        channel=channel,
        event_id=str(event_id),
    )


# ── Internal helpers ──────────────────────────────────────────────────


def _maybe_decrypt(
    data: bytes,
    offset: int = 0,
    *,
    nonce: str = "",
    password: str = "",
    use_aes: bool = False,
) -> bytes:
    """Try to decrypt data using the negotiated encryption mode.

    Tries AES-128-CFB first (if use_aes and key material present), then
    XOR BCEncrypt with the channel offset. Returns the decrypted bytes if
    they parse as XML, otherwise the original data.
    """
    candidates: list[bytes] = []
    if use_aes and nonce:
        try:
            key = derive_aes_key(nonce, password)
            candidates.append(aes_decrypt_cfb(data, key))
        except Exception:
            pass
    candidates.append(bc_decrypt(data, offset))

    for decrypted in candidates:
        try:
            # Check if the decrypted result looks like XML (starts with '<').
            stripped = decrypted.lstrip()[:16]
            if stripped.startswith(b"<?xml") or stripped.startswith(b"<"):
                # Make sure it's valid XML
                ET.fromstring(decrypted)
                return decrypted
        except Exception:
            continue

    return data


def _parse_xml(data: bytes):
    """Parse XML data, returning Element or None on failure."""
    try:
        return ET.fromstring(data)
    except Exception as e:
        logger.debug("XML parse failed: %s", e)
        return None


def _extract_events_from_root(root, channel: int) -> list[ReolinkEvent]:
    """Extract event objects from an XML root element."""
    events: list[ReolinkEvent] = []

    # Various possible structures depending on situation and camera:
    # 1. Root -> AlarmEventList -> AlarmEvent (multiple)  [Reolink native]
    # 2. Root -> AlarmEventList -> Event (multiple)
    # 3. Root -> Event (direct)
    # 4. Root -> param -> AlarmEventList -> AlarmEvent/Event
    # 5. Root -> AlarmEvent (direct)

    event_elements = []

    # Check for AlarmEventList wrapper
    alarm_list = root.find("AlarmEventList")
    if alarm_list is not None:
        for child in alarm_list:
            if child.tag in ("Event", "AlarmEventList", "AlarmEvent"):
                event_elements.extend(_find_event_children(child))
    else:
        # Check for direct Event / AlarmEvent elements
        event_elements.extend(root.findall("Event"))
        event_elements.extend(root.findall("AlarmEvent"))
        # Check for param wrapper
        param = root.find("param")
        if param is not None:
            for child in param:
                if child.tag == "AlarmEventList":
                    event_elements.extend(_find_event_children(child))
                elif child.tag in ("Event", "AlarmEvent"):
                    event_elements.append(child)

    # If no specific event structure found, treat the whole XML as one event
    if not event_elements and root is not None:
        event_elements = [root]

    for elem in event_elements:
        event_dict = _xml_to_dict(elem)
        event = interpret_event(event_dict)
        if event.channel == 0 and channel:
            event = _replace_field(event, "channel", channel)
        events.append(event)

    return events


def _find_event_children(parent) -> list:
    """Recursively find Event / AlarmEvent elements in XML subtree."""
    events = []
    for child in parent:
        if child.tag in ("Event", "AlarmEvent"):
            events.append(child)
        else:
            events.extend(_find_event_children(child))
    return events


def _xml_to_dict(element, prefix: str = "") -> dict[str, Any]:
    """Convert XML element to dict, preserving structure."""
    result: dict[str, Any] = {}
    full_tag = f"{prefix}.{element.tag}" if prefix else element.tag

    # Check for attributes
    if element.attrib:
        result["@attributes"] = dict(element.attrib)

    children = list(element)
    if not children:
        # Leaf node
        text = (element.text or "").strip()
        if text:
            try:
                result["_value"] = int(text)
            except ValueError:
                try:
                    result["_value"] = float(text)
                except ValueError:
                    result["_value"] = text
            if element.tag:
                result[element.tag] = result.get(element.tag, result.get("_value", text))
        elif element.tag:
            result[element.tag] = ""
    else:
        for child in children:
            child_dict = _xml_to_dict(child, full_tag)
            for key, val in child_dict.items():
                if key in result:
                    if not isinstance(result[key], list):
                        result[key] = [result[key]]
                    result[key].append(val)
                else:
                    result[key] = val

    return result


def _extract_timestamp(payload: dict[str, Any]) -> datetime:
    """Extract timestamp from event payload."""
    # Check various possible locations
    for key in ("time", "timestamp", "createTime", "eventTime", "EventTime", "AlarmTime"):
        value = payload.get(key)
        if value:
            parsed = _try_parse_timestamp(value)
            if parsed:
                return parsed

    # Check nested
    for key in ("param", "AlarmEventList"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            for subkey in ("time", "timestamp", "createTime", "eventTime"):
                value = nested.get(subkey)
                if value:
                    parsed = _try_parse_timestamp(value)
                    if parsed:
                        return parsed

    return datetime.now(tz=timezone.utc)


def _try_parse_timestamp(value) -> datetime | None:
    """Parse a timestamp string to datetime."""
    if not value:
        return None
    try:
        if isinstance(value, str):
            # Handle various formats
            cleaned = value.strip()
            if cleaned.endswith("Z"):
                cleaned = cleaned[:-1] + "+00:00"
            parsed = datetime.fromisoformat(cleaned)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
    except (ValueError, TypeError):
        pass

    # Try as integer (Unix timestamp)
    try:
        ts = float(value)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        pass

    return None


def _map_event_type(payload: dict[str, Any]) -> str:
    """Map event payload to canonical event type string."""
    # Check various possible keys
    cmd = payload.get("cmd", "")
    event_type_val = payload.get("type", "")
    channel_event = payload.get("channelEvent", "")
    eventType = payload.get("eventType", "")
    ai_type = payload.get("AItype") or payload.get("aiType") or payload.get("aitype") or ""

    type_str = str(cmd or event_type_val or channel_event or eventType or "").lower()

    def get_strings_to_check():
        """Yield candidate type strings, from AItype, type fields, and any
        string values nested anywhere in the payload."""
        # 1: ai_str
        yield str(ai_type).lower()

        # 2: type_str (also covers list-valued fields via their repr, e.g.
        #    type=["intrusion","people"] -> "['intrusion', 'people']")
        yield str(cmd or event_type_val or channel_event or eventType or "").lower()

        # 3: all string values anywhere in the payload, including list-valued
        #    fields (type) and nested structures (_value array).
        for text in _iter_texts(payload):
            if text and text.strip():
                yield text.lower()

        # 4: dict 'param'
        param = payload.get("param")
        if isinstance(param, dict):
            for key in ("type", "event", "event_type"):
                val = param.get(key, "")
                if isinstance(val, str) and val.strip():
                    yield val.lower()

    for text in get_strings_to_check():
        if not text:
            continue
        for event_result, keywords in EVENT_MAP.items():
            if any(keyword in text for keyword in keywords):
                return event_result

    if cmd:
        return str(cmd).lower().replace("-", "_").replace(" ", "_")

    return "system"


def _map_event_state(payload: dict[str, Any]) -> str:
    """Map event state to 'active' or 'inactive'.

    Reolink alarm push frames (cmdId=33) are detections: the camera pushes a
    frame at the moment of detection (motion, person, vehicle, ...). Many of
    these payloads carry no explicit state field (e.g. ``status="none"``), so
    default to ``"active"``. This is what lets the Episode engine create and
    link an Episode: it only correlates ACTIVE events, or INACTIVE events that
    follow a matching preceding ACTIVE transition. Without this default, every
    detection would be stored as ``inactive`` with no preceding active event,
    so no Episode would ever be created or linked.
    """

    def get_state_values():
        """Yield candidate state fields from the payload and nested param."""
        for key in ("state", "status", "active", "enabled"):
            yield payload.get(key)

        param = payload.get("param")
        if isinstance(param, dict):
            for key in ("state", "status", "active"):
                yield param.get(key)

    for value in get_state_values():
        if value is not None:
            val_str = str(value).lower()
            if val_str in ACTIVE_STATES:
                return "active"
            if val_str in INACTIVE_STATES:
                return "inactive"

    # A pushed detection without an explicit state is activity.
    return "active"


def _replace_field(obj, field_name: str, new_value: Any):
    """Create a copy of a frozen dataclass with a field replaced."""
    return replace(obj, **{field_name: new_value})
