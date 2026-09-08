# Local verification — 2026-09-08

The standalone dispatcher was built and tested in its own directory and virtual
environment. Deck Lab source and production configuration were not modified.

## Verified

- Dispatcher unit/provider tests and real Docker tests: **56 passing** after the
  Cursor integration, including an entirely offline feedback-to-artifact demo.
- Lint and formatting checks pass. The editable package and webhook-server extra
  install successfully; `sdlc-dispatcher` is available in `.venv/bin/`.
- A real Docker worker runs non-root with a read-only root filesystem, cannot read
  the host Docker socket, and cannot connect directly to external IPs. Its model
  proxy refuses unrelated origins. Checks have no network access. Timed-out
  containers are removed before the worker returns.
- Draft-PR creation/reconciliation is tested against a fake GitHub API, including
  a lost creation response. No live GitHub write was made.
- Linear signature verification, replay rejection, routing, owner approval,
  duplicate delivery, and fresh-issue validation are tested with fixtures. The
  first live widget report also arrived successfully through Linear (see below).
- Deck Lab committed source `e5f7fe0e5b60ed89f0591ad373a56b4f698784f4` passes
  ruff, Black, mypy (**134 source files, no issues**), and **1,331 tests**, with
  **31 opt-in tests skipped**, in the new offline development image. No production
  database or external model was used. The successful mypy rerun used the same
  source commit and immutable image as the other successful checks.
- The local preview passed real HTTP health, sign-in, CSRF-protected deck creation,
  editor, research, profile and admin smoke checks using synthetic accounts/data.
  Embedded browser access was unavailable; this was HTTP verification, not a
  visual UI review.

## Memory gate resolved

The first mypy process exited with code 137 in a Colima VM reporting about 2 GB
total RAM. After the user authorized a restart, Colima was restarted with **4 GB**.
Host inspection showed this Mac has 8 GB total, so allocating all 8 GB to the VM
was avoided. All seven previously running containers were restored, and the
synthetic preview passed its HTTP smoke test again.

The full isolated mypy check then passed in **29.75 seconds**, with a measured
process peak of **1,593.84 MiB (1.56 GiB)** and cgroup/container peak of
**1,940.34 MiB (1.89 GiB)**. The latter includes charged container memory such as
the temporary cache. These measurements explain why a roughly 2 GB VM shared
with other services was tight; they do not establish that an 8 GB VM is needed.
The 4 GB worker ceiling and VM allocation are sufficient for this measured run.

No global Docker pruning was performed. Only explicitly identified unused cache
records from this task's superseded build were removed after the larger image
encountered the VM's disk limit. The corrected image loads, and its Codex CLI was
smoke-tested without a model request.

## Activation still required

The Cursor key and Linear read key are installed and validated. Before the first
paid pilot, approve the report and choose a provider spending limit. Before draft
PR publication, supply a publisher credential and verify repository protections
and external deployment hooks. Real coding-model execution, GitHub publication,
and hosted staging remain unverified. Automatic intake is off. Production
deployment is a separate protected workflow, not implemented in this dispatcher.

### Receiver preparation — 2026-09-08

The Mac mini now runs a local-only receiver via a login LaunchAgent. Team/project
IDs were copied from the saved 2026-09-07 production deployment record. Automatic
intake remains disabled. Three new tests passed for private secret storage, hidden
input/restart, and request rate limiting; the ten existing Linear tests also pass.
Lint and formatting pass for the new files. Live localhost HTTP checks confirmed
health 200, webhook 503 without its secret, and 404 for root/admin/state-file paths.
The actual listener is bound only to `127.0.0.1:8787`.

The user installed the signing secret; the local receiver now rejects unsigned
requests with 400. After an initial automatic approval rejection, the user
explicitly approved public Tailscale Funnel exposure of the receiver on port 8787.
The user enabled Funnel at the account level and the authorized activation
completed. Tailscale issued the HTTPS certificate and runs the tunnel in the
background, preserving the private port-8080 route.

Public-ingress verification resolved the hostname through public DNS and connected
directly to the returned global IPv4 address, preserving hostname/SNI and normal
certificate verification. Results: health 200, unsigned webhook 400, signed ignored
health event 200, admin/private-file routes 404. No issue/job was created by the
probe. Evidence is saved in `.dispatcher/https-verification.json` without the
signing secret or signature.

### Cursor worker and first live intake — 2026-09-08

