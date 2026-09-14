# Automatic feedback and Linear pipeline states

Effective September 8, 2026. Feedback from Deck Lab's registered Linear team/project
with the `user-feedback` label queues automatically, including bugs, suggestions
and UX feedback. It does not require a ready-state transition or human intake
approval. Other projects/labels and completed/canceled/duplicate issues are excluded.
Existing eligible open feedback is backfilled; previously blocked attempts retain
their limits and become visible as Blocked rather than being retried indefinitely.

The former three-initial-runs daily quota is disabled (`max_daily_runs = 0`). Global
coding concurrency stays one, each attempt has the existing 30-minute deadline,
and the independent Astra gate allows at most two repair rounds. Code acceptance
and production deployment can now be authorized by the owner in Linear; see
[the Linear release handoff](linear-release-handoff.md).

| Event | Linear status |
| --- | --- |
| Eligible feedback accepted | Todo |
| Baseline or candidate checks | Verifying |
| Cursor coding | In Progress |
| Independent browser evidence and Astra review | AI Review |
| Bounded Cursor repair | Repairing |
| Current passing candidate with delivered screenshot evidence | Ready for Your Review |
| Verified owner approval to merge and deploy | Ready to Deploy |
| PR merged, not yet verified live | Merged — Awaiting Deployment |
| Production deploy job executing after environment approval | Deploying |
| Successful production workflow and healthy live build containing the merge | Done |
| Failed checks/review, exhausted bounds, failed publication/release | Blocked |
| Issue canceled/duplicated or PR closed without merge | Canceled (manual Duplicate is preserved) |

The local pipeline projection is stored durably in SQLite. Stage writes have a
monotonically increasing version and an acknowledged version. The controller reads
Linear before changing `stateId`; it never rewrites report text. Every Blocked stage
also gets a reason comment with the current attempt's verified Astra summary,
findings, requested next steps and evidence limitations when available. Failures
before review and publication/release failures are identified separately from
Astra's verdict. Existing Blocked stages receive missing comments on reconciliation.
The controller persists each comment body and stable UUID before sending it, checks
Linear for an already-created comment on retries, and confirms the comment before
acknowledging the Blocked status. Comment delivery failures use the same retry queue.
A lost response retries by re-reading and acknowledging an already matching state.
Newer stage versions supersede stale writes. Provider failures retain retry state
and cannot produce a false completion. Status delivery does not pause coding or
release workflows; a provider outage can delay the displayed state.

The synchronizer processes stage changes every ten seconds, observes GitHub every
thirty seconds, and backfills missed feedback every five minutes. Very short stages
may be coalesced. Timestamps and all original events remain in the local audit log.
No unauthenticated GitHub webhook or public administrative endpoint is introduced.

## Activity in Linear

Each open pipeline issue has one **Agent activity** comment, edited in place across
stages, repair rounds, retries, and dispatcher restarts. It shows the current actor
and task, attempt and repair count, next step, task start time, and update time.
The Linear status remains the board-level overview; open the issue to see this card.

The worker reports controller-owned milestones for baseline checks, Cursor coding
or repair, regression reproduction, candidate verification, browser evidence capture,
and Astra review. A supervisor heartbeat is recorded at most every 15 seconds while
the worker polls an executing process. The card distinguishes a responding supervisor
from an overdue heartbeat (90 seconds without a report). A heartbeat is not proof of
useful agent progress; time in the current task makes a long-running phase visible.

The sync loop checks every ten seconds, coalesces edits to at most one per 15 seconds,
and refreshes unchanged running tasks about every two minutes. Short phases may be
coalesced. If Linear or the local synchronizer is unavailable, the last update time
stays visible; the card cannot promise real-time status during that outage.

