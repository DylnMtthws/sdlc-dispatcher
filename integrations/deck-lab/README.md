# Deck Lab integration

This directory adapts the standalone dispatcher to `DylnMtthws/commander-deck-engine`.
The core stays standalone. Deck Lab supplies project policy, browser fixtures and
a separate GitHub workflow for human-approved production release. Run the following commands from the dispatcher repository root.

## Agent development environment

```bash
PYTHONPATH=src .venv/bin/python integrations/deck-lab/build.py agent
PYTHONPATH=src .venv/bin/python integrations/deck-lab/build_cursor.py
PYTHONPATH=src .venv/bin/python -m sdlc_dispatcher.cli \
  verify-project --project integrations/deck-lab/project.toml
```

The build helper exports the committed source first, excluding local credentials,
databases and worktrees. The image includes Python 3.11, Codex 0.153.1, the app's
development, Postgres and legacy extras to match CI, with CPU-only PyTorch.
`build_cursor.py` layers checksum-pinned Cursor CLI `2026.09.02-c22c1a3` over that
base image as `deck-lab-cursor:local`. The initial pinned package supports Linux
arm64; other architectures require a reviewed package checksum. No host Cursor
configuration or login keychain is mounted. The registration selects
`engine = "cursor"` and `model = "grok-4.6[effort=high,fast=false]"`.
It downloads no embedding model weights and does not query production.
All configured checks must pass offline before an agent is launched. Source is
mounted into `/workspace`, separately from the dependency installation.
This project allows 4 GB per worker. A 4 GB Colima VM passed the full type check
alongside the existing development containers on this 8 GB Mac. The measured
container peak was 1.89 GiB; larger allocations are not required by the current
evidence. The capacity check fails before a job is claimed if the VM is too small.
See the [verification record](../../docs/verification.md) for measurements and results.

The initial change allowlist covers UI templates/assets and new tests. Existing
tests, authentication/admin surfaces, infrastructure and dependencies cannot be
changed by the automated path. Expand the allowlist deliberately as pilot evidence
supports it. A new regression test must fail on base code and pass after the fix.

Linear team/project IDs match the saved production deployment record from
2026-09-07. The live registration now selects `automatic_intake = true` and
`linear_intake_mode = "feedback"`; the `user-feedback` label queues eligible reports
automatically. No ready-state transition or reporter approval is required. No new production widget endpoint is needed.

## Mac mini webhook receiver

Cursor worker credential preparation: create a user API key at
`https://cursor.com/dashboard/api`, then
run `.venv/bin/python integrations/deck-lab/save_credential.py cursor` in an
interactive SSH shell from the dispatcher root. Input is hidden and stored in
`.dispatcher/secrets/cursor-api-key` with owner-only permissions. This only stores
the key; it does not validate it, change engines, or launch a job. The same helper
accepts `linear-read` for the separate Linear read-only key. Credentials are never
added to the receiver environment or project TOML.

Verify the selected model through the actual isolated worker without a coding run:

```bash
.venv/bin/python integrations/deck-lab/worker.py check-agent
```

After explicit approval of a report, approve its current policy and run one job:

```bash
.venv/bin/sdlc-dispatcher approve --project integrations/deck-lab/project.toml JOB_ID
.venv/bin/python integrations/deck-lab/worker.py work
```

The worker helper loads the Cursor and Linear-read keys privately. Only the Cursor
key enters the coding container; Linear validation runs in the trusted controller.
The current model is `grok-4.6[effort=high,fast=false]`: Cursor Grok 4.6,
High reasoning, standard speed. Preflight uses Cursor ACP to select the
canonical model and every parameter without submitting a prompt, verifies the
persisted selection, and obtains its derived runtime display name. The coding
process must report that exact name and successful completion. A mismatch blocks
the artifact. These setup/preflight commands do not approve or launch jobs.

The initial mana-icon attempt stopped at startup on a model-name mismatch. A
subsequent user-authorized trial passed that guard but exposed missing API5
agent hosts in the proxy. The proxy now permits Cursor's documented API and
agent hosts, with unrelated destinations and direct Internet access blocked.
The mana issue remains blocked with two attempts and no artifact. See
`docs/verification.md` for the multi-issue pilot and recovered partner-zone patch.

The local pilot receiver is installed as the login LaunchAgent
`com.sdlc-dispatcher.deck-lab-receiver`. It binds to `127.0.0.1:8787`, starts at
login, and restarts after process failure. It does not start coding workers.
Logs are private files under `.dispatcher/receiver.*.log`.

After connecting to the Mac mini over SSH, install Linear's signing secret with:

```bash
cd /Users/dylan/Projects/sdlc-dispatcher
.venv/bin/python integrations/deck-lab/receiver.py set-secret
```

The prompt hides input, writes an owner-only secret file under the Git-ignored
`.dispatcher/secrets/` directory, then restarts the receiver. The secret is never
included in shell arguments or the LaunchAgent plist. Use `ssh -t` if invoking
the script directly instead of opening an interactive SSH shell.

The active HTTPS webhook URL is
`https://macmini.tail8c92e6.ts.net/webhooks/linear/deck-lab`.
The user approved this route and enabled Funnel in Tailscale. Public ingress was
verified using public DNS and certificate-validated HTTPS: health 200, unsigned
webhook 400, signed ignored health event 200, and admin/private-file routes 404.
The signing secret is installed and the first live widget report arrived in
`needs_review`. The supplied read-only Linear key validates that issue successfully.
The existing private Tailscale service on port 8080 is unchanged.

