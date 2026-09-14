# Submitted Linear fixes

## Batch 1: remove obsolete UI and repair builder actions

| Issues | Scope | Acceptance criteria |
| --- | --- | --- |
| DYL-18 | Remove the legacy Strategy Pack Builds and Ready to Edit panels from Build. | Build shows only the deck library and new-deck flow. |
| DYL-22 | Remove the expensive "plays this card" commander filter. | The commander filter form and server query no longer expose it. |
| DYL-23 | Remove the tournament-evidence disclosure. | The Research header contains no tournament-evidence paragraph. |
| DYL-25 | Remove Add top 40 to a new deck. | Commander profiles retain individual card actions but no bulk top-40 action. |
| DYL-26 | Remove unhelpful Recent lists. | Commander profiles do not show anonymous/meaningless recorded-list rows. |
| DYL-27 | Remove Official card rulings from commander profiles. | Commander profiles do not render that section. |
| DYL-28 | Correct Start a build on mobile. | The primary action fills the available mobile action grid without overflow. |
| DYL-32 | Move commander selection out of card search. | A labeled deck-options menu opens commander selection; the search panel has no commander-selection button. |

## Batch 2: research product model

| Issues | Scope | Acceptance criteria |
| --- | --- | --- |
| DYL-19 | Extend card filters with Oracle text, supertype/type/subtype include-or-exclude controls, colour include/exclude/exact, bounded numeric controls, rarity, and Commander-legal-only results. | Each filter composes server-side and invalid combinations return no unsafe broad result. |
| DYL-20 | Split Commanders and Meta into distinct data products. | Commanders is alphabetical commander discovery; Meta presents cEDH archetype/deck statistics and provenance. |
| DYL-29 | Remove visible Meta-tab loading delay. | Meta data starts loading before selection and selection has a bounded loading state. |

## Batch 3: decks and account surfaces

| Issues | Scope | Acceptance criteria |
| --- | --- | --- |
| DYL-21 | Add private-by-default deck visibility and a public-deck discovery view. | Owners explicitly publish/unpublish; unauthenticated or other users only see published, non-sensitive deck data. |
| DYL-24 | Profile and Build navigation latency. | Navigation is measured on production-like data, slow dependencies are removed or deferred, and a regression budget is added. |
| DYL-30 | Harden password changes. | Require current password and reauthentication, rate-limit sensitive actions, invalidate other sessions, and audit the change. |
| DYL-31 | Fix mobile alignment of user-selection category buttons. | 430px viewport has equal-height, vertically aligned controls with keyboard focus visibility. |

Batch 1 is safe to implement as a single visual cleanup. Batches 2 and 3 change data, authorization, or protected account/admin code and need their own reviewable PRs.
