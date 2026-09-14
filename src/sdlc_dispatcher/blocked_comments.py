"""Durable explanations for blocked Linear stages, sent by the trusted controller."""

import hashlib
import json
import uuid
from pathlib import Path

from .config import DispatchError
from .privacy import model_report
from .review_contract import validate_schema


def comment_pending(store, job, stage):
    with store.connect() as db:
        row = db.execute(
            "SELECT delivered FROM blocked_comments WHERE job=? AND version=?",
            (job["id"], stage["version"]),
        ).fetchone()
    return not row or not row["delivered"]


def blocked_body(store, job):
    with store.connect() as db:
        event = db.execute(
            """SELECT action,detail FROM audit WHERE job=? AND action IN
            ('blocked','failed','publication_needs_attention','publication_preflight_blocked',
             'policy_changed_or_limit_reached','eligibility_withdrawn','release_blocked') ORDER BY id DESC LIMIT 1""",
            (job["id"],),
        ).fetchone()
        # Never attach an earlier attempt's verdict to a new failed attempt.
        review = db.execute(
            """SELECT * FROM reviews WHERE job=? AND created >=
            (SELECT COALESCE(MAX(at),0) FROM audit WHERE job=? AND action IN
             ('started','ingested','retry_authorized','approved','automatic_approval'))
            ORDER BY created DESC LIMIT 1""",
            (job["id"], job["id"]),
        ).fetchone()
    reason = (
        event["detail"]
        if event and event["detail"]
        else (
            "The dispatcher stopped this run without a detailed diagnostic. "
            "Inspect the private job audit before retrying."
        )
    )
    if job["status"] == "published" and not (event and event["action"] == "release_blocked"):
        reason = (
            "The production release failed, was canceled, or timed out. Inspect the deployment run."
        )
    parts = [
        "**Dispatcher — Blocked**",
        f"Attempt {job['attempts']}. Reason: {reason}",
    ]
    if event and event["action"] == "release_blocked":
        parts.append(
            "This is a release-stage failure. Earlier candidate approval does not establish that this release succeeded. Follow the reason above before retrying."
        )
        body = model_report({"body": "\n\n".join(parts)})["body"]
        return body[:23500] + f"\n\nDispatcher job: `{job['id']}`"
    if event and event["action"].startswith("publication_"):
        parts.append(
            "The publication step stopped after candidate verification. "
            "Inspect publisher diagnostics and reconcile any existing GitHub branch/PR before retrying."
        )
    if review:
        try:
            metadata_path = Path(review["metadata_path"])
            metadata_raw = metadata_path.read_bytes()
            if hashlib.sha256(metadata_raw).hexdigest() != review["metadata_digest"]:
                raise DispatchError("Review metadata changed")
            metadata = json.loads(metadata_raw)
            raw = metadata_path.with_name("review.json").read_bytes()
            if hashlib.sha256(raw).hexdigest() != metadata["review_digest"]:
                raise DispatchError("Review result changed")
            result = json.loads(raw)
            validate_schema(result)
            if result["verdict"] != review["verdict"]:
                raise DispatchError("Review verdict changed")
        except (OSError, ValueError, KeyError, TypeError, DispatchError):
            parts.append(
                "The recorded Astra review could not be verified; inspect private review artifacts."
            )
        else:
            parts.extend([f"**Astra review: {result['verdict']}**", result["summary"]])
            if result["verdict"] == "pass":
                parts.append(
                    "Astra passed this candidate; the blocker occurred elsewhere in the pipeline."
                )
            for finding in result["findings"]:
                location = f" ({finding['path']}:{finding['line']})" if finding["path"] else ""
                kind = "blocking" if finding["blocking"] else "advisory"
                parts.append(
                    f"- {finding['severity']} / {kind}{location}: {finding['requirement']}\n"
                    f"  Evidence: {finding['evidence']}\n"
                    f"  Next step: {finding['requested_change']}"
                )
            if result["limitations"]:
                parts.append(
                    "Evidence limitations:\n" + "\n".join(f"- {x}" for x in result["limitations"])
                )
    else:
        parts.append("No completed Astra review is recorded for this attempt.")
    body = model_report({"body": "\n\n".join(parts)})["body"]
    # Keep provider payloads bounded; private logs, credentials and report context
    # are never copied into the comment.
    if len(body) > 24000:
        body = body[:23900] + "\n\nReview details truncated; inspect private review artifacts."
    return body + f"\n\nDispatcher job: `{job['id']}`"


def deliver_comment(store, job, stage, linear):
    body = blocked_body(store, job)
    # Linear accepts UUIDv4 IDs; INSERT OR IGNORE preserves the first ID on retries.
    comment_id = str(uuid.uuid4())
    with store.transaction() as db:
        db.execute(
            "INSERT OR IGNORE INTO blocked_comments(job,version,comment_id,body) VALUES(?,?,?,?)",
            (job["id"], stage["version"], comment_id, body),
        )
        row = db.execute(
            "SELECT * FROM blocked_comments WHERE job=? AND version=?",
            (job["id"], stage["version"]),
        ).fetchone()
    if row["delivered"]:
        return
    linear.ensure_comment(job["external_id"], row["comment_id"], row["body"])
    with store.transaction() as db:
        db.execute(
            "UPDATE blocked_comments SET delivered=1 WHERE job=? AND version=?",
            (job["id"], stage["version"]),
        )
