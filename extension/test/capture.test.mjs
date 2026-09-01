/**
 * Tests for the extension's capture logic.
 *
 * Run with `node --test` — no dependencies, so this works on a machine with
 * nothing installed. The privacy assertions are written as claims about what
 * must *not* be produced, because that is the property that matters: anything
 * this module emits has already left the guarantee behind.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { describe, it } from "node:test";

import {
  accessibleName,
  buildEvent,
  describeElement,
  hostOf,
  isSensitiveField,
  makeQueue,
  originAllowed,
  pathOf,
  redact,
} from "../src/capture.js";

const POLICY = {
  allowed_origins: ["portal.demo.local", "mail.demo.local"],
  blocked_origins: [],
  extra_sensitive_fields: [],
};

function raw(overrides = {}) {
  return {
    action: "click",
    origin: "https://portal.demo.local",
    timestamp: "2026-04-06T09:00:00.000Z",
    page_title: "Orders",
    element: { role: "button", accessible_name: "Search" },
    ...overrides,
  };
}

/** A minimal stand-in for a DOM node, enough for describeElement. */
function node({ tag = "input", attrs = {}, text = "", labels = [], id = "" } = {}) {
  return {
    tagName: tag.toUpperCase(),
    id,
    textContent: text,
    labels,
    getAttribute: (name) => attrs[name] ?? null,
    ownerDocument: { getElementById: () => null },
  };
}

describe("origin allowlist", () => {
  it("accepts an allowlisted host", () => {
    assert.equal(originAllowed("https://portal.demo.local/orders", POLICY), true);
  });

  it("accepts a subdomain of an allowlisted host", () => {
    assert.equal(originAllowed("https://eu.portal.demo.local", POLICY), true);
  });

  it("refuses anything else", () => {
    assert.equal(originAllowed("https://personal-banking.example.com", POLICY), false);
  });

  it("records nothing when the allowlist is empty", () => {
    assert.equal(originAllowed("https://portal.demo.local", { allowed_origins: [] }), false);
    assert.equal(originAllowed("https://portal.demo.local", {}), false);
  });

  it("lets a block override the allowlist", () => {
    const policy = { allowed_origins: ["demo.local"], blocked_origins: ["payroll.demo.local"] };
    assert.equal(originAllowed("https://payroll.demo.local", policy), false);
    assert.equal(originAllowed("https://portal.demo.local", policy), true);
  });

  it("refuses an unparseable origin", () => {
    assert.equal(originAllowed("", POLICY), false);
    assert.equal(hostOf("not a url"), "");
  });
});

describe("sensitive fields", () => {
  it("recognises a password input type", () => {
    assert.equal(isSensitiveField({ input_type: "password" }, POLICY), true);
  });

  it("recognises a sensitive autocomplete hint", () => {
    assert.equal(isSensitiveField({ autocomplete: "one-time-code" }, POLICY), true);
  });

  for (const name of ["password", "card_number", "cvv", "iban", "api_key", "otp"]) {
    it(`recognises the field name '${name}'`, () => {
      assert.equal(isSensitiveField({ field_name: name }, POLICY), true);
    });
  }

  it("recognises a password field with a short name and no hints", () => {
    assert.equal(isSensitiveField({ field_name: "pwd" }, POLICY), true);
  });

  it("honours the workspace's extra sensitive fields", () => {
    const policy = { ...POLICY, extra_sensitive_fields: ["patient"] };
    assert.equal(isSensitiveField({ field_name: "patient_ref" }, policy), true);
    assert.equal(isSensitiveField({ field_name: "patient_ref" }, POLICY), false);
  });

  it("leaves ordinary fields alone", () => {
    assert.equal(isSensitiveField({ field_name: "order_id" }, POLICY), false);
  });
});

describe("buildEvent", () => {
  it("refuses keystroke events outright", () => {
    const result = buildEvent(raw({ domEventType: "keydown" }), POLICY);
    assert.match(result.refused, /never recorded/);
    assert.equal(result.event, undefined);
  });

  it("refuses a non-allowlisted origin", () => {
    const result = buildEvent(raw({ origin: "https://elsewhere.example.com" }), POLICY);
    assert.match(result.refused, /allowlist/);
  });

  it("attaches a value for an ordinary field", () => {
    const result = buildEvent(
      raw({
        action: "input",
        value: "ORDER-10482",
        element: { role: "textbox", accessible_name: "Search", field_name: "order_id" },
      }),
      POLICY,
    );
    assert.equal(result.event.value, "ORDER-10482");
  });

  it("sends a password step with no value at all", () => {
    const result = buildEvent(
      raw({
        action: "input",
        value: "hunter2-must-never-leave-the-machine",
        element: {
          role: "textbox",
          accessible_name: "Password",
          field_name: "password",
          input_type: "password",
        },
      }),
      POLICY,
    );
    // The step survives so the workflow still makes sense...
    assert.equal(result.event.action, "input");
    assert.equal(result.event.element.accessible_name, "Password");
    // ...but nothing derived from the value goes with it.
    assert.equal("value" in result.event, false);
    assert.equal(JSON.stringify(result.event).includes("hunter2"), false);
  });

  it("drops the query string from a navigation target", () => {
    const result = buildEvent(
      raw({
        action: "navigate",
        target: "https://portal.demo.local/orders?customer=jane&token=abc123",
      }),
      POLICY,
    );
    assert.equal(result.event.target, "/orders");
    assert.equal(result.event.target.includes("token"), false);
  });

  it("redacts identifiers from the accessible name and title", () => {
    const result = buildEvent(
      raw({
        page_title: "Message from jane@example.com",
        element: { role: "button", accessible_name: "Reply to jane@example.com" },
      }),
      POLICY,
    );
    assert.equal(result.event.page_title.includes("@"), false);
    assert.equal(result.event.element.accessible_name.includes("@"), false);
  });
});

