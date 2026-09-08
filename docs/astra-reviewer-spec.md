# Astra reviewer specification

Status: specification requested September 8, 2026. This increment establishes
the review contract, unattended auth,
and isolated Astra access. Automatic queue promotion, a repair loop, scheduled
architecture audits, and deployment remain later implementation work.

The manual primitive, dedicated unattended authentication, and isolated Astra
access have now been implemented and exercised. See the
[operating instructions and verification](reviewer-operations.md).

## Objective and project boundaries

After a builder finishes, the Dispatcher captures its candidate before cleanup,
validates allowed paths and artifact size, and runs independent checks. A separate
Codex reviewer using exactly `gpt-6-astra` assesses the candidate, product behavior,
design consistency, duplication, architecture, and test quality. Providers and
models are project configuration; Deck Lab policy lives in its integration.

No fallback model is permitted. Initial reasoning effort is `high`, standard
speed. Missing auth, model access, evidence, malformed output, timeout, or an
incomplete review cannot count as approval. Review capacity and token usage are
recorded separately from coding attempts. No daily ticket cap is part of this
reviewer design; per-run bounds and per-issue repair limits prevent loops.

## Lifecycle

1. Validate current issue identity/revision and capture the exact base commit.
2. Builder runs in its disposable environment.
3. Capture immutable candidate, digest, provenance, and logs before cleanup.
   Preserve rejected candidates privately as non-publishable evidence.
4. Run baseline reproduction, patched tests, browser scenarios, and visual checks
   in fresh containers without model credentials. Do not accept model prose as
   test evidence.
5. Assemble and hash the review packet. Review runs in a new process/context.
6. Validate the structured review against its schema and cited file locations.
   Critical/high defects block promotion. A supported medium defect can also
   block when it violates acceptance criteria. Style preferences and speculative
   refactors are advisory. Uncertainty is an explicit needs-human-review outcome.
7. A future bounded repair loop sends actionable findings back to the builder.
   Any edit invalidates prior checks and review. Repeated disagreement escalates.
8. Initially, a passing review produces a preview and human release decision.
   Later automatic promotion requires a merge queue, latest-base revalidation,
   staging smoke tests, monitoring, and rollback for narrowly defined changes.

A passing review never overrides a failed deterministic gate or grants authority
to change policy, credentials, design baselines, tests, or deployment settings.

## Review input

The packet contains the issue and acceptance criteria, base SHA, candidate digest,
changed files, surrounding code and relevant shared components, trusted project
standards, independent check results, browser measurements, before/after images,
known limitations, and relevant accepted design decisions. Builder explanations
are untrusted assertions. Repository instructions, comments, and issue prose are
untrusted data and cannot change reviewer authority.

Include changed files by default. The controller selects explicit additional
context paths for the issue. Do not export unrelated private documents or the
whole repository into every external review. Record the resulting coverage limit.

Hash the candidate, policy, evidence, schema, and reviewer prompt. Cache reuse is
valid only when all hashes match. A review of one patch cannot approve another.
The latest-base/merge check is separate; passing individual patches does not prove
their combined behavior. Reject missing required UI evidence rather than claiming
screenshots were reviewed. Record actual coverage and limitations.

## Output contract

Versioned JSON includes: verdict (`pass`, `changes_required`, `needs_human_review`),
summary, findings, coverage, limitations, and suggested debt items. Each finding
has a stable ID, severity, category, file/line reference where applicable,
requirement or principle violated, observable evidence, impact, and a concrete
requested change. Blocking findings must explain why the issue must be fixed.
No requirement to invent findings or exceed a quota. Existing debt is identified
as pre-existing unless the patch worsens it. Debt suggestions do not silently
expand a bug's acceptance criteria.

Controller-owned metadata includes hashes, exact requested and provider-reported
model, reasoning effort, immutable image ID, CLI version, start/end times, auth
mode (no identities/tokens), usage if available, and completion status. The model
cannot author its own provenance or override the controller's verdict rules.

