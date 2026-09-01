/**
 * Settings: where to send recordings, and which workspace key to use.
 *
 * The consent policy is shown here read-only. It is deliberately not editable
 * from the browser: what may be recorded is a workspace decision, and letting
 * the recorder widen its own boundary would defeat the point.
 */
const $ = (id) => document.getElementById(id);

async function load() {
  const stored = await chrome.storage.local.get(["apiBase", "apiKey"]);
  $("apiBase").value = stored.apiBase || "http://localhost:8000";
  $("apiKey").value = stored.apiKey || "";
  showPolicy();
}

async function showPolicy() {
  const base = ($("apiBase").value || "").replace(/\/+$/, "");
  const key = $("apiKey").value;
  if (!base) return;

  try {
    const response = await fetch(`${base}/v1/consent`, {
      headers: key ? { "X-API-Key": key } : {},
    });
    if (!response.ok) throw new Error(String(response.status));
    const policy = await response.json();

    $("policy").hidden = false;
    $("policy-body").innerHTML = `
      <p class="muted">${policy.note}</p>
      <p><strong>Origins recorded</strong><br />${
        policy.allowed_origins.length
          ? policy.allowed_origins.map((o) => `<span class="pill">${o}</span>`).join("")
          : '<span class="muted">none — nothing can be recorded</span>'
      }</p>
      ${
        policy.blocked_origins.length
          ? `<p><strong>Blocked</strong><br />${policy.blocked_origins
              .map((o) => `<span class="pill">${o}</span>`)
              .join("")}</p>`
          : ""
      }
      <p class="muted">
        Screenshots: ${policy.screenshots_captured ? "captured" : "never"} ·
        Keystrokes: ${policy.keystrokes_captured ? "captured" : "never"} ·
        Typed values: ${policy.values_stored ? "stored" : "hashed only"}
      </p>`;
  } catch {
    $("policy").hidden = false;
    $("policy-body").innerHTML =
      '<p class="muted">Could not reach the workspace. Check the address and key.</p>';
  }
}

$("save").addEventListener("click", async () => {
  await chrome.storage.local.set({
    apiBase: $("apiBase").value.trim(),
    apiKey: $("apiKey").value.trim(),
  });
  $("saved").hidden = false;
  setTimeout(() => ($("saved").hidden = true), 2000);
  showPolicy();
});

load();
