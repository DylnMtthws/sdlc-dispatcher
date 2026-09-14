"""Durable, controller-owned Linear status projection. Never executes candidate code."""

import json
import re
import time
from urllib.parse import quote

from .config import DispatchError
from .http import request_json
from .linear import eligible, ingest_feedback, terminal

FIELDS = "id title description archivedAt state { id type } team { id } project { id } labels { nodes { name } }"


class LinearClient:
    def __init__(self, read_token, write_token):
        self.read_token = read_token
        self.write_token = write_token

    def call(self, query, variables, *, write=False):
        result = request_json(
            "https://api.linear.app/graphql",
            "POST",
            {"query": query, "variables": variables},
            {"Authorization": self.write_token if write else self.read_token},
        )
        if result.get("errors") or not isinstance(result.get("data"), dict):
            raise DispatchError(
                "Linear status API refused the operation; inspect key permissions and state IDs"
            )
        return result["data"]

    def issue(self, issue_id):
        return self.call("query($id:String!){issue(id:$id){" + FIELDS + "}}", {"id": issue_id})[
            "issue"
        ]

    def update(self, issue_id, state_id):
        result = self.call(
            """mutation($id:String!,$state:String!){
            issueUpdate(id:$id,input:{stateId:$state}){success issue{id state{id}}}}""",
            {"id": issue_id, "state": state_id},
            write=True,
        )["issueUpdate"]
        if (
            not result.get("success")
            or result.get("issue", {}).get("state", {}).get("id") != state_id
        ):
            raise DispatchError("Linear did not confirm the requested issue status")

    def ensure_comment(self, issue_id, comment_id, body, *, mutable=False):
        # A stable client-generated UUID prevents duplicate creation even when a
        # successful mutation loses its response or the controller crashes.
        cursor = None
        while True:
            issue = self.call(
                """query($id:String!,$after:String){issue(id:$id){
                comments(first:100,after:$after){nodes{id body}
                pageInfo{hasNextPage endCursor}}}}""",
                {"id": issue_id, "after": cursor},
            )["issue"]
            connection = issue["comments"]
            existing = next(
                (item for item in connection["nodes"] if item["id"] == comment_id), None
            )
            if existing:
                if mutable and existing.get("body") != body:
                    result = self.call(
                        """mutation($id:String!,$body:String!){
                        commentUpdate(id:$id,input:{body:$body}){success comment{id body}}}""",
                        {"id": comment_id, "body": body},
                        write=True,
                    )["commentUpdate"]
                    if (
                        not result.get("success")
                        or result.get("comment", {}).get("id") != comment_id
                    ):
                        raise DispatchError("Linear did not confirm the activity update")
                return
            if not connection["pageInfo"]["hasNextPage"]:
                break
            next_cursor = connection["pageInfo"]["endCursor"]
            if not next_cursor or next_cursor == cursor:
                raise DispatchError("Linear comment pagination did not advance")
            cursor = next_cursor
        result = self.call(
            """mutation($input:CommentCreateInput!){
            commentCreate(input:$input){success comment{id}}}""",
            {"input": {"id": comment_id, "issueId": issue_id, "body": body}},
            write=True,
        )["commentCreate"]
        if not result.get("success") or result.get("comment", {}).get("id") != comment_id:
            raise DispatchError("Linear did not confirm the requested issue comment")

    def backfill(self, store, project):
        cursor = None
        total = 0
        for _ in range(40):
            result = self.call(
                """query($id:String!,$after:String){project(id:$id){
                issues(first:50,after:$after){nodes{"""
                + FIELDS
                + """}pageInfo{hasNextPage endCursor}}}}""",
                {"id": project.linear_project_id, "after": cursor},
            )
            connection = result["project"]["issues"]
            for issue in connection["nodes"]:
                if eligible(issue, project):
                    ingest_feedback(store, project, issue)
                    total += 1
            if not connection["pageInfo"]["hasNextPage"]:
                return total
            cursor = connection["pageInfo"]["endCursor"]
        raise DispatchError("Feedback backfill exceeded 2000 issues; narrow project routing")


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{40}", value) is not None


