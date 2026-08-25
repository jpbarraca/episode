import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const moduleUrl = source =>
  "data:text/javascript;base64," + Buffer.from(source).toString("base64");
const source = await readFile(
  new URL("../../src/episode/ui/inventory-pages.js", import.meta.url),
  "utf8",
);

const apiUrl = moduleUrl(`
  export const API = "/api/v1";
  export async function api(path) { return globalThis.systemResponses[path]; }
  export async function apiRequest(path, options) {
    globalThis.systemRequests.push({ path, options });
  }
`);
const componentsUrl = moduleUrl(`
  export function detailMetric() { return ""; }
  export function eventSourceBadges() { return ""; }
  export function pageHeader(value) { return "<header><h2>" + value.title + "</h2></header>"; }
  export function sectionHeading() { return ""; }
`);
const dialogsUrl = moduleUrl(`
  export function confirmDialog(options) { globalThis.systemConfirmation = options; }
  export function notify(message) { globalThis.systemNotification = message; }
`);
const domUrl = moduleUrl(`
  export function escHtml(value) { return String(value ?? ""); }
`);
const formatUrl = moduleUrl(`
  export function fmtBytes(value) { return String(value ?? 0); }
  export function fmtShort(value) { return String(value ?? ""); }
  export function plural(value, label) { return value + " " + label; }
  export function titleCase(value) { return String(value ?? ""); }
`);
const timelineUrl = moduleUrl(`export function eventTitle() { return "Event"; }`);
const inventoryUrl = moduleUrl(`
  export function confirmAreaDelete() {}
  export function confirmDeviceDelete() {}
  export function openAreaEditor() {}
  export function openDeviceEditor() {}
`);
const viewUrl = moduleUrl(`
  export function showContent(html) { globalThis.systemHtml = html; }
  export function showError(error) { throw new Error(error); }
  export function showLoading() {}
`);

globalThis.window = {};
globalThis.FormData = class {
  constructor(form) { this.form = form; }
  get(name) { return this.form[name]; }
};
globalThis.systemRequests = [];
globalThis.systemResponses = {
  "/diagnostics": {
    status: {
      version: "0.1.0-beta.3",
      state: "healthy",
      active_recordings: 0,
      integrations: { healthy: 0, total: 0 },
    },
    services: [],
    integrations: [],
    storage: { data_bytes: 0, filesystem_free_bytes: 1000 },
  },
  "/settings/retention": {
    retention_days: 30,
    notice: "Retention requirements vary by jurisdiction and use case.",
  },
};

const module = await import(moduleUrl(
  source
    .replace('"./api.js?v=3"', JSON.stringify(apiUrl))
    .replace('"./components.js?v=4"', JSON.stringify(componentsUrl))
    .replace('"./dialogs.js?v=1"', JSON.stringify(dialogsUrl))
    .replace('"./dom.js"', JSON.stringify(domUrl))
    .replace('"./format.js?v=4"', JSON.stringify(formatUrl))
    .replace('"./timeline.js?v=5"', JSON.stringify(timelineUrl))
    .replace('"./inventory.js?v=6"', JSON.stringify(inventoryUrl))
    .replace('"./view.js?v=1"', JSON.stringify(viewUrl)),
));

test("System exposes one global visual Evidence retention setting", async () => {
  await module.systemStatus();

  assert.match(globalThis.systemHtml, /name="retention_days"/);
  assert.match(globalThis.systemHtml, /value="30"/);
  assert.match(globalThis.systemHtml, /requirements vary by jurisdiction/);
  assert.match(globalThis.systemHtml, /class="section system-retention"/);
  assert.match(globalThis.systemHtml, /class="form-grid system-retention-form"/);

  globalThis.window.saveRetention({ retention_days: "15" });
  assert.match(globalThis.systemConfirmation.message, /permanently delete/);
  await globalThis.systemConfirmation.onConfirm();

  assert.deepEqual(globalThis.systemRequests, [{
    path: "/settings/retention",
    options: { method: "PUT", body: { retention_days: 15 } },
  }]);
  assert.equal(globalThis.systemNotification, "Visual Evidence retention updated");
});
