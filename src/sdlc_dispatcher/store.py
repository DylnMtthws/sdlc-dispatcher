"""Single-host durable queue. All claims and quotas are transactional."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import DispatchError, Project


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, project TEXT NOT NULL, source TEXT NOT NULL,
                    external_id TEXT NOT NULL, report TEXT NOT NULL, revision TEXT NOT NULL,
                    status TEXT NOT NULL, policy TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL,
                    deadline REAL, cancel INTEGER NOT NULL DEFAULT 0,
                    artifact TEXT, artifact_digest TEXT, base_sha TEXT, pr_url TEXT,
                    UNIQUE(project, source, external_id)
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    project TEXT NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL,
                    PRIMARY KEY(project,id)
                );
                CREATE TABLE IF NOT EXISTS runs (job TEXT, project TEXT, started REAL);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, job TEXT, action TEXT NOT NULL, at REAL NOT NULL,
                    detail TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reviews (
                    job TEXT NOT NULL, artifact_digest TEXT NOT NULL,
                    metadata_path TEXT NOT NULL, metadata_digest TEXT NOT NULL,
                    verdict TEXT NOT NULL, created REAL NOT NULL,
                    PRIMARY KEY(job,artifact_digest)
                );
                CREATE TABLE IF NOT EXISTS previews (
                    job TEXT NOT NULL, artifact_digest TEXT NOT NULL,
                    receipt TEXT NOT NULL, PRIMARY KEY(job,artifact_digest)
                );
                CREATE TABLE IF NOT EXISTS pipeline (
                    job TEXT PRIMARY KEY, stage TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
                    synced_version INTEGER NOT NULL DEFAULT 0, synced_state TEXT NOT NULL DEFAULT '',
                    updated REAL NOT NULL, next_retry REAL NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT '', release_sha TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS retry_grants (
                    job TEXT PRIMARY KEY, revision TEXT NOT NULL, policy TEXT NOT NULL,
                    ceiling INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pending_requeues (
                    job TEXT PRIMARY KEY, policy TEXT NOT NULL, revision TEXT NOT NULL
                );
            """)
        path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def audit(db, job, action, detail=""):
        db.execute(
            "INSERT INTO audit(job,action,at,detail) VALUES(?,?,?,?)",
            (job, action, time.time(), detail),
        )
        stage = {
            "approved": "queued",
            "retry_authorized": "queued",
            "automatic_approval": "queued",
            "started": "testing",
            "coding_started": "coding",
            "verification_started": "testing",
            "review_started": "ai_review",
            "repair_started": "repairing",
            "ready": "awaiting_approval",
            "published": "awaiting_approval",
            "failed": "blocked",
            "blocked": "blocked",
            "cancelled": "cancelled",
            "publication_needs_attention": "blocked",
            "publication_preflight_blocked": "blocked",
            "policy_changed_or_limit_reached": "blocked",
        }.get(action)
        if action == "ingested":
            stage = "queued" if detail == "queued" else "blocked"
        if job and stage:
            Store._stage(db, job, stage)

    @staticmethod
    def _stage(db, job, stage):
        db.execute(
            """INSERT INTO pipeline(job,stage,updated) VALUES(?,?,?)
            ON CONFLICT(job) DO UPDATE SET stage=excluded.stage,version=pipeline.version+1,
            updated=excluded.updated,next_retry=0,last_error=''
            WHERE pipeline.stage != excluded.stage""",
            (job, stage, time.time()),
        )

    def set_stage(self, job, stage):
        with self.transaction() as db:
            self._stage(db, job, stage)

    def stage(self, job):
        with self.connect() as db:
            row = db.execute("SELECT * FROM pipeline WHERE job=?", (job,)).fetchone()
        return dict(row) if row else None

    def ingest(
        self,
        project: Project,
        source: str,
        external_id: str,
        report: dict,
        delivery: str | None = None,
        delivery_digest: str = "",
        auto_ready=False,
    ) -> str:
        if source not in {"manual", "linear", "demo"} or not external_id or len(external_id) > 200:
            raise DispatchError("Invalid source or issue identity")
        if (
            not isinstance(report, dict)
            or set(report) != {"title", "description"}
            or any(not isinstance(report[k], str) for k in report)
        ):
            raise DispatchError("Report must contain only title and description strings")
        if (
            not report["title"].strip()
            or len(report["title"]) > 300
            or len(report["description"]) > 20_000
        ):
            raise DispatchError("Report is empty or exceeds size limits")
        content = json.dumps(report, sort_keys=True)
        revision = hashlib.sha256(content.encode()).hexdigest()
        now = time.time()
        with self.transaction() as db:
            if delivery:
                old = db.execute(
                    "SELECT digest FROM deliveries WHERE project=? AND id=?",
                    (project.id, delivery),
                ).fetchone()
                if old:
                    if old[0] != delivery_digest:
                        raise DispatchError("Delivery ID was reused with different contents")
                    row = db.execute(
                        "SELECT id FROM jobs WHERE project=? AND source=? AND external_id=?",
                        (project.id, source, external_id),
                    ).fetchone()
                    return row[0] if row else "ignored"
                db.execute(
                    "INSERT INTO deliveries VALUES(?,?,?)",
                    (project.id, delivery, delivery_digest),
                )
            row = db.execute(
                "SELECT * FROM jobs WHERE project=? AND source=? AND external_id=?",
                (project.id, source, external_id),
            ).fetchone()
            status = "queued" if auto_ready and project.automatic_intake else "needs_review"
            policy = project.fingerprint if status == "queued" else ""
            if row:
                if row["revision"] == revision:
                    if status == "queued" and row["status"] == "published" and row["cancel"]:
                        db.execute("UPDATE jobs SET cancel=0 WHERE id=?", (row["id"],))
                        self.audit(db, row["id"], "tracking_resumed")
                    if status == "queued" and row["status"] == "needs_review":
                        if row["attempts"] >= self._attempt_limit(db, row, project):
                            db.execute("UPDATE jobs SET status='blocked' WHERE id=?", (row["id"],))
                            self.audit(db, row["id"], "blocked", "Attempt limit reached")
                            return row["id"]
                        db.execute(
                            "UPDATE jobs SET status=?,policy=?,updated=? WHERE id=?",
                            (status, policy, now, row["id"]),
                        )
                        self.audit(db, row["id"], "automatic_approval")
                    return row["id"]
                # Reports cannot silently change an approved task or an active PR.
                if row["status"] in {"publishing", "published"}:
                    self.audit(db, row["id"], "report_changed_after_publication")
                    return row["id"]
                active = row["status"] in {"running", "verifying"}
                automatic = status == "queued" and project.linear_intake_mode == "feedback"
                db.execute(
                    """UPDATE jobs SET report=?,revision=?,status=?,policy=?,updated=?,
                              cancel=?,attempts=?,artifact=NULL,artifact_digest=NULL WHERE id=?""",
                    (
                        content,
                        revision,
                        row["status"] if active else status if automatic else "needs_review",
                        policy if automatic else "",
                        now,
                        int(active),
                        row["attempts"] if active or not automatic else 0,
                        row["id"],
                    ),
                )
                self.audit(db, row["id"], "report_changed_approval_revoked")
                if automatic and active:
                    db.execute(
                        "INSERT OR REPLACE INTO pending_requeues VALUES(?,?,?)",
                        (row["id"], policy, revision),
                    )
                elif automatic:
                    self.audit(db, row["id"], "automatic_approval")
                return row["id"]
            job = uuid.uuid4().hex
            db.execute(
                """INSERT INTO jobs(id,project,source,external_id,report,revision,status,
                          policy,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    job,
                    project.id,
                    source,
                    external_id,
                    content,
                    revision,
                    status,
                    policy,
                    now,
                    now,
                ),
            )
            self.audit(db, job, "ingested", status)
            return job

    def get(self, job: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
        if not row:
            raise DispatchError("Unknown job")
        return dict(row)

    def jobs(self) -> list[dict]:
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,project,source,status,attempts,updated,pr_url FROM jobs ORDER BY created DESC"
                )
            ]

    @staticmethod
    def _attempt_limit(db, row, project):
        grant = db.execute(
            "SELECT ceiling FROM retry_grants WHERE job=? AND revision=? AND policy=?",
            (row["id"], row["revision"], project.fingerprint),
        ).fetchone()
        return max(project.max_attempts, grant[0] if grant else 0)

    def retry(self, job: str, project: Project, reason: str):
        """Authorize one more attempt without resetting history or changing project policy."""
        if not reason.strip() or len(reason) > 1000:
            raise DispatchError("A concise retry reason is required")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            if (
                not row
                or row["project"] != project.id
                or row["status"] not in {"blocked", "failed", "cancelled", "needs_review"}
            ):
                raise DispatchError("Only inactive, unpublished jobs can be retried")
            ceiling = max(project.max_attempts, row["attempts"] + 1)
            db.execute(
                "INSERT OR REPLACE INTO retry_grants VALUES(?,?,?,?)",
                (job, row["revision"], project.fingerprint, ceiling),
            )
            db.execute("DELETE FROM pending_requeues WHERE job=?", (job,))
            db.execute(
                """UPDATE jobs SET status='queued',policy=?,cancel=0,deadline=NULL,
                artifact=NULL,artifact_digest=NULL,base_sha=NULL,updated=? WHERE id=?""",
                (project.fingerprint, time.time(), job),
            )
            self.audit(
                db,
                job,
                "retry_authorized",
                json.dumps(
                    {
                        "reason": reason,
                        "prior_attempts": row["attempts"],
                        "attempt_ceiling": ceiling,
                    }
                ),
            )

    def approve(self, job: str, project: Project):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            if (
                not row
                or row["project"] != project.id
                or row["status"] not in {"needs_review", "failed", "blocked", "ready", "cancelled"}
            ):
                raise DispatchError("Job is not eligible for approval")
            if row["attempts"] >= self._attempt_limit(db, row, project):
                raise DispatchError("Attempt limit reached; investigate before creating new work")
            db.execute(
                "UPDATE jobs SET status='queued',policy=?,cancel=0,artifact=NULL,artifact_digest=NULL,updated=? WHERE id=?",
                (project.fingerprint, time.time(), job),
            )
            self.audit(db, job, "approved")

    def paused(self) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()
        return bool(row and row[0] == "1")

    def pause(self, enabled=True):
        with self.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO settings VALUES('paused',?)",
                (str(int(enabled)),),
            )
            if enabled:
                db.execute("UPDATE jobs SET cancel=1 WHERE status IN ('running','verifying')")
                db.execute("DELETE FROM pending_requeues")
            self.audit(db, None, "paused" if enabled else "resumed")

    def withdraw(self, project: str, external_id: str, *, terminal=False):
        with self.transaction() as db:
            row = db.execute(
                "SELECT id,status FROM jobs WHERE project=? AND source='linear' AND external_id=?",
                (project, external_id),
            ).fetchone()
            if row and row["status"] not in {"publishing", "published"}:
                db.execute("DELETE FROM pending_requeues WHERE job=?", (row["id"],))
                active = row["status"] in {"running", "verifying"}
                db.execute(
                    "UPDATE jobs SET cancel=?,status=?,policy='',updated=? WHERE id=?",
                    (
                        int(active),
                        row["status"] if active else "cancelled" if terminal else "needs_review",
                        time.time(),
                        row["id"],
                    ),
                )
                self.audit(db, row["id"], "eligibility_withdrawn")
                self._stage(db, row["id"], "cancelled" if terminal else "blocked")

    def claim(self, project: Project) -> dict | None:
        now = time.time()
        with self.transaction() as db:
            paused = db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()
            if paused and paused[0] == "1":
                return None
            # Global concurrency=1 is deliberate for v0. A stale run fails closed until recovery.
            if db.execute(
                "SELECT 1 FROM jobs WHERE status IN ('running','verifying','publishing')"
            ).fetchone():
                return None
            count = db.execute(
                "SELECT COUNT(*) FROM runs WHERE project=? AND started>=?",
                (project.id, now - 86400),
            ).fetchone()[0]
            if project.max_daily_runs and count >= project.max_daily_runs:
                return None
            row = db.execute(
                "SELECT * FROM jobs WHERE project=? AND status='queued' ORDER BY created LIMIT 1",
                (project.id,),
            ).fetchone()
            if not row:
                return None
            if row["policy"] != project.fingerprint or row["attempts"] >= self._attempt_limit(
                db, row, project
            ):
                db.execute(
                    "UPDATE jobs SET status='needs_review',updated=? WHERE id=?",
                    (now, row["id"]),
                )
                self.audit(db, row["id"], "policy_changed_or_limit_reached")
                return None
            db.execute(
                "UPDATE jobs SET status='running',attempts=attempts+1,deadline=?,updated=? WHERE id=?",
                (now + project.timeout_seconds, now, row["id"]),
            )
            db.execute("INSERT INTO runs VALUES(?,?,?)", (row["id"], project.id, now))
            self.audit(db, row["id"], "started")
            return dict(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def cancelled(self, job: str) -> bool:
        row = self.get(job)
        return self.paused() or bool(row["cancel"]) or row["status"] not in {"running", "verifying"}

    def cancel(self, job: str):
        with self.transaction() as db:
            row = db.execute("SELECT status FROM jobs WHERE id=?", (job,)).fetchone()
            if not row or row[0] in {"publishing", "published"}:
                raise DispatchError("Cannot cancel an unknown or publishing/published job")
            status = row[0] if row[0] in {"running", "verifying"} else "cancelled"
            db.execute("DELETE FROM pending_requeues WHERE job=?", (job,))
            db.execute(
                "UPDATE jobs SET cancel=1,status=?,updated=? WHERE id=?",
                (status, time.time(), job),
            )
            self.audit(db, job, "cancellation_requested")

    def resume_revision(self, job, project):
        """Only the worker calls this after its old candidate resources are removed."""
        with self.transaction() as db:
            pending = db.execute("SELECT * FROM pending_requeues WHERE job=?", (job,)).fetchone()
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            if not pending or not row or row["status"] != "cancelled":
                return
            paused = db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()
            if (
                pending["policy"] != project.fingerprint
                or pending["revision"] != row["revision"]
                or (paused and paused[0] == "1")
            ):
                return
            db.execute(
                "UPDATE jobs SET status='queued',policy=?,attempts=0,cancel=0,updated=? WHERE id=?",
                (project.fingerprint, time.time(), job),
            )
            db.execute("DELETE FROM pending_requeues WHERE job=?", (job,))
            self.audit(db, job, "automatic_approval")

    def finish(self, job: str, status: str, detail="", **fields):
        if status not in {
            "verifying",
            "ready",
            "failed",
            "blocked",
            "cancelled",
        } or set(
            fields
        ) - {"artifact", "artifact_digest", "base_sha"}:
            raise DispatchError("Invalid job transition")
        with self.transaction() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job,)).fetchone()
            if not row or row["status"] not in {"running", "verifying"}:
                raise DispatchError("Job is no longer owned by the worker")
            paused = db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()
            if row["cancel"] or (paused and paused[0] == "1"):
                status, fields = "cancelled", {}
            assignments = ",".join(f"{key}=?" for key in fields)
            db.execute(
                f"UPDATE jobs SET status=?,updated=?{',' + assignments if assignments else ''} WHERE id=?",
                (status, time.time(), *fields.values(), job),
            )
            self.audit(db, job, status, detail)
            if (
                status == "cancelled"
                and db.execute("SELECT 1 FROM pending_requeues WHERE job=?", (job,)).fetchone()
            ):
                # A changed report is waiting for cleanup, not a canceled issue.
                # Never send a Canceled status that would cancel the new revision.
                self._stage(db, job, "queued")

    def events(self, job: str):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT action,at,detail FROM audit WHERE job=? ORDER BY id", (job,)
                )
            ]

    def record_review(self, job, artifact_digest, metadata_path, metadata_digest, verdict):
        with self.transaction() as db:
            row = db.execute("SELECT status,cancel FROM jobs WHERE id=?", (job,)).fetchone()
            if not row or row["status"] != "verifying" or row["cancel"]:
                raise DispatchError("Job no longer accepts review results")
            db.execute(
                "INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?,?)",
                (job, artifact_digest, str(metadata_path), metadata_digest, verdict, time.time()),
            )
            self.audit(db, job, "review_completed", verdict + ":" + artifact_digest)

    def review(self, job, artifact_digest):
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM reviews WHERE job=? AND artifact_digest=?", (job, artifact_digest)
            ).fetchone()
        return dict(row) if row else None

    def record_preview(self, job, artifact_digest, receipt):
        with self.transaction() as db:
            db.execute(
                "INSERT OR REPLACE INTO previews VALUES(?,?,?)",
                (job, artifact_digest, json.dumps(receipt, sort_keys=True)),
            )
            self.audit(db, job, "preview_prepared", artifact_digest)

    def preview(self, job, artifact_digest):
        with self.connect() as db:
            row = db.execute(
                "SELECT receipt FROM previews WHERE job=? AND artifact_digest=?",
                (job, artifact_digest),
            ).fetchone()
        return json.loads(row[0]) if row else None
