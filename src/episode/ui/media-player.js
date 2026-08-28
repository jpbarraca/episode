const players = new WeakMap();

export function isHlsEvidence(evidence) {
  return evidence?.metadata?.format === "hls-fmp4"
    || evidence?.mime_type === "application/vnd.apple.mpegurl";
}

export function evidenceMediaUrl(evidence) {
  return isHlsEvidence(evidence)
    ? `/api/v1/recordings/${encodeURIComponent(evidence.id)}/index.m3u8`
    : `/api/v1/evidence/${encodeURIComponent(evidence.id)}/file`;
}

export function attachMediaSource(video, url, { live = false } = {}) {
  detachMediaSource(video);
  if (video.canPlayType("application/vnd.apple.mpegurl")) {
    video.src = url;
    return () => {
      video.removeAttribute("src");
      video.load();
    };
  }
  if (!window.Hls?.isSupported()) {
    video.src = url;
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
  player.loadSource(url);
  player.attachMedia(video);
  players.set(video, player);
  return () => detachMediaSource(video);
}

export function detachMediaSource(video) {
  const player = players.get(video);
  if (player) {
    player.destroy();
    players.delete(video);
  }
}
