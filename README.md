# AI Shadow Operator

Watches work you explicitly asked it to watch, notices what you do over and over, and
offers to do the safe parts for you.

You record a session. It finds the sequence you repeated 34 times, tells you which
fields changed between runs and which stayed the same, marks where your runs diverged,
and compiles the result into an automation that stops and asks you before it does
anything anyone outside the system would see.

---

## The two rules that shape everything

**This is a consent-based productivity tool, not surveillance.** That is enforced in
code at the capture boundary, not promised in a policy document:

- nothing is recorded until somebody starts a session, and an empty origin allowlist —
  the default — records nothing at all;
- password, card, OTP and similarly named fields produce **no stored value in any form**,
  not even a hash: a hash of a password is still a password oracle;
- keystroke events are refused outright, because a keystroke stream reconstructs
  everything the field-level filter would have caught;
- screenshots are never captured;
- a recording is deletable as one operation that leaves nothing behind.

**Repetition is a fact, not an opinion.** The miner decides what repeated; the model may
name and describe a workflow afterwards, but "this happened 34 times" is never something
a language model asserted.

## What it does

0. **Records** through a browser extension that enforces the consent boundary before
   anything is transmitted — see [`extension/`](extension/).
1. **Captures** semantic actions — role and accessible name, never CSS paths — filtered
   through the consent policy on arrival.
2. **Segments** sessions into task-shaped runs, drops noise, and fingerprints each run.
3. **Clusters** runs by edit distance and merges each cluster into a canonical workflow.
4. **Classifies** each field as a variable, a constant, or something that stays manual.
5. **Reports** optional steps and branch points rather than resolving them.
6. **Compiles** an accepted candidate into a restricted DSL.
7. **Executes** it — dry by default, with high-risk steps stopping for a human.

## Quick start

```bash
docker compose up
```

- API and interactive docs: <http://localhost:8000/docs>
- UI: <http://localhost:3000>

The demo seeds 30 recorded sessions of a support agent answering "where is my order",
including a login whose password must not survive capture, keystroke events that must be
refused, and activity on a non-allowlisted personal site that must be rejected at the
door.

### Without Docker

```bash
cd backend && pip install -r requirements-dev.txt && uvicorn app.main:app --reload
```

```bash
cd frontend && npm install && npm run dev
```

## Tests

```bash
cd backend && python -m pytest -q      # 145 tests
cd extension && node --test test/      # 46 tests, no dependencies
```

The privacy guarantees are written as assertions about what must **not** exist in stored
data, and the execution tests assert not only what the engine returned but what it
actually did — including that a dry run did nothing at all.

The extension also has an end-to-end check against a running backend:

```bash
cd extension && node test/e2e.mjs http://localhost:8000
```

Unit tests prove the extension does not *send* a password; this drives the real path and
then searches the stored sessions for the secret, proving the backend did not *receive*
one.

## The execution DSL

Twelve action kinds: `navigate`, `read_text`, `input`, `click`, `wait_for`, `extract`,
`transform`, `condition`, `api_call`, `create_draft`, `approval`, `notify`.

There is no `script`, `eval`, `shell` or generic `http` primitive, and a test asserts
that. An outbound call can only name a connector the workspace has allowlisted, so
adding a destination is an administrative act rather than something a generated workflow
can do to itself.

**Risk is a property of the action kind, not a field somebody sets.** A workflow author
cannot mark a send as low risk — `Action` has no `risk` field at all. A click is
medium by default and escalates to high when the control's accessible name contains
`send`, `submit`, `refund`, `delete`, `confirm` and similar.

## Design decisions worth knowing

Most of these exist because running the thing showed the first version was wrong.

**A per-session salt destroyed constant detection.** Values are stored as salted hashes
so the miner can tell "this differed every run" from "this was always the same" without
knowing either. With a salt per session, the same signature text hashed differently in
every recording and read as a variable with 26 distinct values. The salt is
workspace-wide for exactly this reason; rotating it makes every earlier hash
uncorrelatable, which is the point of having one.

**Paths are generalised before they reach a workflow.** The canonical run recorded
`/orders/17306/detail`. Compiling that literally would have sent every future run to the
one order that happened to be open when the session was recorded.

