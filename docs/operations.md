# Operating and security boundaries

## Trust model

This is a single-owner, single-host pilot. The operator, host Docker daemon,
registered source commits, project configuration, image and dispatcher installation
are trusted. Reports, links, generated code, tests and agent output are untrusted.
Do not expose the Docker socket or a writable dispatcher/state directory to a
worker. Do not run workers on the production application machine.

Use a dedicated development VM for unattended operation. Docker containers share
a kernel and are not a hostile multi-tenant security guarantee. Container time,
memory and PID limits do not impose a hard host-disk quota; configure a separate,
bounded data disk or storage quota on the dedicated worker host.

The model credential is accessible inside its worker. Use a dedicated provider
project/key with a provider-enforced spending cap and revoke it independently.
The dispatcher enforces attempt/launch/time limits, **not a hard dollar budget**.
It does not assume subscription access grants unlimited unattended usage.

## Data and permissions

- Coding containers receive only the approved report, committed source snapshot,
  operator-declared non-secret settings, and model key. No production databases,
  `.env` mounts, local CLI logins, SSH agents, GitHub/Linear credentials or Docker
  socket. The model API necessarily receives report/source context.
- Checks run in fresh offline containers. Dependencies must already exist in the
  trusted image. Reports with private screenshots are not fetched automatically.
  Redact report identifiers/content before intake where required.
- Code and new tests must be text, within allowed paths and patch limits. Symlinks,
  hard links, special files, changes to existing tests and known control-plane files
  are rejected. Tests may still be misleading or malicious; human review remains
  required. Network/credential boundaries protect the host independently.
- Private logs can contain sensitive model output or source; they are not published.
  Basic credential checks on the patch are defense in depth, not a DLP guarantee.
  Inspect patches for private user data before explicitly publishing them.
- The publisher uses GitHub's Git-object API and opens draft PRs. It has no merge,
  issue-closing or release operation. Protect main with required CI and human review,
  and exclude its credential from bypass actors. PR-triggered CI must itself be safe
  for untrusted code and have no production secrets or deployment permissions.

## State and recovery

Jobs follow `needs_review → queued → running → verifying → ready → publishing → published`.
Failures end in `blocked`, `failed` or `cancelled`. `published` means a draft PR
exists, not that a fix shipped. Approval pins report revision and project-policy
fingerprint. The source commit and immutable image are recorded in each artifact.

SQLite uses immediate transactions for claims and quotas, plus a global active-run
lock. Do not put the database on NFS or share it between hosts. Worker crashes fail
closed until `recover` removes expired containers. A paused run stays cancelled
even if the service is resumed before its next poll. Queued jobs remain queued.

Use explicit reapproval for bounded retries. Existing issue IDs remain deduplicated
after terminal states; reopening a published issue requires deliberate human triage.
There is no automatic merge-conflict repair or unlimited iteration loop.

Publication uses a deterministic per-job branch, immutable artifact digest, tree
comparison, a durable lease, and PR lookup to reconcile ambiguous responses.
Do not clear `publishing` manually after a timeout without inspecting the remote
branch/PR. A token with narrower permissions may need configuration before recovery.

Keep receiver, worker and publisher as separate processes with distinct environment
variables. They may share the local state database, but only the trusted dispatcher
processes can read it. The receiver requires HTTPS termination and ingress request
rate limits in a deployed setup. No hosted service or background startup agent has
been installed by the generic package. The Deck Lab local pilot separately installs
a receiver-only login LaunchAgent and uses an explicitly approved HTTPS Funnel;
coding workers and publication remain explicit CLI operations.

State defaults to owner-private permissions. Stop processes before copying the whole
state directory, or use SQLite's backup API for an online backup. Retention/pruning
is manual in v0.1: review local policy, stop processing, preserve reconciliation and
deduplication records, then remove unneeded terminal-job logs/artifacts. Deleting
queue records can permit duplicate processing and discards the audit trail.

## Production promotion

Keep production deployment in a separate, protected workflow. Agent-generated PR
code must not receive production credentials even when its CI runs. Require human
review and independent checks, deploy the reviewed artifact to an isolated staging
app, smoke test it, then promote the same artifact digest with a separate approval.
Database migrations need a backup and compatibility/rollback plan. Never promote
staging data or attach staging to production's volume.

The local preview is development evidence, not a substitute for a hosted staging
deployment with production-like auth, TLS, service contracts and release testing.

## Primary integration references

- [Linear signed webhooks](https://linear.app/developers/webhooks)
- [Codex unattended execution](https://learn.chatgpt.com/docs/non-interactive-mode)
- [GitHub Git trees](https://docs.github.com/en/rest/git/trees)
- [GitHub pull requests](https://docs.github.com/en/rest/pulls/pulls)
- [Docker runtime controls](https://docs.docker.com/engine/containers/run/)