class ReleaseObserver:
    """Read-only GitHub/live evidence, cached for one reconciliation pass."""

    def __init__(self, project, github, health=request_json, store=None):
        self.project, self.github, self.health = project, github, health
        self.store = store
        self._release = None
        self._ancestry = {}

    def includes(self, ancestor, head):
        if not valid_sha(ancestor) or not valid_sha(head):
            raise DispatchError("Invalid commit identity in release evidence")
        if ancestor == head:
            return True
        key = (ancestor, head)
        if key not in self._ancestry:
            result = self.github.call("GET", f"/compare/{ancestor}...{head}")
            self._ancestry[key] = result.get("status") in {"ahead", "identical"}
        return self._ancestry[key]

    def releases(self):
        if self._release is not None:
            return self._release
        try:
            health = self.health(self.project.production_health_url)
        except DispatchError:
            health = {}
        live = health.get("build_sha") if health.get("status") == "ok" else None
        live = live if valid_sha(live) else None
        from . import digitalocean

        if digitalocean.enabled(self.project):
            evidence = digitalocean.status()
            receipt, remote = evidence["release"], evidence["health"]
            verified = (
                {live}
                if live
                and remote.get("healthy")
                and remote.get("build_sha") == live
                and receipt.get("sha") == live
                and receipt.get("status") == "success"
                else set()
            )
            active, failed = [], []
            if self.store:
                with self.store.connect() as db:
                    for row in db.execute(
                        "SELECT status,data FROM release_requests WHERE project=?",
                        (self.project.id,),
                    ):
                        head = json.loads(row[1]).get("merge_sha")
                        if valid_sha(head) and row[0] == "local_deploying":
                            active.append(head)
                        if valid_sha(head) and row[0] == "blocked":
                            failed.append(head)
            self._release = {"live": live, "verified": verified, "active": active, "failed": failed}
            return self._release
        runs = self.github.call(
            "GET",
            "/actions/workflows/deploy-production.yml/runs?branch="
            + quote(self.project.base_ref, safe="")
            + "&event=workflow_dispatch&per_page=30",
        )["workflow_runs"]
        verified, active, failed = set(), [], []
        cache_key = "verified-release:" + self.project.id + ":" + self.project.github_repository
        if self.store and live:
            with self.store.connect() as db:
                cached = db.execute(
                    "SELECT value FROM settings WHERE key=?", (cache_key,)
                ).fetchone()
            if cached and cached[0] == live:
                verified.add(live)
        for run in runs:
            if (
                run.get("path") != ".github/workflows/deploy-production.yml"
                or run.get("event") != "workflow_dispatch"
                or run.get("head_branch") != self.project.base_ref
                or run.get("repository", {}).get("full_name") != self.project.github_repository
                or not valid_sha(run.get("head_sha"))
            ):
                continue
            sha = run["head_sha"]
            # Ignore unrelated old releases. A live proof needs the production job,
            # not just a green preparation job or a matching public health string.
            if (
                sha == live
                and run.get("conclusion") == "success"
                or run.get("status") != "completed"
            ):
                jobs = self.github.call("GET", f"/actions/runs/{run['id']}/jobs?per_page=100")[
                    "jobs"
                ]
                deploy = next((job for job in jobs if job["name"] == "deploy"), None)
                if (
                    deploy
                    and deploy.get("conclusion") == "success"
                    and run.get("conclusion") == "success"
                ):
                    verified.add(sha)
                elif deploy and deploy.get("status") == "in_progress":
                    active.append(sha)
            elif run.get("conclusion") in {"failure", "cancelled", "timed_out"}:
                failed.append(sha)
        if self.store and live in verified:
            with self.store.transaction() as db:
                db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (cache_key, live))
        self._release = {"live": live, "verified": verified, "active": active, "failed": failed}
        return self._release

    def stage(self, job):
        match = re.fullmatch(
            r"https://github\.com/" + re.escape(self.project.github_repository) + r"/pull/(\d+)",
            job["pr_url"] or "",
        )
        if not match:
            raise DispatchError("Unexpected published pull request URL")
        pr = self.github.call("GET", "/pulls/" + match[1])
        if (
            pr.get("base", {}).get("ref") != self.project.base_ref
            or pr.get("head", {}).get("ref") != f"sdlc/{self.project.id}/{job['id']}"
            or pr.get("head", {}).get("repo", {}).get("full_name") != self.project.github_repository
        ):
            raise DispatchError("Published PR identity changed; human reconciliation required")
        if not pr.get("merged"):
            return ("cancelled" if pr.get("state") == "closed" else "awaiting_approval"), ""
        merged = pr.get("merge_commit_sha")
        release = self.releases()
        if release["live"] in release["verified"] and self.includes(merged, release["live"]):
            return "done", release["live"]
        if any(self.includes(merged, sha) for sha in release["active"]):
            return "deploying", ""
        if not release["live"]:
            raise DispatchError("Live build identity is unavailable; completion is not verified")
        if any(self.includes(merged, sha) for sha in release["failed"]):
            return "blocked", ""
        return "ready_for_release", ""


