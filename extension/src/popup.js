/**
 * Popup: start and stop a recording, and show exactly what is being kept.
 *
 * The counts shown are the ones the backend actually accepted, not what the
 * extension queued. If the two ever disagree the user should see the smaller,
 * truthful number.
 */
const $ = (id) => document.getElementById(id);

function ask(message) {
  return new Promise((resolve) => chrome.runtime.sendMessage(message, resolve));
}

function render(status) {
  const box = $("status");
  if (status?.active) {
    box.innerHTML = `
      <p class="recording"><span class="dot"></span>Recording</p>
      <div class="counts">
        <div><div class="count-value">${status.accepted}</div><div class="count-label">kept</div></div>
        <div><div class="count-value">${status.refused}</div><div class="count-label">refused</div></div>
        <div><div class="count-value">${status.queued}</div><div class="count-label">queued</div></div>
      </div>`;
    $("toggle").textContent = "Stop recording";
    $("toggle").classList.add("stop");
  } else {
    box.innerHTML =
      '<p class="muted">Not recording. Nothing on this machine is being captured.</p>';
    $("toggle").textContent = "Start recording";
    $("toggle").classList.remove("stop");
  }

  const origins = status?.policy?.allowed_origins ?? [];
  $("origins").hidden = origins.length === 0;
  $("origin-list").innerHTML = origins
    .map((origin) => `<span class="pill">${origin}</span>`)
    .join("");

  const refusals = status?.refusals ?? [];
  $("refusals").hidden = refusals.length === 0;
  $("refusal-list").innerHTML = refusals.map((reason) => `<li>${reason}</li>`).join("");
}

async function refresh() {
  render(await ask({ type: "status" }));
}

$("toggle").addEventListener("click", async () => {
  const button = $("toggle");
  button.disabled = true;
  $("error").hidden = true;

  const status = await ask({ type: "status" });
  const result = await ask(status?.active ? { type: "stop" } : { type: "start" });

  if (!result?.ok) {
    $("error").textContent = result?.error ?? "Something went wrong.";
    $("error").hidden = false;
  }
  button.disabled = false;
  refresh();
});

$("options").addEventListener("click", (event) => {
  event.preventDefault();
  chrome.runtime.openOptionsPage();
});

refresh();
setInterval(refresh, 2000);