Activity delivery has its own durable UUID, acknowledgement and retry backoff. A
lost response or restart reuses the same comment; activity errors cannot hold up
the workflow. No raw agent transcripts, tool arguments, private logs, or report text
are copied into the card. This uses the existing Linear API-key integration and
[Linear's comment API](https://linear.app/developers/graphql); it requires no new OAuth
agent installation. Editing one card avoids new per-step comments; notification
behavior still depends on Linear and the viewer's notification settings.

The final Astra readout, screenshot review handoff, actionable blocker reason, and
verified deployment receipt remain separate comments. Existing cards settle when
work stops or needs owner review; historical Done/Canceled issues do not receive new
activity cards. Existing open Blocked issues receive a truthful stopped-work summary.
Archived issues are skipped; Linear does not accept new comments on archived issues.

DYL-14's activity test also exposed a browser-routing gap: home-page alignment fixes
were being reviewed against deck-builder screenshots. Changes to the home template
or `.dl-step` styles now select the home-page scenario. It captures all three "How it
goes" boxes in Chromium and WebKit at 1920×945 and 390×844, plus doubled heading text
to exercise wrapping. Section close-ups, desktop page context, first-line baseline
measurements and overflow checks accompany the candidate. The text-size stress test
is labeled separately from real browser zoom, and measurement probes are removed
before screenshots.

Publication failures now retain the trusted publisher's diagnostic and retry up to
three invocations per verified artifact. After a partial write, the controller
waits for the publisher lease and reconciles the existing branch/PR. A stranded
publication no longer holds the coding concurrency slot. Exhausted publication
retries still become Blocked with a reason comment.

An explicit coding retry carries forward the previous verified review only when
it belongs to the same report revision. Actionable `needs_human_review` findings
return to the builder within the existing two-repair limit; each repair reruns
independent checks, fresh browser evidence and Astra. Uncertainty without an
actionable repair remains a decision for the owner. Neither path bypasses the
passing-review requirement for publication.

For completion, match the fixed production health build SHA to a successful main
`Deploy production` run whose `deploy` job succeeded. Then verify the PR's actual
merge commit is that release or its ancestor. A green CI/preparation job, merge,
stale live version, unrelated release, failed deployment or missing health evidence
cannot mark a fix Done. Previously established live-release proofs are cached so
old releases remain verifiable after they leave the recent-runs window. A rollback
that no longer contains the merge removes the automatic Done state on reconciliation.

Own status-update webhooks do not start another coding attempt. Content edits to
an inactive unpublished report start a new revision automatically; edits during a
run cancel the old revision and queue the new one only after cleanup. A manual
cancel/pause prevents that requeue. Content changes after publication require a new
feedback issue rather than modifying an already reviewed PR's scope. Canceling or
duplicating a published issue suspends tracking; reopening the unchanged issue
resumes it. Removing project/label eligibility stops queued/running work.

## Credentials and operations

The dedicated `linear-status-api-key` has Read/Write access to the feedback team.
It is loaded only by the trusted status synchronizer. Cursor and Astra receive
neither it nor the read-only Linear/GitHub publisher credentials. The synchronizer
uses the existing publisher key for read-only GitHub requests and never merges or
deploys. The separate Linear release controller uses the owner GitHub session only
after a verified screenshot-bound owner approval. Initial state setup adds missing team states without renaming existing ones.

```bash
.venv/bin/python integrations/deck-lab/save_credential.py linear-status
.venv/bin/python integrations/deck-lab/sync.py --setup
# Register the returned state IDs in project.toml before activation.
.venv/bin/python integrations/deck-lab/sync.py --backfill
.venv/bin/python integrations/deck-lab/services.py
```

The login service is `com.sdlc-dispatcher.deck-lab-linear-sync`. Logs are private
`.dispatcher/linear-sync.*.log` files. Inspect `pipeline.last_error` and `next_retry`
in the queue database for per-issue delivery failures. The Mac mini, login session,
Docker and Tailscale must remain available for autonomous coding and previews.

Verification covers automatic eligible intake, terminal/foreign refusal, webhook
echoes, edits during execution, cancel precedence, unlimited daily intake with
bounded concurrency/attempts, version races, lost-write retry and exact-release
completion. A real workflow state creation and actual issue status writes validate
the credentials; historical production evidence validates the read-only observer.

Activation verification: 112 tests passed with Docker boundary tests enabled.
The team state mapping and real issue updates were verified. DYL-11 began coding
automatically; DYL-10 and DYL-9 queued without approval, while the two earlier
blocked pilot jobs remained Blocked. Both local and public receiver health returned
200, and the release observer verified the deployed build using GitHub/live evidence.

### Explicit retries after a blocked run

Use `sdlc-dispatcher retry --project <project.toml> <job-id> --reason "<reason>"`
after investigating a failure. This queues the existing issue and, when its normal
attempt budget is exhausted, authorizes one extra attempt for the current report
and policy. Attempt numbers, artifacts, run counts, and audit history are preserved.
The grant cannot interrupt active work, duplicate a published PR, bypass checks or
review, or change project-wide limits. Linear returns to Todo through normal status
synchronization. An exhausted retry needs another explicit operator decision.
