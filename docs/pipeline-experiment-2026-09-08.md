# Pipeline recovery experiment

Success means an independently verified candidate reaches a GitHub PR for the
owner's code acceptance and deployment decisions. A terminal Blocked result is
an unsuccessful run requiring diagnosis, not successful pipeline completion.

| Issue | Initial outcome | Pipeline gap | Experiment outcome |
| --- | --- | --- | --- |
| DYL-10 | Astra needs human review | Idle commander screenshots did not exercise dragging or load artwork; retry discarded prior findings | Attempt 2 passed checks and Astra with no findings; PR #36 published |
| DYL-12 | Passed Astra, failed publication | No automatic reconciliation after a partial provider write; diagnostic discarded | Existing verified branch reconciled to PR #33; subsequent approved deployment exposed backup disk exhaustion; repair proposed in PR #34 |
| DYL-13 | Astra needs human review | Evidence never opened Spoiler view; accessibility concern had no repair path | Attempt 4 passed checks and Astra; PR #35 published |

The experiment preserves the independent review gate and owner-controlled merge
and deployment. Missing evidence should lead to evidence collection and actionable
findings should return to the builder, within bounded attempts. Provider failures
should retry/reconcile with retained diagnostics. Repeated identical attempts are
not useful experiments: change the identified cause before rerunning.

## Observed causes and interventions

- Review evidence had been fixed to idle commander layouts. The adapter now chooses
  Spoiler and drag scenarios from changed behavior and runs Chromium and WebKit.
  Synthetic artwork is decoded locally; external requests remain blocked.
- WebKit required a dotted isolated hostname for its session cookie. The fixture
  now uses `app.test`, and browser-process diagnostics are retained privately.
- The old Spoiler candidate rendered eight symbols but removed all of them from
  the accessible paragraph. Attempt 4 retains all eight in both browsers and both
  viewports. Its regression tests execute JavaScript using a local DOM harness.
- Real sidebar drags exposed `effectAllowed=copy` versus `dropEffect=move` in both
  base and old candidate. Revision-scoped controller observations pass that evidence
  into DYL-10's next attempt; no issue description is rewritten.
- Publication failures retry/reconcile at most three times per artifact, preserving
  provider diagnostics and respecting the publisher lease. They no longer occupy
  the coding concurrency slot indefinitely.
- Actionable uncertainty now returns to the builder within the existing repair
  bound. A new independent passing review is still required. Retries carry forward
  verified feedback only for the same report revision.
- DYL-12's approved deployment failed twice because a 232 MB database plus two
  232 MB backups left 208 MB free. PR #34 stages the consistent copy off-volume and
  verifies compressed persistent output. Existing backups are preserved; production
  data has not been modified by this experiment. Its 19 focused tests pass.

Private stage/attempt observations are retained in
`.dispatcher/experiments/blocked-recovery/observations.jsonl`. The dispatcher suite
ran 131 tests successfully (seven optional Docker tests skipped); live browser
probes exercised the actual isolated base/candidate apps. Native drag compositor
pixels are outside page screenshots, and Playwright WebKit is not branded Safari.
Those limitations are explicitly included in review evidence.

## Result

All three sampled issues reached a PR after the interventions: DYL-10 in #36,
DYL-12 in #33, and DYL-13 in #35. Both coding reruns passed their first fresh Astra
review. DYL-10 measurements show the decoded preview moving 30 pixels with each
30-pixel pointer movement in Chromium and WebKit, successful moves, sidebar
quantity increasing from one to two, and no leftover preview after cancellation.

This is a PR-delivery result, not a claim that production deployment succeeded.
DYL-12's release remains blocked on the separately reviewed backup-space repair
in PR #34. That PR passed Astra and GitHub test/container/review checks. Its
controller rollout and all application merges/deployments remain the owner's
GitHub decisions. Existing production backups were not deleted or altered.

Final GitHub review, test and container checks passed on PRs #34, #35 and #36.
A read-only production compression estimate produced 51,695,626 bytes versus
208,211,968 bytes free, confirming room for the compressed backup and reserve.
The estimate discarded its output; it was not a restore backup or a production
mutation. Compressed backups still require the documented retention maintenance.

## Authorized merge and release follow-through

The owner subsequently authorized merging and deploying the PRs. PR #34 was
already merged. PR #35 had acquired a main-merge commit after its original Astra
pass, invalidating the old status. Its application and review-context files were
verified byte-identical, and a fresh Astra review passed on
`78ec69fef068f8c20eb74fa33bd7db92224391ba`; all required checks passed before merge.

PR #36 was updated after #35 merged. Fresh combined Chromium/WebKit evidence
exercised both Spoiler rendering and dragging on
`12b52bae2b57f741f71c1066f8daba5369861b72`. Twenty-one focused regressions passed,
and a fresh independent Astra review passed with no blocking findings. No old
review status was copied onto a changed commit. This also exposed a remaining
pipeline gap: updating a published branch currently needs explicit review
refresh orchestration; a prior published state alone does not establish that the
current GitHub head remains ready to merge.


The authorized release completed successfully in [production run 34297634861](https://github.com/DylnMtthws/commander-deck-engine/actions/runs/34297634861),
using the image from passing main CI run 34297205770. Live `/healthz` reports
`status=ok` and build `c0006d29665531b25e21b56c716676533b6f97a7`. The deployed
registry digest matches the release receipt, and the existing production machine
and volume were preserved. The verified online backup compressed 232,370,176
bytes to 51,696,495 bytes; volume snapshot `vs_mkV840MqQ50KC9pwyJ6exOBK` completed
before promotion. The temporary exact-live-SHA rollout setting was restored to
its previous value after success. The production observer automatically moved
DYL-10, DYL-12 and DYL-13 to Done, confirmed by direct Linear reads.

This closes the three sampled issues through production, including recovery from
the prior publication, review-evidence, and backup-capacity failures. The retained
private deployment receipt and tracking record live under
`.dispatcher/experiments/blocked-recovery/`.
