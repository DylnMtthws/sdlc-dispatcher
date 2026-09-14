"""Durable human release approvals, separate from coding policy and intake."""

import hashlib
import json
import time
import uuid
from datetime import datetime

from .config import DispatchError

TERMINAL = {"done", "blocked", "cancelled", "superseded"}


def setup(store):
    with store.connect() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS review_cards (
            job TEXT NOT NULL, head TEXT NOT NULL, digest TEXT NOT NULL,
            manifest TEXT NOT NULL, comment_id TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
            delivered INTEGER NOT NULL DEFAULT 0, ready_at REAL NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '', PRIMARY KEY(job,head)
        );
        CREATE TABLE IF NOT EXISTS release_uploads (
            digest TEXT PRIMARY KEY, url TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS release_events (
            project TEXT NOT NULL, delivery TEXT NOT NULL, digest TEXT NOT NULL,
            created REAL NOT NULL, result TEXT NOT NULL, PRIMARY KEY(project,delivery)
        );
        CREATE TABLE IF NOT EXISTS release_event_order (
            project TEXT NOT NULL, issue TEXT NOT NULL, created REAL NOT NULL,
            PRIMARY KEY(project,issue)
        );
        CREATE TABLE IF NOT EXISTS release_requests (
            id TEXT PRIMARY KEY, job TEXT NOT NULL, project TEXT NOT NULL,
            head TEXT NOT NULL, card_digest TEXT NOT NULL, revision TEXT NOT NULL,
            actor TEXT NOT NULL, delivery TEXT NOT NULL, created REAL NOT NULL,
            updated REAL NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}',
            attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
            error TEXT NOT NULL DEFAULT '', cancel_requested INTEGER NOT NULL DEFAULT 0
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_release_per_issue ON release_requests(job)
          WHERE status NOT IN ('done','blocked','cancelled','superseded');
        """)


def settings(store, project):
    with store.connect() as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key=?", ("linear-release:" + project.id,)
        ).fetchone()
    return json.loads(row[0]) if row else {}


def configure(store, project, config):
    required = {
        "actor_ids",
        "approval_state",
        "review_state",
        "deploying_state",
        "github_owner",
        "enabled_at",
        "mode",
    }
    if not required <= config.keys() or config["mode"] not in {"observe", "live", "off"}:
        raise DispatchError("Incomplete release configuration")
    if not config["actor_ids"] or not all(
        config[k] for k in ("approval_state", "review_state", "deploying_state", "github_owner")
    ):
        raise DispatchError("Release authorization identities are required")
    if config["review_state"] != project.linear_statuses.get("awaiting_approval"):
        raise DispatchError("Review state must match the pipeline mapping")
    setup(store)
    with store.transaction() as db:
        db.execute(
            "INSERT OR REPLACE INTO settings VALUES(?,?)",
            ("linear-release:" + project.id, json.dumps(config, sort_keys=True)),
        )
        store.audit(db, None, "release_configuration", json.dumps(config, sort_keys=True))


def latest(store, job):
    with store.connect() as db:
        row = db.execute(
            "SELECT * FROM release_requests WHERE job=? ORDER BY created DESC,rowid DESC LIMIT 1",
            (job,),
        ).fetchone()
    return dict(row) if row else None


def save(store, request, status=None, **changes):
    data = json.loads(request["data"])
    data.update(changes)
    with store.transaction() as db:
        db.execute(
            "UPDATE release_requests SET status=?,data=?,updated=?,attempts=0,next_retry=0,error='' WHERE id=?",
            (
                status or request["status"],
                json.dumps(data, sort_keys=True),
                time.time(),
                request["id"],
            ),
        )
        store.audit(db, request["job"], "release_progress", status or request["status"])


def transition(store, project, event, delivery, digest, now):
    """Only called after raw webhook signature and timestamp verification."""
    config = settings(store, project)
    if config.get("mode", "off") == "off":
        return None
    setup(store)
    data, actor = event["data"], event.get("actor") or {}
    previous = event.get("updatedFrom") or {}
    state = data.get("stateId") or (data.get("state") or {}).get("id")
    if "stateId" not in previous and event.get("action") != "remove":
        return None
    from .linear import eligible

    with store.transaction() as db:
        seen = db.execute(
            "SELECT * FROM release_events WHERE project=? AND delivery=?", (project.id, delivery)
        ).fetchone()
        if seen:
            if seen["digest"] != digest:
                raise DispatchError("Release webhook delivery changed")
            return seen["result"]
        job = db.execute(
            "SELECT * FROM jobs WHERE project=? AND source='linear' AND external_id=? AND status='published'",
            (project.id, data.get("id", "")),
        ).fetchone()
        result = "release_ignored"
        try:
            created = datetime.fromisoformat(event["createdAt"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError, TypeError):
            created = 0
        owner = actor.get("id") in config["actor_ids"] and actor.get("type", "").lower() == "user"
        if job and owner and created >= config["enabled_at"] and created <= now + 60:
            ordered = db.execute(
                "SELECT created FROM release_event_order WHERE project=? AND issue=?",
                (project.id, job["external_id"]),
            ).fetchone()
            if ordered and created < ordered["created"]:
                db.execute(
                    "INSERT INTO release_events VALUES(?,?,?,?,?)",
                    (project.id, delivery, digest, created, "release_stale"),
                )
                return "release_stale"
            db.execute(
                "INSERT OR REPLACE INTO release_event_order VALUES(?,?,?)",
                (project.id, job["external_id"], created),
            )
            current = db.execute(
                "SELECT * FROM release_requests WHERE job=? ORDER BY created DESC,rowid DESC LIMIT 1",
                (job["id"],),
            ).fetchone()
            if current and created < current["created"]:
                result = "release_stale"
            elif (
                state == config["approval_state"]
                and previous.get("stateId") == config["review_state"]
                and eligible(data, project)
            ):
                card = db.execute(
                    "SELECT * FROM review_cards WHERE job=? AND delivered=1 AND ready_at>0 ORDER BY ready_at DESC LIMIT 1",
                    (job["id"],),
                ).fetchone()
                revision = hashlib.sha256(
                    json.dumps(
                        {"title": data.get("title"), "description": data.get("description") or ""},
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                if (
                    card
                    and created >= int(card["ready_at"])
                    and revision == job["revision"]
                    and not job["cancel"]
                ):
                    if not current or current["status"] in TERMINAL:
                        identifier = str(uuid.uuid4())
                        db.execute(
                            "INSERT INTO release_requests(id,job,project,head,card_digest,revision,actor,delivery,created,updated,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                identifier,
                                job["id"],
                                project.id,
                                card["head"],
                                card["digest"],
                                revision,
                                actor["id"],
                                delivery,
                                created,
                                now,
                                "queued",
                            ),
                        )
                        store.audit(
                            db,
                            job["id"],
                            "linear_release_approved",
                            json.dumps(
                                {
                                    "id": identifier,
                                    "head": card["head"],
                                    "actor": actor["id"],
                                    "delivery": delivery,
                                }
                            ),
                        )
                        result = "release_approved"
                    else:
                        result = "release_already_queued"
                else:
                    result = "release_without_current_review"
            elif (
                current
                and current["status"] not in TERMINAL
                and state not in {config["approval_state"], config["deploying_state"]}
            ):
                db.execute(
                    "UPDATE release_requests SET cancel_requested=1 WHERE id=?", (current["id"],)
                )
                store.audit(db, job["id"], "linear_release_revoked", delivery)
                result = "release_revoked"
        db.execute(
            "INSERT INTO release_events VALUES(?,?,?,?,?)",
            (project.id, delivery, digest, created, result),
        )
    return result


def projected_stage(store, project, job, default):
    """The observer cannot overwrite a human command with its old PR projection."""
    if settings(store, project).get("mode", "off") == "off":
        return default
    setup(store)
    request = latest(store, job["id"])
    if request:
        if request["status"] == "blocked":
            return "blocked"
        if request["status"] not in TERMINAL:
            return "deploying" if request["status"] != "queued" else "release_approved"
    if default == "awaiting_approval":
        with store.connect() as db:
            card = db.execute(
                "SELECT ready_at,error FROM review_cards WHERE job=? ORDER BY rowid DESC LIMIT 1",
                (job["id"],),
            ).fetchone()
        return (
            "awaiting_approval"
            if card and card["ready_at"]
            else "blocked" if card and card["error"] else "testing"
        )
    return default
