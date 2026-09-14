"""One editable Linear activity card; trusted milestones, never agent transcripts."""

import json
import time
import uuid
from datetime import datetime, timezone

from .config import DispatchError
from .linear import eligible

# Fixed controller-owned descriptions prevent tool output or model text leaking
# into the progress surface. Heartbeats prove supervision, not useful progress.
PHASES = {
    "baseline": ("Verifier", "Checking the starting code", "Cursor implements the fix"),
    "coding": ("Cursor", "Implementing the fix and regression coverage", "Independent checks"),
    "repairing": (
        "Cursor",
        "Addressing the review findings",
        "Fresh checks and another Astra review",
    ),
    "regression": (
        "Verifier",
        "Confirming the new test reproduces the original bug",
        "Check the corrected code",
    ),
    "checks": ("Verifier", "Running independent checks on the fix", "Capture browser evidence"),
    "browser": (
        "Browser verifier",
        "Capturing before/after UI evidence",
        "Astra reviews the code and screenshots",
    ),
    "astra": (
        "Astra",
        "Reviewing the code, tests, and UI evidence",
        "Review verdict or a bounded repair round",
    ),
}
STAGES = {
    "queued": ("Dispatcher", "Queued for the worker", "Baseline checks"),
    "testing": (
        "Verifier",
        "Verifying the candidate and preparing review evidence",
        "Independent review and screenshot handoff",
    ),
    "coding": PHASES["coding"],
    "repairing": PHASES["repairing"],
    "ai_review": (
        "Code review pipeline",
        "Preparing evidence or reviewing the candidate",
        "Astra verdict",
    ),
    "awaiting_approval": (
        "You",
        "Ready for Your Review",
        "Inspect the final review and screenshots, then select Ready to Deploy",
    ),
    "release_approved": (
        "Release agent",
        "Approval received; release queued",
        "Recheck the approved PR and merge",
    ),
    "ready_for_release": (
        "Release agent",
        "Merged; awaiting verified deployment",
        "Deploy and verify the live build",
    ),
    "deploying": (
        "Release agent",
        "Managing merge and deployment",
        "Verify the production workflow and live build",
    ),
    "done": ("Dispatcher", "Deployed and verified", "No action needed"),
    "blocked": (
        "Dispatcher",
        "Blocked — pipeline needs attention",
        "See the blocker comment for the reason and required next step",
    ),
    "cancelled": ("Dispatcher", "Stopped", "No further work is running for this issue"),
}
QUIET = {"awaiting_approval", "done", "blocked", "cancelled", "ready_for_release"}
# Increment when the card format changes so quiet existing cards can be refreshed.
FORMAT_VERSION = 1


def mark(store, job, phase):
    """Called by the supervising worker at a meaningful task boundary."""
    if phase not in PHASES:
        raise ValueError("Unknown activity phase")
    now = time.time()
    with store.transaction() as db:
        db.execute(
            "INSERT OR REPLACE INTO agent_activity VALUES(?,?,?,?,?,?)",
            (job["id"], job["revision"], job["attempts"], phase, now, now),
        )


def monitor(store, job):
    """Keep cancellation intact and persist a supervisor heartbeat at most every 15s."""
    last = float("-inf")

    def cancelled():
        nonlocal last
        now = time.time()
        if now - last >= 15:
            with store.transaction() as db:
                db.execute(
                    "UPDATE agent_activity SET heartbeat=? WHERE job=? AND revision=? AND attempt=?",
                    (now, job["id"], job["revision"], job["attempts"]),
                )
            last = now
        return store.cancelled(job["id"])

    return cancelled


def stamp(at):
    return datetime.fromtimestamp(at, timezone.utc).strftime("%b %d, %H:%M UTC")