**Steps done in a minority of runs are kept, not dropped.** The canonical skeleton is the
*most common* sequence, so a step performed 30% of the time is missing from it entirely
— and that is precisely what an optional step is. Dropping it silently would hand the
reviewer a workflow that does not match what they do, and an automation that skips a
check somebody makes one time in three.

**Confidence is measured against the required steps only.** Including the optional ones
made every run look like a poor match for its own workflow, penalising the miner for
having found more.

**A branch is reported, never resolved.** The miner can see runs diverged; it cannot see
why. Guessing is how an automation quietly starts doing the wrong thing in the case
nobody tested, so each branch compiles into an approval.

**A constant compiles to an operator-supplied variable, not a literal.** The value was
never captured — only a hash of it — so there is no literal to emit. Modelling it as a
required input keeps the workflow honestly un-runnable until somebody fills it in,
rather than shipping an input that silently types nothing.

**A trailing high-risk click becomes draft → approval → click.** The last step of most
support workflows is "send". Generating that as a bare click means the first dry run
switched to live sends real mail to real customers.

**A dry run is not "a run with a flag set".** The mutating branches are never reached,
and a test asserts the fake portal recorded zero clicks, zero fills and zero drafts.

**Origins are enforced at run time, not only at validation.** A stored workflow may have
been edited since it was checked; a test edits one after validation to prove the engine
still blocks it.

**Step logs record the variable name and the value's shape, never the value.** An audit
trail can then be kept indefinitely without becoming a data liability.

### Deviations from the original brief, and what is not verified

- **Execution is tested against a simulated driver, not Playwright.** A Playwright driver
  is implemented (`app/execution/drivers/playwright_driver.py`) and is deliberately thin
  — every decision about *whether* an action may run has already been made by the
  engine. It is **not exercised by the test suite**: that needs a browser binary and a
  live site, and a driver whose every call is mocked is not tested at all. What the tests
  do cover, in full, is the engine — approval gates, origin enforcement, dry-run
  isolation, sanitised logging — against an in-process fake portal that records every
  mutating call.
- **The browser extension is unpacked-only.** It is built and tested (`extension/`), but
  there is no store listing, it records only the top frame, and it ships without icons.
- **No isolated executor container.** The Dockerfile runs the API; a production deployment
  would run the Playwright driver in a separate sandboxed container.
- **No client-side data-fetching library.** App Router server components fetch directly,
  keeping the tenant API key on the server.

## Repository layout

```
backend/app/
  core/          config, database session, tenant resolution, structured logging
  capture/       the consent boundary: filtering, hashing, redaction, generalisation
  mining/        segmentation, edit distance, clustering, merging, variable detection
  dsl/           the restricted action schema, risk classification, validation
  workflows/     compiling a mined candidate into the DSL
  execution/     the driver-agnostic engine, plus simulated and Playwright drivers
  sessions/      service layer over recordings and discovery
  ai/            naming and failure explanation (advisory only)
  demo/          30 synthetic sessions with planted patterns and planted violations
frontend/
  app/           Next.js App Router pages (server components)
extension/
  src/capture.js pure, tested filtering: allowlist, sensitivity, redaction, queue
  src/content.js DOM listeners and the always-visible recording indicator
  src/background.js  session lifecycle and batched transmission
```

## The demo data

Eight things it is built to demonstrate, each with a test:

| Planted | Expected |
|---|---|
| The order-lookup workflow, repeated ~37 times | discovered as one candidate |
| Paid vs unpaid reply paths | reported as a branch, not resolved |
| A varying order number | detected as a variable, with its shape |
| A fixed signature | detected as a constant |
| A customer-history detour in ~30% of runs | kept and marked optional |
| An unrelated expense workflow | kept separate |
| A login with a password and keystrokes | no value stored, keystrokes refused |
| Activity on a personal banking site | rejected at capture |

## Multi-tenancy

Every table carries `tenant_id`, every query filters on it, and the tenant is resolved
from an `X-API-Key` header before any handler runs. Cross-tenant access returns 404. The
capture policy is built from the tenant row in the service layer, so no route can record
with a wider policy than the workspace consented to — and a workspace with no allowlist
cannot start a recording at all.

## What is not built

No desktop companion — the extension covers browser work only. No background job queue;
discovery runs synchronously. No scheduled triggers. No connector implementations (the
allowlist and the gating exist; the integrations do not). No RBAC beyond a per-tenant
key.
