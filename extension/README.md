# Recorder extension

The browser half of AI Shadow Operator. Records sessions you explicitly start, on
origins your workspace allowlists, and enforces the privacy guarantees **before
anything is transmitted**.

## Why the filtering happens here as well as on the server

The backend applies the same rules on arrival. That is not enough on its own: a
guarantee that only holds server-side means the data already left the machine. Anything
this extension refuses is never sent at all, and the backend's check becomes a second
line rather than the only one.

Where the two differ, the stricter wins — this side may drop something the server would
have accepted, never the reverse.

## What it will not do

| | |
|---|---|
| **Keystrokes** | No key listener is attached. Not a filtered one — none. `content.js` never references `keydown`, `keyup`, `keypress`, `beforeinput` or `textInput`, and a test asserts that. |
| **Passwords, cards, OTPs** | The sensitivity check runs *before* the value is read, so the value never enters a variable. The step is still recorded, with nothing derived from what was typed. |
| **Screenshots** | Never captured, and there is no code path that could. |
| **Query strings** | Dropped from every URL: they routinely carry identifiers, tokens and search terms. |
| **CSS selectors** | Elements are described by role and accessible name. A generated selector binds automation to markup that will change, and the path itself can embed page content. |
| **Anything off the allowlist** | An empty allowlist records nothing, and that is the default. |

The recording indicator is fixed at the top of the page with the maximum z-index and
`pointer-events: none`. A consent-based recorder whose indicator can be styled away by
the site being recorded is not consent-based.

## Install

1. Run the backend (`uvicorn app.main:app --reload` in `backend/`).
2. Open `chrome://extensions`, enable **Developer mode**, choose **Load unpacked**, and
   select this `extension/` directory.
3. Open the extension's **Settings** and set the API address and your workspace key.
4. Click the toolbar icon and press **Start recording**.

The allowlist is fetched from the workspace, not configured in the browser. Letting the
recorder widen its own boundary would defeat the point, so the settings page shows the
policy read-only.

## Tests

```bash
node --test test/
```

46 tests, no dependencies. They cover the allowlist, sensitive-field detection,
redaction, element description, the queue, and three static guarantees about
`content.js` itself — including that no keyboard event is referenced anywhere in it.

### End-to-end, against a running backend

```bash
node test/e2e.mjs http://localhost:8000
```

Drives the exact path the service worker takes, then asks the backend what it actually
stored. Unit tests prove the extension does not *send* a password; this proves the
backend did not *receive* one — it searches the stored sessions for the secret and fails
if it appears.

## Files

```
manifest.json      MV3 manifest
src/capture.js     pure, tested logic: allowlist, sensitivity, redaction, queue
src/content.js     DOM listeners and the recording indicator
src/background.js  session lifecycle, filtering, batched transmission
src/popup.*        start/stop, live counts, what was refused
src/options.*      API address and workspace key; policy shown read-only
```

`content.js` is a classic content script and cannot import `capture.js`, so the small
amount of element description it needs is duplicated there. Every decision about what
may be *sent* happens in the service worker, which does import the tested module.

## Known limits

- **Not published.** Load it unpacked; there is no store listing and no signing.
- **Single frame.** `all_frames` is off, so interaction inside cross-origin iframes is
  not recorded. Recording inside a frame would need its origin checked separately, and
  getting that wrong is exactly the kind of leak this design exists to prevent.
- **The service worker can be evicted** mid-recording, dropping anything queued but not
  yet flushed. Events are flushed every four seconds to bound the loss; the indicator is
  re-broadcast on wake so a recording never continues invisibly.
- **No icons.** The manifest declares an empty `icons` block; the toolbar shows Chrome's
  default.