def reconcile(store, project, linear, observer, *, now=None, observe_releases=True):
    now = time.time() if now is None else now
    with store.connect() as db:
        jobs = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM jobs WHERE project=? AND source=?", (project.id, "linear")
            )
        ]
    changed = 0
    for job in jobs:
        stage = store.stage(job["id"])
        if stage and stage["next_retry"] > now:
            continue
        if job["status"] == "published" and job["cancel"]:
            continue  # A user's Canceled/Duplicate selection suspends tracking.
        try:
            if job["status"] == "published" and observe_releases:
                # Completed PRs cannot become unmerged. Keep their proven state
                # without re-reading every historical PR on every polling pass.
                # A changed live SHA still rechecks ancestry, including rollbacks.
                release = observer.releases() if stage and stage["stage"] == "done" else None
                if (
                    release
                    and stage["release_sha"] == release["live"]
                    and release["live"] in release["verified"]
                ):
                    desired, release_sha = "done", release["live"]
                else:
                    desired, release_sha = observer.stage(job)
                from .release_store import projected_stage

                desired = projected_stage(store, project, job, desired)
                store.set_stage(job["id"], desired)
                with store.transaction() as db:
                    db.execute(
                        "UPDATE pipeline SET release_sha=? WHERE job=?", (release_sha, job["id"])
                    )
            elif not stage:
                desired = {
                    "queued": "queued",
                    "running": "coding",
                    "verifying": "testing",
                    "ready": "awaiting_approval",
                    "cancelled": "cancelled",
                }.get(job["status"], "blocked")
                store.set_stage(job["id"], desired)
            stage = store.stage(job["id"])
            if job["status"] in {"ready", "publishing"}:
                from .release_store import settings

                if settings(store, project).get("mode", "off") != "off":
                    store.set_stage(job["id"], "testing")
                    stage = store.stage(job["id"])
            from .blocked_comments import comment_pending, deliver_comment

            needs_comment = stage["stage"] == "blocked" and comment_pending(store, job, stage)
            if stage["synced_version"] == stage["version"] and not needs_comment:
                continue
            state_id = project.linear_statuses.get(stage["stage"])
            if stage["stage"] == "release_approved":
                from .release_store import settings

                state_id = settings(store, project).get("approval_state")
            if not state_id:
                raise DispatchError("Pipeline stage has no registered Linear state")
            issue = linear.issue(job["external_id"])
            if not issue or not eligible(issue, project):
                store.withdraw(project.id, job["external_id"])
                acknowledge(store, job["id"], store.stage(job["id"])["version"], "")
                continue
            state = issue.get("state") or {}
            if state.get("type") in {"canceled", "duplicate"} or (
                terminal(issue, project) and job["status"] != "published"
            ):
                if job["status"] == "published":
                    with store.transaction() as db:
                        db.execute("UPDATE jobs SET cancel=1 WHERE id=?", (job["id"],))
                else:
                    store.withdraw(project.id, job["external_id"], terminal=True)
                store.set_stage(job["id"], "cancelled")
                acknowledge(
                    store, job["id"], store.stage(job["id"])["version"], state.get("id", "")
                )
                continue
            # Do not write a stale intermediate stage if the worker advanced while
            # the network read was in flight. A lost write response is safe to retry:
            # re-read first and avoid a duplicate mutation if the state already matches.
            if store.stage(job["id"])["version"] != stage["version"]:
                continue
            if needs_comment:
                deliver_comment(store, job, stage, linear)
                if store.stage(job["id"])["version"] != stage["version"]:
                    continue
            if state.get("id") != state_id:
                linear.update(job["external_id"], state_id)
                changed += 1
            acknowledge(store, job["id"], stage["version"], state_id)
        except (DispatchError, KeyError, TypeError, ValueError) as exc:
            if not store.stage(job["id"]):
                store.set_stage(
                    job["id"], "awaiting_approval" if job["status"] == "published" else "blocked"
                )
            with store.transaction() as db:
                db.execute(
                    "UPDATE pipeline SET next_retry=?,last_error=? WHERE job=?",
                    (
                        now + 60,
                        (
                            str(exc)
                            if isinstance(exc, DispatchError)
                            else "Invalid provider response; status was not confirmed"
                        ),
                        job["id"],
                    ),
                )
    return changed


def acknowledge(store, job, version, state_id):
    with store.transaction() as db:
        db.execute(
            "UPDATE pipeline SET synced_version=?,synced_state=?,last_error='',next_retry=0 WHERE job=? AND version=?",
            (version, state_id, job, version),
        )