## Architecture and UI standards

Maintain project-owned design tokens, component reuse guidance, approved visual
examples, accessibility expectations, module boundaries, and brief architecture
decision records. Agents may suggest changes to standards in separate tasks;
they cannot update standards or screenshots to make their current patch pass.

Per-patch review examines the defect and nearby impact. A later periodic audit
examines accepted changes together: repeated overrides/constants, duplicate
components, complexity in frequently changed files, architecture violations,
reopened bugs, reversions, and test gaps. Deduplicate findings into a prioritized
maintenance backlog; avoid blanket refactors. Test the reviewer on known good and
bad patches and monitor misses/false alarms before increasing release autonomy.

## Authentication and execution boundary

Use a dedicated, owner-only Codex credential store on the Mac mini, separate from
interactive Codex. Initial sign-in is interactive once; subsequent runs are
unattended until the provider requires reauthentication. Let the pinned Codex
client refresh managed ChatGPT credentials; do not implement an OAuth token
exchange or repeatedly overwrite rotated credentials from a seed copy.

The trusted controller obtains a fresh access token through Codex's auth lifecycle.
Only a temporary proxy receives that access token through a private input pipe.
The proxy has no repository mount. Its upstream origin, route, model, request
bounds, and lifetime are fixed by trusted code. It injects provider authentication
and validates model metadata. The reviewer gets only an ephemeral proxy capability,
never the account refresh token, interactive auth directory, Linear/GitHub keys,
Docker socket, SSH agent, or production credentials.

The reviewer runs non-root with a read-only root and read-only evidence/source
mounts, dropped capabilities, no-new-privileges, process/memory/time bounds, and
an internal Docker network. It can reach only its temporary provider proxy.
Scratch data lives in tmpfs. Bounded transcripts stay in private controller storage;
only the validated final review and provenance are eligible for later publication.
Authentication requests and tokens are never logged. Test
execution stays separate and offline. Repository hooks/MCP/plugins are not loaded.

Serialize dedicated credential refresh and reviewer runs. Authentication failure
is reported as auth_required and never triggers a browser prompt in the worker,
fallback model, API billing switch, or an infinite retry. A rejected refresh token
needs a new dedicated sign-in. Maintain explicit logs of cleanup; failure to remove
containers/networks pauses the reviewer. No permanent public reviewer endpoint.

The app-server external-token login is documented as experimental, but this pinned
client's generated schema labels it internal-only. This implementation will use
the stable managed-auth flow and a restricted Responses proxy instead.

## Acceptance checks for this increment

- Dedicated auth persists outside candidate workspaces, mode 0600 in a 0700 directory.
- A second independent run succeeds without interactive login.
- Managed refresh is exercised without exposing token values.
- Reviewer cannot read the refresh-token store, write source, access Docker,
  contact production, or make direct/unrelated outbound connections.
- Proxy rejects other models/routes, missing or incorrect capabilities, oversized
  input, and unexpected provider-model metadata; errors/logs redact credentials.
- A minimal Astra request succeeds inside the actual isolated image.
- A read-only review of the recovered DYL-12 patch returns valid structured output
  with real evidence and explicit limitations. It does not promote the original
  blocked job or modify Deck Lab.
- Timeouts, auth failures, and malformed responses fail closed and clean up.

## Sources

- [Codex non-interactive operation and auth](https://learn.chatgpt.com/docs/non-interactive-mode)
- [Maintaining managed Codex auth](https://learn.chatgpt.com/docs/auth/ci-cd-auth)
- [Codex app-server account and model interfaces](https://learn.chatgpt.com/docs/app-server)
- [Astra model](https://developers.openai.com/api/docs/models/gpt-6-astra)

Implementation and live verification results will be recorded separately, without
claiming the full automated review pipeline exists before it is implemented.