describe("redaction", () => {
  it("removes emails and long digit runs", () => {
    assert.equal(redact("write to jane@example.com").includes("@"), false);
    assert.equal(redact("card 4111111111111111").includes("4111"), false);
  });

  it("leaves ordinary text alone", () => {
    assert.equal(redact("Order 4821 shipped"), "Order 4821 shipped");
  });

  it("handles empty input", () => {
    assert.equal(redact(""), "");
    assert.equal(redact(null), "");
  });
});

describe("pathOf", () => {
  it("keeps the path and drops everything else", () => {
    assert.equal(pathOf("https://portal.demo.local/orders/10482/detail?x=1"), "/orders/10482/detail");
  });

  it("handles a bare path", () => {
    assert.equal(pathOf("/orders?x=1"), "/orders");
  });
});

describe("describeElement", () => {
  it("infers a button role", () => {
    assert.equal(describeElement(node({ tag: "button", text: "Search" })).role, "button");
  });

  it("infers a combobox for a select", () => {
    assert.equal(describeElement(node({ tag: "select" })).role, "combobox");
  });

  it("prefers an explicit role attribute", () => {
    assert.equal(describeElement(node({ tag: "div", attrs: { role: "listitem" } })).role, "listitem");
  });

  it("carries the input type and autocomplete through", () => {
    const described = describeElement(
      node({ tag: "input", attrs: { type: "password", autocomplete: "current-password" } }),
    );
    assert.equal(described.input_type, "password");
    assert.equal(described.autocomplete, "current-password");
  });
});

describe("accessibleName", () => {
  it("prefers aria-label", () => {
    assert.equal(accessibleName(node({ attrs: { "aria-label": "Search orders" } })), "Search orders");
  });

  it("falls back to a label element", () => {
    const labelled = node({ labels: [{ textContent: " Order number " }] });
    assert.equal(accessibleName(labelled), "Order number");
  });

  it("falls back to the placeholder", () => {
    assert.equal(accessibleName(node({ attrs: { placeholder: "Order id" } })), "Order id");
  });

  it("never falls back to an input's value", () => {
    // A filled field's text is its value; using it would put typed content in
    // the accessible name, where nothing filters it.
    const filled = node({ tag: "input", text: "ORDER-10482", attrs: { name: "order_id" } });
    const name = accessibleName(filled);
    assert.equal(name, "order_id");
    assert.equal(name.includes("10482"), false);
  });

  it("uses text content for non-input elements", () => {
    assert.equal(accessibleName(node({ tag: "button", text: "  Send  reply " })), "Send reply");
  });
});

describe("queue", () => {
  it("accepts events and reports its size", () => {
    const queue = makeQueue();
    assert.equal(queue.add(buildEvent(raw(), POLICY)), true);
    assert.equal(queue.size, 1);
  });

  it("keeps a refusal reason but not the event", () => {
    const queue = makeQueue();
    const accepted = queue.add(buildEvent(raw({ domEventType: "keydown" }), POLICY));
    assert.equal(accepted, false);
    assert.equal(queue.size, 0);
    assert.equal(queue.refusals.length, 1);
  });

  it("records each distinct refusal once", () => {
    const queue = makeQueue();
    for (let i = 0; i < 5; i += 1) {
      queue.add(buildEvent(raw({ domEventType: "keydown" }), POLICY));
    }
    assert.equal(queue.refusals.length, 1);
  });

  it("takes batches of the configured size", () => {
    const queue = makeQueue({ maxBatch: 2 });
    for (let i = 0; i < 5; i += 1) queue.add(buildEvent(raw(), POLICY));
    assert.equal(queue.takeBatch().length, 2);
    assert.equal(queue.size, 3);
  });

  it("discards anything unsent when a recording stops", () => {
    const queue = makeQueue();
    for (let i = 0; i < 3; i += 1) queue.add(buildEvent(raw(), POLICY));
    assert.equal(queue.discard(), 3);
    assert.equal(queue.size, 0);
  });
});

describe("content script guarantees", () => {
  const source = readFileSync(new URL("../src/content.js", import.meta.url), "utf8");

  it("registers no keyboard listener of any kind", () => {
    // The strongest form of "keystrokes are never recorded" is that no key
    // event is ever received. A filtered listener would still be a listener.
    for (const type of ["keydown", "keyup", "keypress", "beforeinput", "textInput"]) {
      assert.equal(
        source.includes(`"${type}"`) || source.includes(`'${type}'`),
        false,
        `content.js must not reference the '${type}' event`,
      );
    }
  });

  it("never reads an input's value for a sensitive field", () => {
    // The sensitivity check must appear before any `.value` read in onChange.
    const onChange = source.slice(source.indexOf("function onChange"), source.indexOf("function onNavigate"));
    assert.ok(onChange.includes("sensitive"), "onChange must check sensitivity");
    assert.ok(
      onChange.indexOf("const sensitive") < onChange.indexOf("node.value"),
      "the sensitivity check must come before the value is read",
    );
  });

  it("does not build CSS selectors", () => {
    for (const forbidden of ["querySelector", "cssPath", "outerHTML"]) {
      assert.equal(source.includes(forbidden), false, `content.js must not use ${forbidden}`);
    }
  });
});
