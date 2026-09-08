# Isolated Astra reviewer

Both manual review and the worker review gate are implemented. It uses Codex CLI 0.153.1,
`gpt-6-astra`, high reasoning, and standard speed. The dedicated managed ChatGPT
login supplies authentication; Cursor's subscription does not supply this access.
There is no API-key billing fallback or model fallback.

See the [reviewer specification](astra-reviewer-spec.md) and the
[implemented pipeline and release contract](github-review-release-spec.md).
Verified worker candidates enter review automatically; blocking findings allow
up to two repair rounds. The publisher independently verifies the passing receipt.
A standalone manual review still does not change a job state or authorize deployment.
Periodic architecture audits remain future work.

## Authentication

Dedicated host store:
`.dispatcher/secrets/reviewer-codex` (0700), containing `auth.json` (0600).
The existing interactive Codex login is separate. Never copy a stale seed over
rotated credentials or mount this directory in the reviewer.

For initial setup or a provider-required reauthentication, run the following on
the Mac mini, then authorize the device login in a browser:

```sh
cd /Users/dylan/Projects/sdlc-dispatcher
env CODEX_HOME="$PWD/.dispatcher/secrets/reviewer-codex" codex login --device-auth
```

The store already exists and is authenticated on this host. A fresh installation
must first create an owner-only directory and configure
`cli_auth_credentials_store = "file"` in its `config.toml`.

`reviewer_auth.access_lease` starts an auth-only Codex app-server process, checks
the pinned client version, refreshes through `account/read`, and verifies the
account's model list. No model task runs on the host. Refresh and full review
execution have separate locks; concurrent reviews return busy for future queuing.
The worker never opens a browser or falls back to the interactive login.

Only the current access token and account routing ID reach a temporary proxy,
through stdin. The refresh token remains in the host store. The reviewer receives
only a random capability for that proxy. Auth expiry/revocation requires a new
dedicated sign-in; unattended does not mean credentials can never expire.

## Preparing a packet

Use `sdlc_dispatcher.review_packet.prepare_packet` with the registered project,
artifact path and independently recorded digest, issue acceptance criteria,
operator-owned policy, selected evidence, and a new destination directory.
It checks artifact/base/policy consistency and revalidates paths and patch size.
Only changed files are included by default. Add relevant existing files through
explicit `context_paths`; unrelated documents and source stay outside the packet.

Packet layout:

```text
packet.json        issue, artifact/base/policy identity, evidence and coverage limits
policy.md          trusted review policy
review.diff        regenerated from the validated candidate
source/            changed source plus explicitly selected context
evidence/          independent check logs, browser measurements and images
```

The runner supplies its own prompt and output schema, copies this packet into a
new private output directory, and hashes every input. Source/evidence are mounted
read-only. Deleted files are represented in the diff. Policy and evidence should
be selected by trusted controller code, never by the coding agent.

## Running a review

From the dispatcher directory, with a fresh output name:

```sh
.venv/bin/python -m sdlc_dispatcher.reviewer \
  --packet .dispatcher/reviewer-packets/DYL-12-scoped \
  --output .dispatcher/reviews/DYL-12-next \
  --auth-home .dispatcher/secrets/reviewer-codex \
  --image deck-lab-agent:local
```

The image is resolved to an immutable digest and its CLI version is checked before
candidate data is read. The process runs non-root, with dropped capabilities,
read-only root/source, 1 GB RAM, two CPUs, 128 processes, and 256 MB scratch tmpfs.
It has no general outbound connectivity, production credentials or Docker socket.
The proxy uses 128 MB, permits only the managed Responses route and Astra High,
rejects provider-hosted browsing/connectors and alternate service tiers, and limits
requests to 40 over 15 minutes. Timeout is at most 900 seconds; input snapshot is
at most 30 MB, each provider request 8 MB, and the captured transcript 5 MB.
These are per-review bounds, not a daily ticket quota or a guaranteed dollar cap.

The CLI's inner sandbox is disabled because Docker enforces the boundary; this
command must never be adapted to execute the review on the host. No source code
is run by the controller. Tests belong in the separate offline verification stage.

Results are private under the output directory:

- `review.json`: validated findings and recommendation.
- `metadata.json`: completion, model confirmation, input/result hashes, usage,
  immutable image, timing, and cleanup status.
- `proxy.jsonl`: credential-free protocol/model events.
- `agent.jsonl`: bounded private transcript, not suitable for automatic publication.
- `input/`: exact source/evidence/policy/schema/prompt reviewed.

Only `metadata.status == completed` plus `cleanup == confirmed` is a completed
review. A verdict is still a recommendation. Missing or contradictory results,
invalid source citations, unsupported model metadata, timeout, or transport/auth
failure fail closed. Cleanup failure requires pausing review execution and repair.
Do not use a leftover `review.json` independently of its metadata and hashes.

The proxy follows the pinned Codex client's managed ChatGPT transport rather than
the public API-key endpoint. Revalidate it when upgrading Codex or if that provider
transport changes. The observed transport omits Content-Type; stream parsing and
the required `response.completed` event still verify the exact provider model.

## Verification

Run the provider-free regression suite and real Docker checks:

```sh
DISPATCHER_DOCKER_TESTS=1 .venv/bin/python -m unittest discover -s tests
```

`--containment-only` on the review command runs the real filesystem, network and
proxy rejection checks with dummy credentials and makes no model request.
For a live access probe use the prepared `access-probe` packet and a fresh output
directory. This consumes account usage but cannot approve application changes.

September 8 verification: dedicated login and managed refresh succeeded; a minimal
probe and a separate DYL-12 review completed without interactive reauthentication.
Both confirmed `gpt-6-astra` in provider response metadata and cleaned up containers
and networks. DYL-12 passed with one advisory finding about its formula/source test;
browser regression coverage, repeated sizing constants and pre-existing overlap
remain follow-up items. The original coding job remains blocked.

The full-repository packet was stopped before review: it exceeded the first size
bound, and automatic approval review rejected sending unrelated private documents.
The replacement packet contains five relevant source files and issue-specific
evidence (492,655 bytes). Packet preparation now defaults to that narrower scope.
Three initial transport probes failed closed before the successful compatibility
fix; their private logs remain available for audit.
