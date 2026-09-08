# GitHub approval, independent review, and release specification

Requested September 8, 2026. This specification extends the isolated Astra
reviewer into the worker/publication path and defines Deck Lab's human release UI.

## User experience

1. Feedback enters Linear and the Dispatcher queue under its registered policy.
2. Cursor produces a candidate. Independent baseline/reproduction/patched checks
   run without model credentials. Capture the candidate before cleanup.
3. The project adapter collects isolated browser evidence and creates a disposable
   private preview. Astra reviews the exact artifact and evidence in a fresh context.
4. A supported blocking finding triggers a bounded repair round. Missing evidence,
   auth failures, uncertainty, repeated blockers, cancellation, or exhausted bounds
   require human intervention. Advisory debt does not initiate a refactor.
5. Only a passing, current review can produce a draft GitHub PR. The PR shows the
   issue reference, changed files, checks, review outcome/advisories, patch identity
   and a preview link. Raw feedback metadata and private transcripts stay private.
6. The owner tries the preview, marks the PR ready, and merges it after required
   checks. This is the decision to accept the code; no deploy runs on merge.
7. CI builds and tests the merged main SHA once, then retains that exact container
   and its checksum/manifest as a release candidate.
8. In GitHub Actions, the owner selects **Deploy production**, supplies the passing
   CI run ID, and clicks **Run workflow**. The workflow validates provenance and
   presents the release SHA before the protected production environment approval.
   The owner approves that environment to permit production credential access.
9. The release promotes the tested image, preserves the single Fly machine/volume,
   records recovery information and backup evidence, verifies health/build identity,
   and reports success or a failed release requiring recovery.

## Authority boundaries

The Dispatcher has no merge or production deploy implementation. Its publisher
credential is separate from Cursor and Astra credentials. Production credentials
exist only in the GitHub production environment, restricted to main and owner
approval. Self-approval of deployment is allowed because this is a solo-owner app;
the explicit release action and environment approval remain recorded.

Require a PR, successful CI, resolution of review conversations, and current base
checks on main. Do not require an owner to approve their own PR with a second
account: a manual merge is the human code acceptance decision. A later dedicated
publisher App can enable required human PR reviews without the self-review problem.
No automatic merge. Candidate changes cannot edit workflow or policy files.

Repository inspection found a public repository, no branch protection, no
environments, and no deployment webhooks. The existing workflow only tests/builds.
Production is the single-machine SQLite Fly app `dylnmtthws-decklab`.

## Review and repair contract

Review policy paths, context selection, image/auth home, evidence adapter and repair
bounds are operator-owned project registration. Reviews and their checksums are
stored separately from coding artifacts. Publisher validation checks completed
status, exact Astra model, cleanup, result/input checksums, issue revision, base SHA,
artifact digest, current policy and candidate contents. A probe, stale result,
missing review, changed file, or needs-human-review cannot pass publication.

Each approved attempt permits at most two repair rounds, within the original total
job deadline. Every round uses the same captured original base, seeds the last
candidate into a fresh workspace, and supplies only actionable blocking findings.
It reruns reproduction on the original base and all patched checks, gathers fresh
evidence, and invokes a fresh reviewer. New test files can be repaired, while tests
present in the original base remain protected. Repeated equivalent blockers stop
early. Repair rounds and reviews are separately audited; no unbounded recursion.
The current daily initial-attempt setting is not changed by this increment.

## Previews and evidence

Use isolated synthetic data, no production copies, no model/Linear/email/Fly keys,
no outbound app network, and a read-only candidate filesystem. Browser checks are
operator-owned. Record actual viewport/scenario coverage; unrelated reports may
need new fixtures and must be escalated rather than declared reproduced.
Preview receipts bind the URL and expiry to the candidate digest. Expired or stale
previews cannot silently show a different candidate. Access is private through
Tailscale; the public webhook Funnel route remains separate.

## Exact-image release

CI runs with read-only repository permissions and no Fly token. Upload the image
only after the full test and installed-image checks pass. The manifest records
the main SHA, image ID, compressed image checksum, configuration checksum, repository
and CI run identity. Release accepts only a successful push-to-main CI run from
this repository and requires the selected SHA to still be main.

Preparation and offline smoke validation occur before production secret access.
The production job uses trusted release code from main, validates the bundle again,
pushes the already tested image to Fly's registry and deploys its immutable digest.
It does not rebuild at promotion or run candidate scripts with production secrets.
No arbitrary app, image, branch or shell-command input is accepted.

Before replacement, verify exactly one running machine and its existing /data
volume, record the old image, create an online SQLite backup and a completed volume
snapshot. Do not upload production databases into public-repository artifacts.
Initial automated promotion is limited to UI/test/documentation changes relative
to the live release. A one-time bootstrap exception, bound to the configured live
SHA, permits reviewed release infrastructure, exact prior workspace-exclusion blobs,
root planning docs, the OCI revision label and the exact health identity addition; schema/backend/config/dependency changes need the separate
reviewed migration/recovery process. Preserve one writer and use rolling update
with HA disabled. Verify machine/volume identity and the health endpoint's build SHA.
Rollback retains the previous digest and backup/snapshot IDs; data restoration is
never automatic because it could discard newer user writes.

## Verification and rollout

Exercise stale/forged review refusal, repair success/exhaustion/repetition/cancel,
private PR formatting, lost-publication reconciliation, preview isolation/expiry,
wrong CI provenance, checksum mismatch, incorrect app/machine/volume, failed backup,
and release health mismatch. Run existing Dispatcher and Deck Lab checks.

Ship Deck Lab's workflow changes as a reviewable PR. Configure protection and the
production environment without deploying. Workflow buttons become available after
the owner merges their definitions to main. Missing Fly login/token provisioning
is an activation dependency, not a reason to leave implementation untested.

References: [GitHub manual workflows](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow),
[deployment environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments),
[Fly deployment](https://fly.io/docs/launch/deploy/),
[Fly access tokens](https://fly.io/docs/security/tokens/),
[Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve).

## Implemented verification record (September 8)

The live isolated Astra integration reviewed a recovered Cursor candidate with
eight independent before/after browser images and accepted it. The controller
validated its exact artifact-bound publication receipt. Full Dispatcher checks
passed 91 tests including Docker boundaries, repair success/exhaustion/repetition,
review tampering, preview route ownership and remote-base refresh without moving
the developer checkout. A certificate-validated tailnet HTTPS request returned 200
for the private review endpoint. The original blocked pilot jobs remain unchanged.

Deck Lab's draft implementation is [PR #31](https://github.com/DylnMtthws/commander-deck-engine/pull/31).
Its initial full application and installed-container CI passed; the final bootstrap
and failed-backup guards also passed 24 focused offline tests. Production has not
been deployed. The publisher credential authenticates successfully, worker and
preview cleanup login services are installed, and original intake approval remains
enforced. Production token provisioning is separately awaiting owner authorization.
