import { api, apiRequest } from "./api.js?v=3";
import { pageHeader } from "./components.js?v=3";
import { closeDialog, confirmDialog, notify } from "./dialogs.js?v=1";
import { openAreaEditor, openDeviceEditor } from "./inventory.js?v=6";
import { refreshRetentionPolicy } from "./retention-policy.js?v=1";
import { showContent, showError, showLoading } from "./view.js?v=1";

let areas = [];
let devices = [];

export async function onboardingNeeded() {
  const [inventory, retention] = await Promise.all([
    api("/devices?include_disabled=true"),
    api("/settings/retention"),
  ]);
  return inventory.length === 0 || retention.policy_state === "unconfirmed";
}

function step(number, title, description, state, action = "") {
  return `<div class="onboarding-step onboarding-step-${state}">
    <div class="onboarding-step-number">${state === "complete" ? "✓" : number}</div>
    <div class="onboarding-step-copy"><h3>${title}</h3><p>${description}</p>${action}</div>
  </div>`;
}

export async function welcome() {
  showLoading();
  try {
    const [areaList, deviceList, status, retention] = await Promise.all([
      api("/areas?include_disabled=true"),
      api("/devices?include_disabled=true"),
      api("/status"),
      api("/settings/retention"),
    ]);
    areas = areaList;
    devices = deviceList;
    const activeAreas = areas.filter(area => area.enabled);
    const hasArea = activeAreas.length > 0;
    const hasDevice = devices.length > 0;
    const retentionConfirmed = retention.policy_state !== "unconfirmed";
    const ready = hasDevice && retentionConfirmed;

    showContent(`
      ${pageHeader({
        eyebrow: "Welcome to Episode",
        title: ready
          ? "Your evidence workspace is ready"
          : hasDevice
            ? "Confirm your evidence policy"
            : "Connect your first Device",
        description: "Create one physical Area, add a Device, validate what it supports, and let Episode handle correlation and capture.",
        actions: ready ? '<a href="#episodes" class="button button-primary">Review Episodes</a>' : "",
      })}
      <div class="onboarding-layout">
        <section class="onboarding-intro">
          <div class="eyebrow">How Episode thinks</div>
          <h2>Events become Episodes. Evidence stays original.</h2>
          <p>An Area keeps related activity together. Devices contribute Events, recordings, and snapshots without changing the source material Episode received.</p>
          <div class="onboarding-principles">
            <span><strong>Area-scoped</strong> correlation and recording</span>
            <span><strong>ONVIF-first</strong> discovery and media</span>
            <span><strong>Raw-first</strong> immutable provenance</span>
          </div>
        </section>
        <section class="onboarding-steps" aria-label="Setup progress">
          ${step(
            1,
            "Create an Area",
            hasArea
              ? `${activeAreas.length} active ${activeAreas.length === 1 ? "Area defines" : "Areas define"} where activity is correlated.`
              : "Use a real physical boundary such as Front entrance, Garage, or Garden.",
            hasArea ? "complete" : "active",
            hasArea ? "" : '<button class="button button-primary" type="button" onclick="startOnboardingArea()">Create first Area</button>',
          )}
          ${step(
            2,
            "Add and validate a Device",
            hasDevice
              ? `${devices.length} ${devices.length === 1 ? "Device is" : "Devices are"} saved. Configured integrations activate automatically.`
              : "Enter the Device address and credentials, then use Validate and discover before choosing its integrations.",
            hasDevice ? "complete" : hasArea ? "active" : "pending",
            !hasDevice && hasArea ? '<button class="button button-primary" type="button" onclick="startOnboardingDevice()">Add first Device</button>' : "",
          )}
          ${step(
            3,
            "Confirm visual Evidence retention",
            retentionConfirmed
              ? retention.enabled
                ? `Automatic deletion is enabled after ${retention.retention_days} days.`
                : "Automatic deletion is disabled. A persistent warning will remain visible."
              : `OpenEpisode is applying its ${retention.retention_days}-day default. Confirm that it is appropriate for your use case and jurisdiction.`,
            retentionConfirmed ? "complete" : "active",
            retentionConfirmed
              ? ""
              : `<div class="onboarding-actions"><button class="button button-primary" type="button" onclick="confirmDefaultRetention(${retention.retention_days})">Confirm ${retention.retention_days} days</button><a href="#system/storage" class="button button-ghost">Review options</a></div>`,
          )}
          ${step(
            4,
            "Verify connections",
            ready
              ? `${status.integrations.healthy}/${status.integrations.total} integrations are healthy. Episode is ready for its first Event.`
              : hasDevice
                ? "Confirm the retention policy to complete setup."
                : "Saving a Device also activates its selected integrations.",
            ready ? "complete" : hasDevice ? "active" : "pending",
            ready
              ? '<div class="onboarding-actions"><a href="#devices" class="button button-ghost">View Device health</a><a href="#episodes" class="button button-primary">Open Episode</a></div>'
              : "",
          )}
        </section>
      </div>`);
  } catch (error) {
    showError(error.message);
  }
}

window.startOnboardingArea = () => openAreaEditor(null, welcome);
window.startOnboardingDevice = () => openDeviceEditor(
  null,
  areas.filter(area => area.enabled),
  welcome,
);
window.confirmDefaultRetention = retentionDays => {
  confirmDialog({
    title: `Confirm ${retentionDays}-day retention?`,
    message: "OpenEpisode will automatically and permanently delete managed visual Evidence older than this period. Exported and externally stored copies are not covered.",
    confirmLabel: "Confirm retention",
    onConfirm: async () => {
      await apiRequest("/settings/retention", {
        method: "PUT",
        body: { enabled: true, retention_days: retentionDays },
      });
      closeDialog();
      notify("Visual Evidence retention confirmed");
      await refreshRetentionPolicy();
      await welcome();
    },
  });
};
window.refreshOnboarding = welcome;
