# Linear review and production release

> Current evidence policy: screenshots and video are optional and disabled by
> default. A delivered text-evidence review card can establish readiness. References
> to screenshot requirements below describe the original rollout; see the
> screenshot policy update at the end for the superseding behavior.

The owner reviews a fix inside Linear. **Ready for Your Review** means a matching
before/after screenshot comment was successfully delivered and the current PR
passed independent Astra review and GitHub review, test and container checks.
Moving that issue to **Ready to Deploy** explicitly authorizes both merge and
production deployment. Selecting this status elsewhere does not grant approval.

| State | Meaning |
| --- | --- |
| Verifying / AI Review | Evidence, code or integration checks are still running |
| Ready for Your Review | Before/after screenshots are available in the issue |
| Ready to Deploy | A verified owner transition queues that reviewed candidate |
| Deploying | Controller is validating, merging, building or releasing |
| Done | Production workflow and healthy live build prove the fix is deployed |
| Blocked | Recovery bounds exhausted; an issue comment explains the actual stage and next step |

Review comments contain the Astra summary, before/after browser captures, relevant
mobile captures and a recording for newly captured drag scenarios. The upload is
stored privately in Linear, independently of the eight-hour Tailscale preview.
Synthetic preview data and evidence limitations are stated in the comment. A
failed upload or comment delivery cannot announce review readiness. Uploads are
cached by content hash; a stable comment UUID reconciles lost mutation responses.

## Approval and execution

Only the configured Linear owner can approve. The existing webhook receiver
verifies the raw HMAC signature and a fresh delivery timestamp before the release
handler sees the event. The handler checks the actor, previous/current states,
issue eligibility, report revision, review availability, event ordering and
activation time. It records the delivery ID, payload digest, owner, PR commit and
screenshot evidence digest transactionally. It never infers approval from polling
a status alone. Missing approval deliveries produce an actionable diagnostic.

The separate release controller processes one request at a time. Its process lock
covers every tick and long review operation; launchd restarts it after a crash.
The SQLite request is durable through branch update, merge, main CI, dispatch,
environment approval and live verification. Repeated deliveries do not duplicate
a release. Merge uses the expected head commit; a lost response is reconciled by
reading the actual PR. Dispatch intent is stored before the request and recovered
from matching workflow runs; an ambiguous dispatch is not blindly repeated.

Main must be the verified live build before a new PR is merged. The controller
updates an outdated branch, runs full offline checks and fresh browser evidence,
and requires another Astra pass on the integrated commit. The approved patch must
remain equivalent (`git patch-id --stable`); changed fixes require fresh screenshots
and another owner approval. The next main build must be the actual approved merge.
Unrelated main changes stop the release instead of being silently included.

Existing GitHub branch protection, review/test/container checks and the production
owner environment gate remain enabled. The controller uses the existing `gh`
owner session to perform these owner-authorized operations. It checks that identity
against `RELEASE_OWNER`. Neither coding agents nor Astra receive release credentials.
A dedicated GitHub App is a future credential migration; this implementation does
not depend on a new App installation or preview deployment-protection features.

The production workflow promotes the exact successful main-CI image. Its existing
compressed online database backup, integrity verification, volume snapshot,
immutable image identity, machine/volume checks and live health verification remain
in force. The controller does not enable infrastructure bootstrap exceptions,
delete backups, deploy arbitrary images or automatically roll back a database.
Backend/dependency/infrastructure rollouts remain separately reviewed operations.

## Recovery and withdrawal

- Changing the issue back before merging withdraws the queued approval. A running
  offline check may finish, but the controller checks approval again before merge
  and deployment. A merge or deployment already initiated cannot be undone by a
  status change; the controller reports that boundary for inspection.
- A code/head change outside the recorded integration update invalidates approval
  and starts fresh review preparation. An actual change to the approved fix also
  requires another human review.
- CI can retry failed jobs up to three workflow attempts. Provider operations use
  bounded retries with a sixty-second delay. A busy Astra lock is a wait, not a
  failed repair attempt. Active release processing has a two-hour bound.
- Production failures stop with the workflow link and instructions to inspect the
  backup/deployment receipt. Deployment is not retried blindly after a potentially
  partial production mutation. Backup retention still needs scheduled maintenance.
- A green preparation job or workflow with stale live health cannot mark Done.
  A failed release comment describes release-stage validation, rather than falsely
  attributing an earlier candidate's passing Astra result to the failed release.

## Operations

```bash
# Validate owner identities and configure states without merge/deploy execution.
.venv/bin/python integrations/deck-lab/configure_release.py --mode observe

# Install the login services after configuration.
.venv/bin/python integrations/deck-lab/services.py

# Enable only after tests and provider checks pass.
.venv/bin/python integrations/deck-lab/configure_release.py --mode live

# Stop future release actions; already-running GitHub jobs are not canceled.
.venv/bin/python integrations/deck-lab/configure_release.py --mode off
```

The service is `com.sdlc-dispatcher.deck-lab-release-controller`. Logs are under
`.dispatcher/release-controller.*.log`; proofs and isolated Git source are under
`.dispatcher/releases/`. SQLite tables `review_cards`, `release_uploads`,
`release_events`, `release_event_order` and `release_requests` preserve evidence,
approvals and progress. Release routing is stored separately from coding policy,
so enabling it does not invalidate previously reviewed candidate artifacts.

The Mac mini must remain awake with the owner login session, Docker, Tailscale and
GitHub authentication available. Observe mode can prepare review evidence and
record real approvals, but cannot merge or deploy. A first live release requires
a real owner status transition; tests never manufacture a production approval.
# Screenshot policy update

Screenshots are optional for both Astra review and the Linear release handoff.
The Deck Lab browser adapter defaults to no screenshot or video capture, while
retaining behavioral assertions and layout measurements. New coding and release
review packets omit media, including media from cached browser evidence. Review
cards can reach Ready for Your Review with text evidence alone. Astra must disclose
the absence of visual inspection; missing screenshots alone do not block release.
Existing verified reviews and delivered cards remain valid.
