/**
 * Service worker: session lifecycle, filtering and transmission.
 *
 * Every action from a content script passes through `buildEvent` — the tested
 * module — before it is queued. Nothing reaches the network without clearing
 * the same rules the backend applies on arrival, so a refusal here means the
 * data never left the machine.
 *
 * The consent policy is fetched from the workspace rather than configured
 * locally, so the allowlist a user sees in the product is the one actually
 * enforced in their browser.
 */
import { buildEvent, makeQueue, originAllowed } from "./capture.js";

const FLUSH_INTERVAL_MS = 4000;
const ALARM_NAME = "shadow-operator-flush";

let state = {
  active: false,
  sessionId: null,
  externalId: null,
  policy: null,
  accepted: 0,
  refused: 0,
  startedAt: null,
};

let queue = makeQueue();

async function settings() {
  const stored = await chrome.storage.local.get(["apiBase", "apiKey"]);
  return {
    apiBase: (stored.apiBase || "http://localhost:8000").replace(/\/+$/, ""),
    apiKey: stored.apiKey || "",
  };
}

async function call(path, options = {}) {
  const { apiBase, apiKey } = await settings();
  const response = await fetch(`${apiBase}/v1${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(apiKey ? { "X-API-Key": apiKey } : {}),
      ...(options.headers || {}),
    },
  });
  if (!response.ok) {
    throw new Error(`${path} → ${response.status}: ${(await response.text()).slice(0, 200)}`);
  }
  return response.json();
}

async function broadcast(active) {
  const tabs = await chrome.tabs.query({});
  for (const tab of tabs) {
    if (!tab.id || !tab.url?.startsWith("http")) continue;
    // A tab that never loaded the content script will reject the message;
    // that is expected, not an error worth surfacing.
    chrome.tabs.sendMessage(tab.id, { type: "recording-state", active }).catch(() => {});
  }
}

async function startRecording({ label = "" } = {}) {
  // The policy comes from the workspace, so what the user consented to in the
  // product is what the browser enforces.
  const policy = await call("/consent");
  if (!policy.allowed_origins?.length) {
    throw new Error(
      "no origins are allowlisted for this workspace, so nothing can be recorded",
    );
  }

  const externalId = `EXT-${Date.now()}`;
  const session = await call("/sessions", {
    method: "POST",
    body: JSON.stringify({
      external_id: externalId,
      device: navigator.userAgent.slice(0, 120),
      label,
    }),
  });

  queue = makeQueue();
  state = {
    active: true,
    sessionId: session.id,
    externalId,
    policy,
    accepted: 0,
    refused: 0,
    startedAt: new Date().toISOString(),
  };
  await chrome.alarms.create(ALARM_NAME, { periodInMinutes: FLUSH_INTERVAL_MS / 60000 });
  await broadcast(true);
  return state;
}

async function flush() {
  if (!state.active || !state.sessionId) return;
  const batch = queue.takeBatch();
  if (!batch.length) return;

  try {
    const result = await call(`/sessions/${state.sessionId}/events`, {
      method: "POST",
      body: JSON.stringify({ events: batch }),
    });
    state.accepted += result.accepted ?? 0;
    // The backend applies the same rules again; anything it refuses is counted
    // here too, so the popup's numbers match what was actually stored.
    state.refused += result.rejected ?? 0;
  } catch (error) {
    console.warn("[shadow-operator] flush failed:", error.message);
  }
}

async function stopRecording() {
  if (!state.active) return state;
  await flush();
  // Anything still queued is dropped rather than sent after the user stopped.
  const dropped = queue.discard();

  try {
    if (state.sessionId) await call(`/sessions/${state.sessionId}/complete`, { method: "POST" });
  } catch (error) {
    console.warn("[shadow-operator] complete failed:", error.message);
  }

  await chrome.alarms.clear(ALARM_NAME);
  await broadcast(false);

  const finished = { ...state, active: false, dropped };
  state = { active: false, sessionId: null, externalId: null, policy: null, accepted: 0, refused: 0, startedAt: null };
  return finished;
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) flush();
});

chrome.runtime.onMessage.addListener((message, sender, respond) => {
  if (message?.type === "action") {
    if (!state.active) return;
    const result = buildEvent({ ...message.payload }, state.policy);
    if (queue.add(result)) {
      // Sent on the next flush.
    } else {
      state.refused += 1;
    }
    return;
  }

  if (message?.type === "who-am-i") {
    const active = state.active && originAllowed(message.origin, state.policy);
    respond({ active });
    return true;
  }

  if (message?.type === "status") {
    respond({
      ...state,
      queued: queue.size,
      refusals: queue.refusals,
    });
    return true;
  }

  if (message?.type === "start") {
    startRecording(message.options || {})
      .then((next) => respond({ ok: true, state: next }))
      .catch((error) => respond({ ok: false, error: error.message }));
    return true;
  }

  if (message?.type === "stop") {
    stopRecording()
      .then((finished) => respond({ ok: true, state: finished }))
      .catch((error) => respond({ ok: false, error: error.message }));
    return true;
  }

  return undefined;
});

// A service worker can be torn down at any time. Losing the queue is
// acceptable — losing the *indicator* is not, so state is re-broadcast on wake.
chrome.runtime.onStartup.addListener(() => broadcast(state.active));
