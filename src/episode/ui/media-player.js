const sourceCleanups = new WeakMap();

function isHlsUrl(url) {
  return /\.m3u8(?:$|[?#])/.test(String(url || ""));
}

function listen(video, event, handler) {
  video.addEventListener(event, handler);
  return () => video.removeEventListener(event, handler);
}

function nativeSource(video, url, notify) {
  const cleanups = [
    listen(video, "loadstart", () => notify("loading", "Loading recording…")),
    listen(video, "waiting", () => notify("buffering", "Buffering recording…")),
    listen(video, "playing", () => notify("ready", "Playback ready")),
    listen(video, "error", () => notify(
      "error",
      "This recording cannot be played. The codec may not be supported by this browser.",
    )),
  ];
  video.src = url;
  return () => {
    cleanups.forEach(cleanup => cleanup());
    video.removeAttribute("src");
    video.load();
  };
}

export function isHlsEvidence(evidence) {
  return evidence?.metadata?.format === "hls-fmp4"
    || evidence?.mime_type === "application/vnd.apple.mpegurl";
}

export function evidenceMediaUrl(evidence) {
  return isHlsEvidence(evidence)
    ? `/api/v1/recordings/${encodeURIComponent(evidence.id)}/index.m3u8`
    : `/api/v1/evidence/${encodeURIComponent(evidence.id)}/file`;
}

export function updateMediaStatus(element, { state, message }) {
  if (!element) return;
  const visible = !["ready", "idle"].includes(state);
  element.className = `media-playback-status media-state-${state}${visible ? "" : " hidden"}`;
  element.textContent = message || "";
}

export function attachMediaSource(video, url, { live = false, onState = () => {} } = {}) {
  detachMediaSource(video);
  const notify = (state, message) => onState({ state, message });
  if (!isHlsUrl(url) || video.canPlayType("application/vnd.apple.mpegurl")) {
    const cleanup = nativeSource(video, url, notify);
    sourceCleanups.set(video, cleanup);
    return () => detachMediaSource(video);
  }
  if (!window.Hls?.isSupported()) {
    notify(
      "unavailable",
      "HLS playback support could not be loaded. Check this browser's internet access.",
    );
    return () => {
      video.removeAttribute("src");
      video.load();
    };
  }
  const player = new window.Hls({
    enableWorker: true,
    lowLatencyMode: false,
    liveDurationInfinity: live,
  });
  const handleManifest = () => notify("ready", "Playback ready");
  const handleFragment = () => notify("ready", live ? "Live recording" : "Playback ready");
  const handleError = (_event, data = {}) => {
    if (!data.fatal) {
      if (data.type === window.Hls.ErrorTypes?.NETWORK_ERROR) {
        notify("buffering", live ? "Waiting for the next recording fragment…" : "Buffering recording…");
      }
      return;
    }
    if (data.type === window.Hls.ErrorTypes?.NETWORK_ERROR) {
      if (live) {
        notify("reconnecting", "Recording stream interrupted · retrying playback…");
        player.startLoad();
      } else {
        notify("error", "Recording media is incomplete or temporarily unavailable.");
      }
      return;
    }
    if (data.type === window.Hls.ErrorTypes?.MEDIA_ERROR) {
      notify("reconnecting", "Browser media decoder interrupted · recovering…");
      player.recoverMediaError();
      return;
    }
    notify(
      "error",
      "This recording cannot be played. The codec may not be supported by this browser.",
    );
  };
  player.on(window.Hls.Events.MANIFEST_PARSED, handleManifest);
  player.on(window.Hls.Events.FRAG_BUFFERED, handleFragment);
  player.on(window.Hls.Events.ERROR, handleError);
  notify("loading", "Loading recording…");
  player.loadSource(url);
  player.attachMedia(video);
  sourceCleanups.set(video, () => player.destroy());
  return () => detachMediaSource(video);
}

export function detachMediaSource(video) {
  const cleanup = sourceCleanups.get(video);
  if (cleanup) {
    sourceCleanups.delete(video);
    cleanup();
  }
}