The spoiler-view mana-symbol report arrived as job
`567938521d4a45fea18bb0e6fbfcab6d`, in `needs_review` with zero attempts. A read-only
Linear query using the supplied key confirmed that its current report and routing
match the queued version. The committed `src`, `tests`, and `pyproject.toml` match
the deployed build recorded in that report (`35f1269...`); local main additionally
contains workspace documentation changes.

The initial Deck Lab registration selected Cursor CLI `2026.09.02-c22c1a3` and the exact
account-listed model `gpt-5.6-sol-medium`. The Linux arm64 package is checksum-pinned
and layered on the previously verified development image. Host authentication
succeeded, then model discovery succeeded inside the actual non-root, read-only
worker through a CONNECT proxy allowing only `api2.cursor.sh:443`. No coding prompt
was submitted. Preflight evidence is in
`.dispatcher/artifacts/preflight-d29ebd72b0574b14962795916b0579f3/`.

The worker refuses unavailable models and requires matching provider-reported
startup metadata and a success event before considering output for verification.
These launcher behaviors are tested with fixtures; live paid streaming execution
has not yet been tested. The selected engine/model are included in approval policy
and the private result artifact. Only the Cursor credential enters its worker;
independent checks remain offline and receive no provider credentials.

All **56 dispatcher tests passed**, including five real Docker tests for isolation,
Cursor credential routing, blocked direct/unrelated egress, timeout cleanup, and
the offline end-to-end demo. Lint/format checks pass. Deck Lab passes ruff, Black,
mypy (134 files), and **1,331 tests with 31 opt-in skips** in the new Cursor image:
`sha256:135492650c621252fa2bf0c27317eb7aaf7241988f55b968efb4f9d9474c065e`.
Evidence: `.dispatcher/cursor-deck-lab-verification.json` and
`.dispatcher/cursor-dispatcher-tests.log`.

### First approved Cursor attempt — blocked

The user approved one isolated attempt for the mana-symbol report. Attempt 1
revalidated the live Linear issue and passed all four fresh baseline gates,
including 1,331 tests. Cursor was invoked with `gpt-5.6-sol-medium`, but its startup
model metadata did not equal either the requested ID or the model-list display
label. The launcher stopped it, and the job is `blocked` with one attempt and no
artifact. No second run was started. The worker containers and network were
confirmed removed; no project source, production environment, or PR was changed.

The first launcher discarded the mismatching metadata before logging it, so the
actual reported name is unavailable. Inspection of the pinned CLI shows that the
startup event uses a derived human-readable display name; a label-format mismatch
is possible, but model substitution has not been ruled out. The guard remains
strict. The launcher now records bounded, credential-redacted requested/listed/
reported model metadata before rejecting a mismatch. All eight Cursor-specific
tests pass, including the new diagnostic-and-process-termination test; lint and
format checks pass. Resolving the naming mismatch and any additional paid attempt
remain pending. Cursor usage for the interrupted startup has not been measured.

Private machine-readable evidence is retained under `.dispatcher/`, including
`demo-result.json`, the initial `deck-lab-verification.json`, the measured rerun
`typecheck-verification.json`, combined `deck-lab-verification-combined.json`,
individual check logs and the demo's digest-bound `result.json`/`review.diff`.
That directory is Git-ignored; the initial failed check record is preserved.

### Canonical Cursor model selection and Grok preflight

The registration now selects `grok-4.6[effort=xhigh,fast=false]` for the user's
coding-quality preference within Cursor Models. The launcher uses ACP's
parameterized model picker without a prompt, validates every selected setting
and the persisted canonical selection, and derives the expected runtime label
from that selection. It rejects unavailable settings, changed values, missing
parameters, and runtime name mismatches; it never falls back to Auto.

The actual isolated preflight passed, reporting `Cursor Grok 4.6 Extra High`.
Evidence: `.dispatcher/artifacts/preflight-1639ac4dbe2a4ee9b0fb3fce065ae461/`.
All 53 offline tests passed (five Docker tests skipped), including nine Cursor
tests covering exact settings, substitution rejection, no-prompt preflight,
redacted errors, stream validation, and approval invalidation. Ruff and Black pass.
The existing image and application code are unchanged. No second coding prompt
was submitted and no job claimed; the mana issue remains blocked at attempt one.
The updated launcher's paid streaming path still needs the approved pilot retry.

### User-selected Grok High at normal speed

The user revised the selection to `grok-4.6[effort=high,fast=false]`.
The registration and integration instructions now use High reasoning and normal
speed. Exact model/parameter preflight passed in the isolated worker; evidence:
`.dispatcher/artifacts/preflight-3c9221241af042f8890c297f5642036e/`.
No coding prompt was submitted. The mana issue remains blocked at attempt one.

