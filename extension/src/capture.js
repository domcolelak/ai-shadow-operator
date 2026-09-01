/**
 * Capture logic, kept free of DOM and Chrome APIs so it can be unit-tested.
 *
 * This is the client-side half of the consent boundary. The backend enforces
 * the same rules again on arrival — but a guarantee that only holds server-side
 * means the data still left the machine. Anything this module refuses is never
 * transmitted at all.
 *
 * The rules mirror `backend/app/capture/model.py` deliberately. Where they
 * differ, the stricter one wins: this side may drop something the server would
 * have accepted, never the reverse.
 */

/** Field names that must never yield a value, in any form. */
export const SENSITIVE_FIELD_PATTERNS = [
  "password",
  "passwd",
  "pwd",
  "secret",
  "token",
  "otp",
  "mfa",
  "2fa",
  "cvv",
  "cvc",
  "card",
  "iban",
  "ssn",
  "national_id",
  "pin",
  "security_answer",
  "api_key",
  "private_key",
];

export const SENSITIVE_INPUT_TYPES = new Set(["password"]);

export const SENSITIVE_AUTOCOMPLETE = new Set([
  "current-password",
  "new-password",
  "cc-number",
  "cc-csc",
  "cc-exp",
  "one-time-code",
]);

/** Events we never record, whatever else is true. */
export const REFUSED_EVENT_TYPES = new Set(["keydown", "keyup", "keypress"]);

const EMAIL = /[\w.+-]+@[\w-]+\.[\w.]+/g;
const LONG_DIGITS = /(?<!\d)\d{9,}(?!\d)/g;

/** Strip direct identifiers from any free text we do keep. */
export function redact(text) {
  if (!text) return "";
  return String(text).replace(EMAIL, "[email]").replace(LONG_DIGITS, "[number]");
}

/** Host of a URL or origin, lowercased. Empty when unparseable. */
export function hostOf(value) {
  if (!value) return "";
  try {
    const url = value.includes("//") ? new URL(value) : new URL(`https://${value}`);
    return url.hostname.toLowerCase();
  } catch {
    return "";
  }
}

function hostMatches(host, pattern) {
  const target = hostOf(pattern) || String(pattern).toLowerCase().replace(/^\./, "");
  if (!target) return false;
  return host === target || host.endsWith(`.${target}`);
}

/**
 * Whether an origin may be recorded.
 *
 * An empty allowlist permits nothing. That is the safe default, and it is the
 * reason the extension records nothing until somebody configures it.
 */
export function originAllowed(origin, policy) {
  const host = hostOf(origin);
  if (!host) return false;
  const blocked = policy?.blocked_origins ?? [];
  if (blocked.some((pattern) => hostMatches(host, pattern))) return false;
  const allowed = policy?.allowed_origins ?? [];
  return allowed.some((pattern) => hostMatches(host, pattern));
}

/**
 * Whether a field's value must never be captured.
 *
 * Checks the input type, the autocomplete hint and the field's names. Any one
 * of them is enough: a password field named "pw" with no autocomplete hint is
 * still a password field.
 */
export function isSensitiveField(descriptor, policy) {
  const inputType = String(descriptor?.input_type ?? "").toLowerCase();
  if (SENSITIVE_INPUT_TYPES.has(inputType)) return true;

  const autocomplete = String(descriptor?.autocomplete ?? "").toLowerCase();
  if (SENSITIVE_AUTOCOMPLETE.has(autocomplete)) return true;

  const haystack = `${descriptor?.field_name ?? ""} ${descriptor?.accessible_name ?? ""}`
    .toLowerCase();
  const extra = (policy?.extra_sensitive_fields ?? []).map((s) => String(s).toLowerCase());
  return [...SENSITIVE_FIELD_PATTERNS, ...extra].some((pattern) => haystack.includes(pattern));
}

/** Path only. Query strings routinely carry identifiers, tokens and search terms. */
export function pathOf(url) {
  if (!url) return "";
  try {
    return new URL(url).pathname || "/";
  } catch {
    return String(url).split("?")[0];
  }
}