def snapshot(store, job, stage, now):
    kind = stage["stage"]
    actor, task, following = STAGES.get(
        kind, ("Dispatcher", "Reconciling pipeline state", "Confirm the next stage")
    )
    started = stage["updated"]
    heartbeat, phase = None, kind
    with store.connect() as db:
        runtime = db.execute("SELECT * FROM agent_activity WHERE job=?", (job["id"],)).fetchone()
        start = db.execute(
            "SELECT id FROM audit WHERE job=? AND action='started' ORDER BY id DESC LIMIT 1",
            (job["id"],),
        ).fetchone()
        repairs = db.execute(
            "SELECT COUNT(*) FROM audit WHERE job=? AND action='repair_started' AND detail!='' AND id>?",
            (job["id"], start[0] if start else 0),
        ).fetchone()[0]
    active = job["status"] in {"running", "verifying"} and kind in {
        "coding",
        "repairing",
        "testing",
        "ai_review",
    }
    if (
        active
        and runtime
        and runtime["revision"] == job["revision"]
        and runtime["attempt"] == job["attempts"]
    ):
        phase = runtime["phase"]
        actor, task, following = PHASES[phase]
        started, heartbeat = runtime["started"], runtime["heartbeat"]
    if job["status"] in {"ready", "publishing"}:
        actor, task, following = (
            "Dispatcher",
            "Preparing the PR and screenshot review handoff",
            "Ready for Your Review",
        )
    health = ""
    if active:
        if heartbeat is None:
            health = "Worker heartbeat has not been reported for this attempt."
        elif now - heartbeat > 90:
            health = "Worker heartbeat is overdue; current activity is unconfirmed."
        else:
            health = "Worker supervision is responding; this is not a completion estimate."
    paused = store.paused()
    if paused and kind not in QUIET:
        task, following = "Pipeline paused", "Resume the dispatcher to continue"
    body = (
        "**Agent activity**\n\n"
        f"**Now:** {actor} — {task}.\n"
        f"**Run:** Attempt {job['attempts']} · review repair rounds: {repairs}.\n"
        f"**Next:** {following}.\n\n"
        f"Stage started: {stamp(started)}."
    )
    if health:
        body += f" Time in this task: {max(0, int((now - started) // 60))} min.\n{health}"
        if heartbeat:
            body += f" Last worker heartbeat: {stamp(heartbeat)}."
    body += f"\n\nUpdated {stamp(now)}. This comment updates in place; the final review and screenshots are posted separately."
    signature = json.dumps(
        [
            FORMAT_VERSION,
            kind,
            stage["version"],
            job["revision"],
            job["attempts"],
            job["status"],
            phase,
            started,
            repairs,
            health,
            paused,
        ]
    )
    return body, signature, active


def reconcile_activity(store, project, linear, *, now=None):
    """Independent delivery/backoff: activity failures never block status or release."""
    now = time.time() if now is None else now
    changed = 0
    with store.connect() as db:
        jobs = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM jobs WHERE project=? AND source='linear'", (project.id,)
            )
        ]
    for job in jobs:
        stage = store.stage(job["id"])
        if not stage:
            continue
        with store.connect() as db:
            card = db.execute(
                "SELECT * FROM activity_comments WHERE job=?", (job["id"],)
            ).fetchone()
        if not card and (stage["stage"] in {"done", "cancelled"} or job["cancel"]):
            continue  # Do not add noise to historical completed issues.
        if card and (card["next_retry"] > now or now - card["sent"] < 15):
            continue
        body, signature, active = snapshot(store, job, stage, now)
        if card and card["signature"] == signature and (not active or now - card["sent"] < 120):
            continue
        with store.transaction() as db:
            db.execute(
                "INSERT OR IGNORE INTO activity_comments(job,comment_id) VALUES(?,?)",
                (job["id"], str(uuid.uuid4())),
            )
            card = db.execute(
                "SELECT * FROM activity_comments WHERE job=?", (job["id"],)
            ).fetchone()
        try:
            issue = linear.issue(job["external_id"])
            if not issue or issue.get("archivedAt") or not eligible(issue, project):
                with store.transaction() as db:
                    db.execute(
                        "UPDATE activity_comments SET next_retry=?,error='' WHERE job=?",
                        (now + 300, job["id"]),
                    )
                continue
            state = issue.get("state") or {}
            if state.get("type") in {"completed", "canceled", "duplicate"} and not card["sent"]:
                with store.transaction() as db:
                    db.execute(
                        "UPDATE activity_comments SET next_retry=? WHERE job=?",
                        (now + 300, job["id"]),
                    )
                continue
            # Skip stale snapshots when a worker advances during the API read.
            fresh = store.get(job["id"])
            if snapshot(store, fresh, store.stage(job["id"]), now)[1] != signature:
                continue
            linear.ensure_comment(job["external_id"], card["comment_id"], body, mutable=True)
            with store.transaction() as db:
                db.execute(
                    "UPDATE activity_comments SET body=?,signature=?,sent=?,next_retry=0,error='' WHERE job=?",
                    (body, signature, now, job["id"]),
                )
            changed += 1
        except (DispatchError, OSError, KeyError, TypeError, ValueError):
            with store.transaction() as db:
                db.execute(
                    "UPDATE activity_comments SET next_retry=?,error=? WHERE job=?",
                    (
                        now + 60,
                        "Activity delivery was not confirmed; retrying the same comment",
                        job["id"],
                    ),
                )
    return changed
