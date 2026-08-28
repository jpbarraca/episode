# Reolink camera setup

Reolink cameras are supported through ONVIF or a lightweight HTTP protocol 
plugin communicating with the Baichuan API on port 9000. The plugin discovers
device information, configures recording endpoints, and receives motion and
person Events via long-polling or server-push, depending on camera firmware.

## About the Baichuan API

Reolink cameras expose a built-in HTTP API (commonly called the Baichuan API)
listening on TCP port 9000. The API uses JSON payloads over HTTP or HTTPS and
provides:

- Device information and firmware version
- Live-stream RTSP URL discovery
- Snapshot capture
- PTZ movement and preset control
- Motion detection configuration and status
- Event subscription via HTTP long-poll or TCP push
- Recording and storage status

The API does not require ONVIF to be enabled. It is independent of the
RTSP stream and uses its own credentials or the camera login credentials.

## Camera requirements

- A Reolink camera with firmware that exposes the Baichuan API on port 9000.
  Most Reolink cameras released since 2020 support this API.
- The Episode host must be able to reach the camera on port 9000.
- Use HTTPS if the camera supports it and the camera certificate can be
  validated. The plugin respects standard system CA certificates.
- Synchronize the camera and Episode host with NTP. Event correlation depends
  on observation times being reasonably close.
- Keep the camera on a trusted network or isolated VLAN.

For camera models known to be compatible, see the [compatibility notes
later in this guide](#compatibility-notes).

## Add a camera

1. Start Episode and open <http://localhost:8989>.
2. Open **Devices → Manage Areas** and create an Area if needed.
3. Select **Add Device**.
4. Enter a name, Area, camera IP address, username, and password.
5. Select **Validate and discover**. A successful Baichuan response reports
   device identity, model, firmware, stream capability, and event capability
   without enabling them.
6. Leave **Video recording** and **Reolink Baichuan** enabled when validation
   supports them.
7. Choose **Own Events only** to record only this camera's Events, or **Any
   Episode in this Area** when the camera should join activity opened by
   another Device.
8. Save the Device. Episode activates its integrations without restarting the
   container.

Credentials are write-only in the API and UI: after saving, they are reported
only as configured and are never returned to the browser. Leaving credential
fields blank while editing keeps their stored values.

The default Baichuan service is HTTP port 9000. Change the port under
**Manual connection overrides** when a camera uses a non-standard value.
Baichuan-discovered manufacturer, model, firmware, and event capabilities are
shown read-only on the Device page.

## What happens at runtime

1. Episode reads the Baichuan API device info to confirm connectivity.
2. It requests the live-stream RTSP URL for the main and sub streams.
3. It registers the selected RTSP endpoint with the media layer.
4. If Baichuan Events are enabled, it opens a long-poll subscription or TCP
   push listener to receive motion and person detection Events.
5. Active Events create or join an Episode and start configured actions.

### Long-poll event subscription

When the camera firmware supports it, Episode opens an HTTP long-poll
subscription to the event API endpoint. The camera holds the request open
until an event occurs or the timeout expires, then immediately returns the
next event or a heartbeat. Episode follows the subscription with automatic
reconnection and deduplicates events that arrive multiple times.

### TCP push event delivery

Newer Reolink firmware versions support a TCP push protocol. When enabled,
the camera initiates a persistent TCP connection to Episode and sends binary
event notifications. This method avoids the latency of long-poll and reduces
server resource usage. The camera must be configured to point to the Episode
host address and port for push delivery.

## Recording and stream discovery

Episode normally discovers the RTSP URI through the Baichuan API. The API
returns the main-stream and sub-stream RTSP URIs along with codec, resolution,
and framerate metadata. Episode selects the main-stream by default for
recording.

A manual RTSP fallback can be enabled explicitly for cameras with incomplete
API discovery. The common Reolink RTSP path is
`rtsp://<host>:554/live/main` for the main stream and
`rtsp://<host>:554/live/sub` for the sub-stream.

## Event interpretation

The Reolink plugin interprets Baichuan Events into Episode's canonical event
model:

- **Motion detection** events create `motion` Events with `active` and
  `inactive` states, preserving the camera's detection region as metadata.
- **Person detection** events create `person` Events with `active` and
  `inactive` states.
- **Doorbell ring** events create `doorbell` Events with `active` and
  `inactive` states (for Reolink doorbell models).
- **Audio detection** events create `audio` Events.
- Unknown or unrecognized event types are preserved as raw deliveries for
  future interpretation and never create guessed Episodes.

Events arriving from Reolink share the same deduplication pipeline as ONVIF
and Hikvision Events. Equivalent topics describing the same camera state are
aggregated, and the complete raw Baichuan JSON payload is preserved exactly as
an immutable artifact.

## Vendor enhancements

Baichuan remains the primary media and event path. The plugin discovers
device identity, firmware version, storage status, and SD card health without
requiring ONVIF. When a camera also supports ONVIF, both integrations can run
side-by-side. Baichuan handles event interpretation and media discovery while
ONVIF provides additional profiles if needed.

### PTZ control

Reolink PTZ cameras expose preset positions, zoom, and pan/tilt through the
Baichuan API. The plugin registers PTZ capabilities on the Device page when
supported. Actual PTZ control is initiated from the Episode UI when viewing a
live stream, and the control commands are forwarded through the Baichuan
API without modifying the stream.

### Recording configuration

The plugin can configure the camera's internal recording settings (SD card or
NVR storage) via the Baichuan API. Supported settings include:

