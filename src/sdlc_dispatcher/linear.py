"""Linear ingress. Feedback is data and routing never comes from its prose."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time

from .config import DispatchError, Project
from .http import request_json
from .store import Store


def _id(data, name):
    value = data.get(name + "Id")
    if value is None and isinstance(data.get(name), dict):
        value = data[name].get("id")
    return value


def eligible(data: dict, project: Project) -> bool:
    labels = data.get("labels", [])
    if isinstance(labels, dict):
        labels = labels.get("nodes", [])
    names = {label.get("name", "").casefold() for label in labels if isinstance(label, dict)}
    return (
        bool(project.linear_team_id and project.linear_project_id)
        and _id(data, "team") == project.linear_team_id
        and _id(data, "project") == project.linear_project_id
        and set(s.casefold() for s in project.required_labels) <= names
    )


def terminal(data, project):
    state = data.get("state") or {}
    return state.get("type") in {"completed", "canceled", "duplicate"} or _id(data, "state") in {
        project.linear_statuses.get(key) for key in ("done", "cancelled", "duplicate")
    } - {None}


def ingest_feedback(store, project, data, *, delivery=None, digest="", auto_ready=False):
    if not eligible(data, project) or terminal(data, project):
        store.withdraw(project.id, data.get("id", ""), terminal=terminal(data, project))
        state = data.get("state") or {}
        if state.get("type") in {"canceled", "duplicate"} or _id(data, "state") in {
            project.linear_statuses.get("cancelled"),
            project.linear_statuses.get("duplicate"),
        } - {None}:
            with store.transaction() as db:
                row = db.execute(
                    "SELECT id FROM jobs WHERE project=? AND source='linear' AND external_id=? AND status='published'",
                    (project.id, data.get("id", "")),
                ).fetchone()
                if row:
                    db.execute("UPDATE jobs SET cancel=1 WHERE id=?", (row["id"],))
                    store._stage(db, row["id"], "cancelled")
        return "ignored"
    return store.ingest(
        project,
        "linear",
        data["id"],
        {"title": data["title"], "description": data.get("description") or ""},
        delivery,
        digest,
        auto_ready=auto_ready
        or (project.automatic_intake and project.linear_intake_mode == "feedback"),
    )


def receive(
    store: Store,
    project: Project,
    raw: bytes,
    signature: str,
    delivery: str,
    secret: str,
    now: float | None = None,
) -> str:
    if len(raw) > 256_000 or not secret:
        raise DispatchError("Webhook disabled or payload too large")
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not isinstance(signature, str) or not hmac.compare_digest(expected, signature):
        raise DispatchError("Invalid webhook signature")
    try:
        event = json.loads(raw)
        timestamp = event["webhookTimestamp"]
        if (
            type(timestamp) not in (int, float)
            or not math.isfinite(timestamp)
            or abs((now if now is not None else time.time()) * 1000 - timestamp) > 60_000
        ):
            raise DispatchError("Webhook timestamp outside allowed window")
        if not isinstance(delivery, str) or not delivery or len(delivery) > 200:
            raise DispatchError("Missing or invalid delivery ID")
        if event.get("type") != "Issue" or event.get("action") not in {
            "create",
            "update",
            "remove",
        }:
            return "ignored"
        data = event["data"]
        if not isinstance(data, dict):
            raise DispatchError("Malformed Linear issue")
        if event.get("action") == "remove" or not eligible(data, project):
            store.withdraw(project.id, data.get("id", ""))
            return "ignored"
        if (
            event.get("action") == "update"
            and project.linear_intake_mode == "approval"
            and "stateId" in event.get("updatedFrom", {})
            and project.linear_ready_state_id
            and _id(data, "state") != project.linear_ready_state_id
        ):
            store.withdraw(project.id, data.get("id", ""))
        auto = (
            event.get("action") == "update"
            and "stateId" in event.get("updatedFrom", {})
            and _id(data, "state") == project.linear_ready_state_id
            and (event.get("actor") or {}).get("id") in project.linear_actor_ids
        )
        return ingest_feedback(
            store,
            project,
            data,
            delivery=delivery,
            digest=hashlib.sha256(raw).hexdigest(),
            auto_ready=auto,
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise DispatchError("Malformed Linear webhook") from exc


def confirm_current(job: dict, project: Project):
    token = os.environ.get("DISPATCHER_LINEAR_API_KEY", "")
    if not token:
        raise DispatchError(
            "Linear jobs require DISPATCHER_LINEAR_API_KEY for fresh issue validation"
        )
    result = request_json(
        "https://api.linear.app/graphql",
        "POST",
        {
            "query": """query DispatcherIssue($id: String!) {
            issue(id: $id) { id title description state { id type } team { id }
                project { id } labels { nodes { name } } }
        }""",
            "variables": {"id": job["external_id"]},
        },
        {"Authorization": token},
    )
    if result.get("errors"):
        raise DispatchError("Could not validate current Linear issue")
    data = (result.get("data") or {}).get("issue")
    if not isinstance(data, dict) or not eligible(data, project):
        raise DispatchError("Issue was removed or no longer matches project eligibility")
    if terminal(data, project):
        raise DispatchError("Linear issue was closed or canceled")
    report = {"title": data["title"], "description": data.get("description") or ""}
    revision = hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest()
    if revision != job["revision"]:
        raise DispatchError(
            "Issue changed since approval; ingest its current content and approve again"
        )
    if (
        project.linear_intake_mode == "approval"
        and project.linear_ready_state_id
        and _id(data, "state") != project.linear_ready_state_id
    ):
        raise DispatchError("Linear issue is no longer in the configured ready state")
