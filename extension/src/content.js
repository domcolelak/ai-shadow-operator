/**
 * Content script: turns page interaction into semantic actions.
 *
 * Three things it deliberately does not do:
 *
 * 1. **It attaches no keyboard listeners at all.** Not filtered ones — none.
 *    The safest way to guarantee a keystroke stream is never recorded is to
 *    never receive one.
 * 2. **It never reads an input's `value` for a sensitive field.** The check
 *    happens before the read, so the value is not even briefly in a variable
 *    that could end up in a log.
 * 3. **It does not run until the background script says a recording is
 *    active**, and it removes its listeners the moment one stops.
 *
 * Note this file is loaded as a classic content script, so it cannot import
 * `capture.js`. The element description is duplicated here in the small form
 * the DOM needs; the decisions about what may be *sent* all happen in the
 * service worker, which does import the tested module.
 */

(() => {
  let recording = false;
  let indicator = null;
  let listeners = [];

  function accessibleName(node) {
    const label = node.getAttribute?.("aria-label");
    if (label) return label.trim().slice(0, 200);

    const labelledBy = node.getAttribute?.("aria-labelledby");
    if (labelledBy) {
      const target = document.getElementById(labelledBy);
      if (target?.textContent) return target.textContent.trim().slice(0, 200);
    }

    const tag = (node.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") {
      if (node.labels?.length && node.labels[0].textContent) {
        return node.labels[0].textContent.trim().replace(/\s+/g, " ").slice(0, 200);
      }
      const placeholder = node.getAttribute("placeholder");
      if (placeholder) return placeholder.trim().slice(0, 200);
      // Never node.value: a filled field's text is its content.
      return node.getAttribute("name") || "";
    }

    const title = node.getAttribute?.("title");
    if (title) return title.trim().slice(0, 200);
    return (node.textContent || "").trim().replace(/\s+/g, " ").slice(0, 200);
  }

  function describe(node) {
    const tag = (node.tagName || "").toLowerCase();
    const inputType = (node.getAttribute?.("type") || "").toLowerCase();
    let role = node.getAttribute?.("role") || "";
    if (!role) {
      if (tag === "button" || (tag === "input" && ["button", "submit", "reset"].includes(inputType))) {
        role = "button";
      } else if (tag === "a") role = "link";
      else if (tag === "select") role = "combobox";
      else if (tag === "textarea") role = "textbox";
      else if (tag === "input") role = inputType === "checkbox" ? "checkbox" : "textbox";
      else if (tag === "li") role = "listitem";
      else role = tag || "generic";
    }
    return {
      role,
      accessible_name: accessibleName(node),
      field_name: node.getAttribute?.("name") || node.id || "",
      input_type: inputType,
      autocomplete: node.getAttribute?.("autocomplete") || "",
    };
  }

  /** The nearest thing that behaves like a control, so a click on an icon counts. */
  function interactiveAncestor(node) {
    let current = node;
    for (let depth = 0; current && depth < 6; depth += 1) {
      const tag = (current.tagName || "").toLowerCase();
      if (["button", "a", "input", "select", "textarea", "li"].includes(tag)) return current;
      if (current.getAttribute?.("role")) return current;
      current = current.parentElement;
    }
    return node;
  }

  function send(action, element, extra = {}) {
    if (!recording) return;
    chrome.runtime.sendMessage({
      type: "action",
      payload: {
        action,
        origin: location.origin,
        timestamp: new Date().toISOString(),
        page_title: document.title,
        element,
        ...extra,
      },
    });
  }

  function onClick(event) {
    const node = interactiveAncestor(event.target);
    if (!node) return;
    send("click", describe(node));
  }

  function onChange(event) {
    const node = event.target;
    if (!node?.tagName) return;
    const element = describe(node);
    const tag = node.tagName.toLowerCase();

    // The sensitivity check happens before the value is read, so a password
    // never enters a variable here at all.
    const sensitive =
      element.input_type === "password" ||
      ["current-password", "new-password", "cc-number", "cc-csc", "one-time-code"].includes(
        element.autocomplete,
      );

    const action = tag === "select" ? "select" : "input";
    if (sensitive) {
      send(action, element);
      return;
    }
    send(action, element, { value: node.value ?? "" });
  }

  function onNavigate() {
    send("navigate", { role: "", accessible_name: "" }, { target: location.href });
  }

  function attach() {
    if (listeners.length) return;
    // No keydown/keyup/keypress listener exists anywhere in this file.
    const bindings = [
      ["click", onClick, true],
      ["change", onChange, true],
    ];
    for (const [type, handler, capture] of bindings) {
      document.addEventListener(type, handler, capture);
      listeners.push([type, handler, capture]);
    }
    window.addEventListener("popstate", onNavigate);
    listeners.push(["popstate", onNavigate, false, window]);
    onNavigate();
  }

  function detach() {
    for (const [type, handler, capture, target] of listeners) {
      (target || document).removeEventListener(type, handler, capture);
    }
    listeners = [];
  }

  function showIndicator(origin) {
    if (indicator) return;
    indicator = document.createElement("div");
    indicator.id = "shadow-operator-indicator";
    indicator.innerHTML =
      '<span class="so-dot"></span>' +
      "<span>Recording this session</span>" +
      `<span class="so-detail">${origin} · no passwords, keystrokes or screenshots</span>`;
    document.documentElement.appendChild(indicator);
    // The banner overlays the page, so push the content down rather than
    // covering whatever is at the top of it.
    document.documentElement.style.setProperty("scroll-padding-top", "36px");
  }

  function hideIndicator() {
    indicator?.remove();
    indicator = null;
  }

  function setRecording(active, origin) {
    if (active === recording) return;
    recording = active;
    if (active) {
      showIndicator(origin || location.host);
      attach();
    } else {
      detach();
      hideIndicator();
    }
  }

  chrome.runtime.onMessage.addListener((message) => {
    if (message?.type === "recording-state") {
      setRecording(Boolean(message.active), message.origin);
    }
  });

  // Ask on load, so a page opened mid-recording picks the state up.
  chrome.runtime.sendMessage({ type: "who-am-i", origin: location.origin }, (response) => {
    if (chrome.runtime.lastError) return;
    setRecording(Boolean(response?.active), location.host);
  });
})();
