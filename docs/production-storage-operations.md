# Deck Lab production storage operations

The production storage remediation complements the Linear release handoff. The application implementation and its runbook are in `DylnMtthws/commander-deck-engine`, PR #38 (`docs/production-storage.md`). The original Fly machine and encrypted volume remain in place; the volume is now 5 GB.

## Dispatcher runtime

`integrations/deck-lab/storage.py` loads the reviewed application helper from `.dispatcher/storage-service/`. That private runtime directory contains `storage_control.py`, `storage_remote.py`, the pinned requirements, and `installed.json` SHA-256 hashes. Update this copy from the reviewed application commit after changes; install requirements into the dispatcher virtual environment. Never copy production data or secret files into a review packet or repository.

The `storage-monitor` login service runs every minute. It checks capacity, attempts bounded checkpoints every five minutes, archives and prunes backups hourly, and independently restores a remote backup weekly. It serializes local runs and defers when another storage operation holds the production lock. Install/reload only this service using:

```sh
.venv/bin/python integrations/deck-lab/services.py --only storage-monitor
```

Configuration is `.dispatcher/storage-config.json`, credentials are owner-only `.dispatcher/secrets/storage.json`, and durable progress is `.dispatcher/storage-monitor.json`. DYL-16 is the single Linear storage-health issue. Updates edit the existing comment; failures have a reason and remain failures until a later successful check. Mac mini uptime and login are required for this scheduler and Linear reporting. GitHub predeployment backups and Fly's configured growth operate independently.

## Release behavior

The release controller calls the same capacity preflight before validation/merge and before dispatch. Busy maintenance defers the check without exhausting repair attempts. Low capacity or unverifiable storage stops the release before it proceeds.

After a deployment failure following a merge, a fresh owner transition to Ready to Deploy can resume the recorded merge. The previous blocked request must match the current approval revision and PR head; its recorded merge must equal the PR merge and current main. A changed main cannot silently inherit old approval. DYL-14 exercised this path: recovery run 34369173075 succeeded, and the issue became Done without a second merge.

## Validation records

Private receipts under `.dispatcher/releases/` record the initial volume/backup/restore verification, verified legacy archival and cleanup, isolated application review, and maintenance deployment. They contain hashes and operational metadata, not database records or bucket credentials. The repeated-cycle, corruption, low-space and restore tests use synthetic data. The production restore drill uses a separate private temporary database and never writes to production.

## 2026-09-09 experiment

The recovery experiment exposed two Fly exec failure modes: connection timeout during long operations and `PayloadTooLarge` for the aggregate cleanup request. The helper now uses compressed command payloads and supervised background jobs with durable receipts. Supervision removes staging directories and job-owned partial local copies after child termination. Local backup publication uses fsync and atomic rename.

Independent Astra review found and drove fixes for exact mount validation, abandoned timeout staging, peak staging capacity, and interrupted local copies. Final application head `9f7b28596b0fb43bc227114871a573c006f7c7a8` passed Astra review without findings and GitHub CI run `34371353280`. PR #38 merged as `b542856c3e74066b581ff5e416f15313c842773a`. The dispatcher suite passed 183 tests (7 skipped); final targeted storage/release checks passed 40 tests. Synthetic repeated-interruption tests cover both temporary staging and the actual local-copy stage.

Maintenance deployment `34372903542` succeeded on 2026-09-09 using main CI `34372190758` (1,378 application tests passed, 31 skipped; mypy checked 134 files). Production health reports build `b542856c3e74066b581ff5e416f15313c842773a`. The live original mount confirms 5 GB, growth at 70%, +1 GB increments and a 10 GB limit. Post-rollout verification found approximately 4.67 GB free, two local backups and zero WAL bytes. The deployment receipt records the verified private predeployment backup and completed Fly snapshot. Both temporary maintenance variables were removed after success.