The authorized tunnel command is:

```bash
tailscale funnel --bg --https=443 http://127.0.0.1:8787
```

For future hosts, Tailscale may require owner enablement of Funnel/HTTPS. Subscribe
to **Issues** for the feedback team. The receiver exposes only `/healthz` and the
signed webhook POST route, limits requests globally to 120 per minute, bounds
request bodies to 256 KB and connections to 32. Unconfigured secrets yield 503;
invalid signatures yield 400. Eligible feedback now queues automatically.

To stop just this public tunnel after activation:

```bash
tailscale funnel --https=443 off
```

The receiver requires the Mac mini to remain awake and the user's login session
to be available. It is a local pilot, not a separately hosted staging service.

## Disposable local review preview

Build either the reviewed main branch or a verified candidate artifact:

```bash
PYTHONPATH=src .venv/bin/python integrations/deck-lab/build.py preview
# For a candidate, add: --job JOB_ID --state .dispatcher
```

Start a local preview with a separate internal network, read-only image and a
temporary in-memory database. Change the Compose port if 5187 is already in use.

```bash
docker compose -f integrations/deck-lab/compose.yaml up -d
.venv/bin/python integrations/deck-lab/smoke_preview.py
```

Open `http://127.0.0.1:5187`. The synthetic account is
`preview@example.test` / `local-preview-only`. These credentials are public test
fixtures, suitable only for this loopback preview. It seeds three synthetic cards,
including test Partner commanders, for basic editor/research interactions.
Only the unprivileged Nginx gateway is exposed on loopback. The app itself stays
on the internal network; this also works with Colima's host-port forwarding.

This preview uses password auth, the redesigned UI, no production services, no
Linear delivery and no research refresh. Its database lives only in `/tmp` inside
the disposable container. Deck Lab's `SABER_DECK_LAB_DEV` flag is off because that
flag forbids the container's internal `0.0.0.0` binding; isolation is enforced by
the container/network/storage settings instead. Never publish this fixture image
as production or internet-accessible staging.

```bash
docker compose -f integrations/deck-lab/compose.yaml down
```

The test corpus is intentionally small. Bugs dependent on real tournament data,
simulator behavior or account history need dedicated sanitized fixtures. The
dispatcher should block those reports until they can be reproduced.

## Hosted staging and production

Hosted staging is not provisioned by this integration. Its required shape is a
separate Fly app/machine, separate SQLite volume and asset storage, separate session
secret, test accounts, and mocked or test-only email/Linear/simulator/data services.
Use protected access for staging and production-like TLS/auth when validating a
release. Do not use the public fixture password from the local preview.

The dispatcher cannot deploy either app. Deck Lab's `Deploy production` workflow
promotes the exact tested main CI image after explicit owner approval and backups.
A continuously hosted staging app is not provisioned; candidate previews are private
and synthetic. Deck Lab's current single-machine/SQLite production
constraint remains in force. Never attach two machines to its existing database
volume or copy staging's synthetic database over production.

Before the first coding run: approve the report and configure a provider spending
limit. Before publication, configure a publisher credential and verify repository
protections/external deployment hooks. Pilot a single report through draft PR
review. No live agent/model request,
Linear webhook installation or GitHub write is part of the offline demo.

## Integrated review, repair and publication

Completed Cursor candidates pass the original-base regression test and full offline
checks, then the trusted adapter records eight desktop/mobile before/after images.
Astra receives only a read-only packet and short-lived model access. Passing review
and a live candidate preview are required before the separate publisher creates a
draft PR. Two repair rounds maximum share the original job deadline; repeated
blockers, missing evidence, auth failures and uncertainty stop for human attention.

Save a fine-grained GitHub token restricted to `commander-deck-engine`, with Contents,
Pull requests and Commit statuses read/write:

```bash
.venv/bin/python integrations/deck-lab/save_credential.py github-publisher
.venv/bin/python integrations/deck-lab/services.py
```

The installer starts a login worker and a five-minute expired-preview collector.
It skips the worker until the publisher key exists. Mac mini, Docker, Tailscale and
the login session must remain available. Current `automatic_intake = true` uses feedback mode, and `max_daily_runs = 0`
disables the daily count gate. Coding concurrency and repair/time bounds remain.
The Linear synchronizer backfills eligible open reports and updates their statuses.

Previews expire after eight hours; the app stops on its own timeout and the collector
removes its gateway, private Serve route and containers within five minutes. Local
evidence remains for audit. Each PR links its exact preview and full private review.
Connect through Tailscale and sign in with the synthetic account
`preview@example.test` / `local-preview-only`. Never use production credentials.
The public repository receives only a sanitized issue reference, file/check facts,
review outcome and private preview links.

If publication fails, inspect `.dispatcher/queue.db` audit events. Retry a ready job
with `integrations/deck-lab/publish.py JOB_ID`. A `publishing` job needs the same helper
with `--reconcile` after its publisher lease expires; do not blindly create another PR.

See [the specification](../../docs/github-review-release-spec.md) and Deck Lab's
`docs/automated-review-release.md` for human merge, production release and recovery.

Before each claimed coding job the controller fetches the registered GitHub base
into a private Git ref; it does not move the developer checkout. Public repository
fetches are anonymous. Private repositories require a separate read-only source
adapter. A fetch failure stops the job before model spend.

See [automatic feedback and Linear states](../../docs/automatic-feedback-linear-statuses.md)
for status mapping, write-key setup, retries and the verified-production completion gate.