- Recording schedule (continuous, motion-triggered, scheduled)
- Event recording length
- Smart detection sensitivity
- Privacy mask regions

These settings are optional and do not affect Episode's own recording, which
continues to run through the RTSP media pipeline independently.

## Troubleshooting

- **HTTP 401 or connection refused on port 9000:** verify the IP address,
  port, and credentials. The Baichuan API must be enabled on the camera.
- **Connection succeeds but no events:** enable motion or person detection
  rules on the camera. Baichuan exposes configured camera events; it does not
  create detection rules.
- **No snapshot:** the API may not return a snapshot URL for all camera models.
  Recording can still work without snapshots.
- **Stream unavailable:** check that the camera permits concurrent streams and
  inspect the selected RTSP URI on the Device page.
- **Events arrive but recordings do not start:** verify the RTSP URI and
  credentials from the Episode host. Check that the camera permits another
  concurrent stream and inspect FFmpeg errors in the Episode logs.
- **Long-poll subscription drops frequently:** check network stability between
  Episode and the camera. The plugin reconnects automatically but frequent
  drops may indicate a firewall or NAT timeout issue.
- **TCP push not working:** the camera must be configured with the correct
  Episode host address and push port. The Episode host must be reachable from
  the camera on the push port.
- **Changes appear saved but are not active:** check the System page for the
  Reolink Baichuan integration state and inspect the Episode logs for its
  Device entry.
- **SD card errors:** the API reports SD card health. Check the device detail
  page for storage warnings.

## Compatibility notes

The following camera families are known to work with the Baichuan API:

- **Reolink Rio** series (Rio 520, Rio 820, etc.)
- **Reolink TrackMix** series (TrackMix 4MP, 5MP, 8MP)
- **Reolink Duo** series (Duo 2, Duo 3, Duo 3 PoE)
- **Reolink RLC** series (RLC-1100, RLC-410, RLC-510A, RLC-520, RLC-810A,
  RLC-820A, RLC-840A, etc.)
- **Reolink RLC-8xx** series (810A, 820A, 840A)
- **Reolink X** series (X2, X5, X8)
- **Reolink E** series (E1 Pro, E1 Zoom, E2 Pro, E5)
- **Reolink Doorbell** series (Doorbell 2, Doorbell 2 PoE, Doorbell 5,
  Doorbell 5 PoE, Doorbell Watch)
- **Reolink Argus** series (Argus 3, Argus 4, Argus Eco, Argus 3 Pro,
  Argus 3 Pro PoE)

Firmware version 4.1.0 or later is recommended for full Baichuan API support.
Older firmware versions may support basic device info and stream discovery
but lack event subscription or advanced PTZ features.