### Multi-issue pilot: Cursor agent transport repair

The user authorized a few isolated coding runs on Linear issues. Two current
eligible reports were selected within the remaining daily budget: DYL-13 (mana
icons) and DYL-12 (partner commander zone sizing). Their current Linear reports
were read and locally approved; no Linear comments or status updates were sent.

DYL-13 attempt 2 passed all baseline gates and the exact Grok High model guard,
but Cursor's stream tried `agentn.global.api5.cursor.sh` and received a proxy
CONNECT 403. It produced no patch. The proxy previously allowed only
`api2.cursor.sh`, which sufficed for metadata discovery but not agent streaming.

The Cursor allowlist now includes `api2.cursor.sh` plus the seven exact API5
agent hosts documented by Cursor. No wildcards, hosted VM access, or general
Internet access were added. All nine Cursor tests and five actual Docker
isolation tests passed. A credential-free probe also passed CONNECT, certificate
validation, and HTTP/2 negotiation to `agentn.global.api5.cursor.sh` through the
isolated worker. Evidence: `.dispatcher/cursor-agent-connectivity.log`.

The trusted coding prompt now explicitly requires a new regression test that
fails against the original source, matching the existing verification gate.

The DYL-12 coding session completed successfully, but its original queue attempt
was blocked because the review-size limit incorrectly counted complete before/
after files. That calculation now measures the unified diff. All 60 dispatcher
tests pass, including five actual Docker tests. Completed edit records were
recovered as a separate candidate and passed fresh offline verification (1,332
application tests, 31 skips), original-source regression failure, and actual
Chromium dimension checks. The original failed queue status remains intact.
See [the pilot report](pilot-2026-09-08.md) for evidence and limitations.

## September 8: independent Astra review

The [reviewer specification](astra-reviewer-spec.md) and manual isolated runner
are implemented. Dedicated Codex device authentication succeeded in a separate
0700 store with 0600 auth file. Codex's own account interface refreshed credentials
and confirmed Astra availability. The refresh token is never mounted in a worker.

Actual provider metadata confirmed `gpt-6-astra` for a minimal access probe, a
separate DYL-12 patch review, and a final access probe after stricter tool filtering.
These runs required no further interactive login. The final client and proxy pin
CLI 0.153.1, Astra High, standard speed, and reject hosted browsing/connectors.
Tests: **75 passed**, including seven real Docker checks. Ruff and Black passed.
The new checks include actual reviewer containment and timeout cleanup, scoped
packet privacy, artifact tampering, malformed output/citations, contradictory
verdicts, wrong models/routes/capabilities, and unsupported provider tools/events.

DYL-12 review: **pass**, with one low-severity advisory finding that the new
source/formula test should gain a browser regression. Pre-existing zone overlap
and repeated sizing constants were identified separately. Review limitations
explicitly cover synthetic asset interception, one Chromium viewport, missing
artwork, and limited source context. This is not a repository-wide architecture
audit or a production approval.

Private evidence:

- `.dispatcher/reviews/astra-probe-4/metadata.json`
- `.dispatcher/reviews/DYL-12-astra-scoped-1/review.json`
- `.dispatcher/reviews/DYL-12-astra-scoped-1/metadata.json`
- `.dispatcher/reviews/astra-final-access/metadata.json`

Candidate SHA-256:
`eda29a5c8a8c939b161e29e1d78550f8e693149af6a9f9aea0799d0727ecf1f3`.
Review packet SHA-256:
`cb9c0ac4fc03ebcb327d328aa464f9470525ee1126c33a7a58a93e0e9a1bed15`.
Validated result SHA-256:
`c9cc8e79fd72b0c65324569b9952fc62973efd099b4dcbcf450ae83d9d1d6796`.

Initial probes failed closed on omitted Content-Type; the compatibility fix still
requires valid event parsing and exact provider completion metadata. A full-source
packet was stopped before review by the size check and then by automatic approval
review for unrelated private material. The permitted replacement was 492,655 bytes,
containing only three changed files, two relevant templates, and issue evidence.
Packet preparation now includes changed files and explicit context only.

Queue state, automatic intake, daily coding limits, and publishing behavior have
not been changed by this increment. The DYL-12 coding job remains blocked; its
recovered candidate and independent review are separate evidence. See
[reviewer operations](reviewer-operations.md) for remaining integration work.
