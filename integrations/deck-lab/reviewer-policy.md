# Deck Lab review policy, version 1

Review the reported behavior and nearby impact against the captured base and the
issue's acceptance criteria. Preserve unaffected behavior. For commander-layout
changes, check both single and partner commanders. Existing CSS, shared components, spacing, typography,
interaction patterns, responsive breakpoints and accessibility behavior provide
the initial reference. Reuse existing mechanisms where practical. Do not introduce
a new design language or require unrelated refactors as part of a local bug fix.

Check relevant browser evidence as well as source: one and two commanders, card containment,
nearby zone geometry, and any responsive or zoom assumptions exposed by the change.
Distinguish new regressions from pre-existing overlap. The supplied screenshots
are evidence, not a comprehensive approved visual baseline. Missing card artwork
and untested viewports limit the conclusions that can be drawn.

Assess whether new tests reproduce observable behavior and would catch recurrence.
Source-string checks or reimplementations of a formula alone do not establish
browser correctness. Independent browser measurements can establish the measured
scenario but do not turn a weak committed test into a robust browser regression.

Do not change existing tests, auth, admin, dependencies, deployment, or this policy.
No repository code execution in the reviewer. Independent offline check results
are evidence supplied by the controller. Never claim to have run them yourself.
Only demonstrated defects or unmet acceptance criteria block this patch. Maintain
actionable debt suggestions separately. Any unresolved material uncertainty is
an explicit needs-human-review result. No review verdict grants release authority.
