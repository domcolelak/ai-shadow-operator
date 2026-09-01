/**
 * End-to-end check of the extension against a running backend.
 *
 * Drives the exact path the service worker takes — fetch the consent policy,
 * start a session, filter events through `buildEvent`, post the survivors,
 * complete the session — and then asks the backend what it actually stored.
 *
 * The point is the last part. Unit tests prove the extension does not *send* a
 * password; this proves the backend did not *receive* one.
 *
 * Usage: node test/e2e.mjs [apiBase]
 */
import { buildEvent, makeQueue } from "../src/capture.js";

const API = (process.argv[2] || "http://localhost:8050").replace(/\/+$/, "");
const SECRET = "hunter2-must-never-leave-the-machine";

let failures = 0;

function check(label, condition, detail = "") {
  const mark = condition ? "ok  " : "FAIL";
  if (!condition) failures += 1;
  console.log(`  ${mark} ${label}${detail ? ` — ${detail}` : ""}`);
}

async function call(path, options = {}) {
  const response = await fetch(`${API}/v1${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!response.ok) {
    throw new Error(`${path} → ${response.status}: ${(await response.text()).slice(0, 200)}`);
  }
  return response.json();
}

/** The events a recorder would emit while answering an order enquiry. */
function pageEvents(origin) {
  const at = (seconds) => new Date(Date.UTC(2026, 3, 6, 9, 0, seconds)).toISOString();
  return [
    {
      action: "navigate",
      origin,
      timestamp: at(0),
      page_title: "Sign in",
      element: { role: "", accessible_name: "" },
      target: `${origin}/login?next=/orders&session=abc123`,
    },
    {
      action: "input",
      origin,
      timestamp: at(2),
      page_title: "Sign in",
      element: {
        role: "textbox",
        accessible_name: "Password",
        field_name: "password",
        input_type: "password",
        autocomplete: "current-password",
      },
      value: SECRET,
    },
    // A raw keystroke, as a badly-behaved recorder might emit.
    {
      action: "input",
      domEventType: "keydown",
      origin,
      timestamp: at(3),
      page_title: "Sign in",
      element: { role: "textbox", accessible_name: "Password" },
      value: "h",
    },
    {
      action: "input",
      origin,
      timestamp: at(6),
      page_title: "Orders",
      element: { role: "textbox", accessible_name: "Search orders", field_name: "order_id" },
      value: "10482",
    },
    {
      action: "click",
      origin,
      timestamp: at(8),
      page_title: "Orders",
      element: { role: "button", accessible_name: "Search" },
    },
    // Activity on a site the workspace never allowlisted.
    {
      action: "navigate",
      origin: "https://personal-banking.example.com",
      timestamp: at(10),
      page_title: "Personal banking",
      element: { role: "", accessible_name: "" },
      target: "https://personal-banking.example.com/accounts",
    },
  ];
}

async function main() {
  console.log(`Extension end-to-end against ${API}\n`);

  const policy = await call("/consent");
  console.log(`policy: ${policy.allowed_origins.join(", ") || "(none)"}\n`);
  check("the workspace allowlists at least one origin", policy.allowed_origins.length > 0);

  const origin = `https://${policy.allowed_origins[0]}`;
  const externalId = `EXT-E2E-${Date.now()}`;
  const session = await call("/sessions", {
    method: "POST",
    body: JSON.stringify({ external_id: externalId, device: "node-e2e", label: "extension check" }),
  });

  // Exactly what the service worker does with each incoming action.
  const queue = makeQueue();
  const raw = pageEvents(origin);
  for (const event of raw) queue.add(buildEvent(event, policy));

  const batch = queue.takeBatch();
  console.log("client-side filtering:");
  check("the keystroke event was refused before sending", queue.refusals.some((r) => /keystroke/.test(r)));
  check("the off-limits origin was refused before sending", queue.refusals.some((r) => /allowlist/.test(r)));
  check("the password step is still sent", batch.some((e) => e.element?.accessible_name === "Password"));
  check(
    "no password value is in the outgoing payload",
    !JSON.stringify(batch).includes(SECRET),
    `${batch.length} events queued`,
  );
  check("the query string was dropped from the navigation", !JSON.stringify(batch).includes("session=abc123"));

  const result = await call(`/sessions/${session.id}/events`, {
    method: "POST",
    body: JSON.stringify({ events: batch }),
  });
  await call(`/sessions/${session.id}/complete`, { method: "POST" });

  console.log("\nwhat the backend accepted:");
  check("every event the extension sent was accepted", result.rejected === 0, `accepted ${result.accepted}`);

  const summary = await call(`/sessions/${session.id}/summary`);
  check("the password step was stored without a value", summary.sensitive_steps_recorded_without_values >= 1);
  check("no values were stored", summary.values_stored === false);
  check("no screenshots were stored", summary.screenshots_stored === false);
  check(
    "only allowlisted origins reached storage",
    summary.origins.every((o) => policy.allowed_origins.some((a) => o === a || o.endsWith(`.${a}`))),
    summary.origins.join(", "),
  );

  // The strongest assertion available: ask the API for everything it holds
  // about this session and look for the secret in the raw response.
  const stored = await fetch(`${API}/v1/sessions`).then((r) => r.text());
  check("the secret appears nowhere in the stored sessions", !stored.includes(SECRET));

  console.log(`\n${failures === 0 ? "PASS" : `FAIL (${failures})`}`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((error) => {
  console.error("e2e failed:", error.message);
  process.exit(1);
});