/**
 * Build the event the backend expects, or return null to refuse it.
 *
 * `reason` on the refusal is kept by the caller for the popup's "what was not
 * recorded" list; the event itself is discarded here and never queued.
 */
export function buildEvent(raw, policy) {
  if (REFUSED_EVENT_TYPES.has(raw?.domEventType)) {
    return { refused: "keystroke events are never recorded" };
  }
  if (!originAllowed(raw?.origin, policy)) {
    return { refused: `origin '${hostOf(raw?.origin) || "(none)"}' is not on the allowlist` };
  }

  const element = {
    role: String(raw?.element?.role ?? ""),
    accessible_name: redact(raw?.element?.accessible_name ?? ""),
    field_name: String(raw?.element?.field_name ?? ""),
    input_type: String(raw?.element?.input_type ?? ""),
    autocomplete: String(raw?.element?.autocomplete ?? ""),
  };

  const event = {
    action: raw.action,
    origin: raw.origin,
    timestamp: raw.timestamp,
    page_title: redact(raw?.page_title ?? ""),
    element,
  };

  if (raw.target) event.target = pathOf(raw.target);

  if (raw.action === "input" || raw.action === "select") {
    // The value is attached only for fields that are not sensitive. For the
    // rest the step is still sent — so the workflow makes sense — with nothing
    // derived from what was typed.
    if (!isSensitiveField(element, policy)) {
      event.value = raw.value ?? "";
    }
  }

  return { event };
}

/**
 * Describe an element semantically.
 *
 * Role and accessible name, never a CSS path: a generated selector binds the
 * automation to markup that will change, and the path itself can embed page
 * content.
 */
export function describeElement(node) {
  if (!node) return { role: "", accessible_name: "", field_name: "", input_type: "" };

  const tag = (node.tagName || "").toLowerCase();
  const explicitRole = node.getAttribute?.("role");
  const inputType = (node.getAttribute?.("type") || "").toLowerCase();

  let role = explicitRole || "";
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

/**
 * The element's accessible name, by the usual precedence.
 *
 * Never falls back to the element's own text for an input, because a filled
 * field's text is its value — which is exactly what must not travel.
 */
export function accessibleName(node) {
  const label = node.getAttribute?.("aria-label");
  if (label) return label.trim();

  const labelledBy = node.getAttribute?.("aria-labelledby");
  if (labelledBy && node.ownerDocument) {
    const target = node.ownerDocument.getElementById(labelledBy);
    if (target?.textContent) return target.textContent.trim().slice(0, 200);
  }

  const tag = (node.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") {
    if (node.labels?.length && node.labels[0].textContent) {
      return node.labels[0].textContent.trim().slice(0, 200);
    }
    const placeholder = node.getAttribute?.("placeholder");
    if (placeholder) return placeholder.trim().slice(0, 200);
    // Deliberately not node.value.
    return node.getAttribute?.("name") || "";
  }

  const title = node.getAttribute?.("title");
  if (title) return title.trim().slice(0, 200);
  return (node.textContent || "").trim().replace(/\s+/g, " ").slice(0, 200);
}

/**
 * Batch events for sending.
 *
 * Batching is not only about efficiency: it means the extension holds events in
 * memory briefly, so stopping a recording can drop anything not yet sent.
 */
export function makeQueue({ maxBatch = 40 } = {}) {
  let pending = [];
  const refusals = [];

  return {
    add(result) {
      if (result.refused) {
        if (!refusals.includes(result.refused)) refusals.push(result.refused);
        return false;
      }
      pending.push(result.event);
      return true;
    },
    takeBatch() {
      const batch = pending.slice(0, maxBatch);
      pending = pending.slice(maxBatch);
      return batch;
    },
    get size() {
      return pending.length;
    },
    get refusals() {
      return [...refusals];
    },
    /** Drop everything not yet sent. Used when a recording is stopped. */
    discard() {
      const dropped = pending.length;
      pending = [];
      return dropped;
    },
  };
}
