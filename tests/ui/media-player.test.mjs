import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const source = await readFile(
  new URL("../../src/episode/ui/media-player.js", import.meta.url),
  "utf8",
);
const media = await import(moduleUrl(source));

function fakeVideo({ nativeHls = false } = {}) {
  const listeners = new Map();
  return {
    src: "",
    canPlayType: type => nativeHls && type.includes("mpegurl") ? "probably" : "",
    addEventListener: (name, handler) => listeners.set(name, handler),
    removeEventListener: name => listeners.delete(name),
    removeAttribute(name) { if (name === "src") this.src = ""; },
    load() {},
    listeners,
  };
}

test("legacy MP4 recordings use the native video element", () => {
  globalThis.window = {};
  const video = fakeVideo();
  const states = [];
  const detach = media.attachMediaSource(video, "/api/v1/evidence/one/file", {
    onState: state => states.push(state.state),
  });

  assert.equal(video.src, "/api/v1/evidence/one/file");
  video.listeners.get("playing")();
  assert.deepEqual(states, ["ready"]);
  detach();
});

test("replacing a native source cleans up the previous attachment", () => {
  globalThis.window = {};
  const video = fakeVideo();

  media.attachMediaSource(video, "/api/v1/evidence/one/file");
  media.attachMediaSource(video, "/api/v1/evidence/two/file");

  assert.equal(video.src, "/api/v1/evidence/two/file");
  assert.equal(video.listeners.size, 4);
  media.detachMediaSource(video);
  assert.equal(video.src, "");
  assert.equal(video.listeners.size, 0);
});

test("missing HLS fallback reports an actionable unavailable state", () => {
  globalThis.window = {};
  const video = fakeVideo();
  const states = [];
  media.attachMediaSource(video, "/api/v1/recordings/one/index.m3u8", {
    onState: state => states.push(state),
  });

  assert.equal(video.src, "");
  assert.equal(states[0].state, "unavailable");
  assert.match(states[0].message, /internet access/i);
});

test("finalized HLS network failures are reported instead of retried forever", () => {
  class FakeHls {
    static Events = { MANIFEST_PARSED: "manifest", FRAG_BUFFERED: "fragment", ERROR: "error" };
    static ErrorTypes = { NETWORK_ERROR: "network", MEDIA_ERROR: "media" };
    static isSupported() { return true; }

    constructor() {
      this.handlers = new Map();
      this.startCalls = 0;
      FakeHls.instance = this;
    }

    on(name, handler) { this.handlers.set(name, handler); }
    loadSource() {}
    attachMedia() {}
    startLoad() { this.startCalls += 1; }
    recoverMediaError() {}
    destroy() {}
  }
  globalThis.window = { Hls: FakeHls };
  const states = [];
  media.attachMediaSource(fakeVideo(), "/api/v1/recordings/one/index.m3u8", {
    onState: state => states.push(state),
  });
  FakeHls.instance.handlers.get("error")("error", {
    fatal: true,
    type: "network",
  });

  assert.equal(states.at(-1).state, "error");
  assert.match(states.at(-1).message, /incomplete|unavailable/i);
  assert.equal(FakeHls.instance.startCalls, 0);
});
