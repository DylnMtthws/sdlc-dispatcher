"""Deterministic merge/deploy controller. Models can review but cannot authorize."""

import json
import re
import subprocess
import time
import uuid
from datetime import datetime

from . import digitalocean
from .config import DispatchError
from .linear import eligible
from .linear_sync import ReleaseObserver
from .release_store import latest, save, settings, setup
from .review_cards import publish_card, ready


class ReviewChanged(DispatchError):
    """The human must inspect newly generated evidence before another release."""


class Deferred(DispatchError):
    """A shared resource is busy; wait without consuming a repair attempt."""


class GitHubRelease:
    """Use the trusted operator's existing gh session, outside every agent sandbox."""

    def __init__(self, repository):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repository):
            raise DispatchError("Invalid release repository")
        self.repository = repository

    def api(self, path, method="GET", body=None):
        args = ["gh", "api", "--hostname", "github.com", path, "--method", method]
        if body is not None:
            args += ["--input", "-"]
        try:
            r = subprocess.run(
                args,
                input=None if body is None else json.dumps(body).encode(),
                capture_output=True,
                check=True,
                timeout=90,
            )
            return json.loads(r.stdout) if r.stdout.strip() else None
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise DispatchError(
                "GitHub release request failed; reconcile recorded operation before retry"
            ) from exc

    def call(self, method, path, body=None):
        return self.api("repos/" + self.repository + path, method, body)

    def make_ready(self, pr):
        if not pr.get("draft"):
            return
        result = self.api(
            "graphql",
            "POST",
            {
                "query": "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{isDraft}}}",
                "variables": {"id": pr["node_id"]},
            },
        )
        if (
            result.get("errors")
            or result["data"]["markPullRequestReadyForReview"]["pullRequest"]["isDraft"]
        ):
            raise DispatchError("GitHub did not mark the reviewed PR ready")


def pr_number(project, job):
    match = re.fullmatch(
        r"https://github\.com/" + re.escape(project.github_repository) + r"/pull/(\d+)",
        job.get("pr_url") or "",
    )
    if not match:
        raise DispatchError("Published pull request identity is invalid")
    return int(match[1])


def validate_pr(project, job, pr):
    if (
        pr["base"]["ref"] != project.base_ref
        or pr["head"]["ref"] != f"sdlc/{project.id}/{job['id']}"
        or pr["head"]["repo"]["full_name"] != project.github_repository
        or pr["base"]["repo"]["full_name"] != project.github_repository
    ):
        raise DispatchError("Pull request identity changed")


def checks(github, head, owner):
    runs = github.call("GET", f"/commits/{head}/check-runs?per_page=100")["check_runs"]
    selected = {}
    for run in sorted(runs, key=lambda r: r["id"], reverse=True):
        if run.get("app", {}).get("id") == 15368:
            selected.setdefault(run["name"], run)
    statuses = github.call("GET", f"/commits/{head}/statuses?per_page=100")
    astra = next((s for s in statuses if s["context"] == "dispatcher/astra"), {})
    passed = all(
        selected.get(name, {}).get("conclusion") == "success"
        for name in ("test", "review", "container")
    )
    return (
        passed
        and astra.get("state") == "success"
        and astra.get("creator", {}).get("login") == owner,
        selected,
    )


def current_issue(linear, project, job, config, accepted):
    import hashlib

    issue = linear.issue(job["external_id"])
    if not issue or not eligible(issue, project):
        raise DispatchError("Issue is no longer eligible; release stopped")
    revision = hashlib.sha256(
        json.dumps(
            {"title": issue["title"], "description": issue.get("description") or ""}, sort_keys=True
        ).encode()
    ).hexdigest()
    if revision != job["revision"]:
        raise DispatchError("Issue scope changed after review; review the new request")
    if accepted and issue["state"]["id"] not in {
        config["approval_state"],
        config["deploying_state"],
    }:
        raise DispatchError("Linear approval was withdrawn; no further release action authorized")
    return issue


class Controller:
    def __init__(self, store, project, linear, github, evidence, health=None):
        self.store, self.project, self.linear, self.github, self.evidence = (
            store,
            project,
            linear,
            github,
            evidence,
        )
        self.health = health
        setup(store)

    def config(self):
        return settings(self.store, self.project)

    def observer(self):
        kwargs = {"health": self.health} if self.health else {}
        return ReleaseObserver(self.project, self.github, store=self.store, **kwargs)

    def pr(self, job):
        pr = self.github.call("GET", f"/pulls/{pr_number(self.project, job)}")
        validate_pr(self.project, job, pr)
        return pr

    def tick(self):
        config = self.config()
        if config.get("mode", "off") == "off" or self.store.paused():
            return
        with self.store.connect() as db:
            pending = db.execute(
                "SELECT * FROM release_requests WHERE project=? AND status NOT IN ('done','cancelled','superseded','blocked') ORDER BY created,rowid LIMIT 1",
                (self.project.id,),
            ).fetchone()
        if pending:
            request = dict(pending)
            if request["next_retry"] > time.time() or config["mode"] != "live":
                return
            try:
                self.advance(request)
            except Deferred:
                return
            except ReviewChanged as exc:
                self.return_to_review(request, str(exc))
            except (DispatchError, OSError, ValueError, KeyError, TypeError) as exc:
                reason = (
                    str(exc)
                    if isinstance(exc, DispatchError)
                    else "Release data or provider response could not be verified"
                )
                self.fail(request, reason)
            return
        for job in self.store.jobs():
            job = self.store.get(job["id"])
            if job["project"] != self.project.id or job["status"] != "published" or job["cancel"]:
                continue
            stage = self.store.stage(job["id"])
            if stage and stage["stage"] == "done":
                continue
            request = latest(self.store, job["id"])
            if request and request["status"] == "blocked":
                continue
            try:
                self.prepare(job, config)
            except Deferred:
                continue
            except (DispatchError, OSError, ValueError, KeyError, TypeError) as exc:
                reason = (
                    str(exc)
                    if isinstance(exc, DispatchError)
                    else "Review evidence could not be verified"
                )
                # A bounded card-preparation retry uses a separate durable counter.
                key = "review-preparation:" + job["id"]
                with self.store.transaction() as db:
                    old = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                    state = json.loads(old[0]) if old else {"attempts": 0, "next": 0}
                    if state["next"] > time.time() or state["attempts"] >= 3:
                        continue
                    state.update(attempts=state["attempts"] + 1, next=time.time() + 60)
                    db.execute(
                        "INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(state))
                    )
                    if state["attempts"] >= 3:
                        db.execute(
                            "INSERT OR IGNORE INTO review_cards(job,head,digest,manifest,comment_id,error) VALUES(?,?,?,?,?,?)",
                            (job["id"], "unavailable", "", "{}", str(uuid.uuid4()), reason),
                        )
                        self.store.audit(db, job["id"], "release_blocked", reason)
                        self.store._stage(db, job["id"], "blocked")

    def prepare(self, job, config):
        pr = self.pr(job)
        if pr.get("merged") or pr["state"] == "closed":
            return
        with self.store.transaction() as db:
            key = "review-head:" + job["id"]
            known = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if known and known[0] != pr["head"]["sha"]:
                db.execute("DELETE FROM settings WHERE key=?", ("review-preparation:" + job["id"],))
                db.execute("UPDATE review_cards SET error='' WHERE job=?", (job["id"],))
            db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, pr["head"]["sha"]))
            db.execute(
                "UPDATE review_cards SET ready_at=0 WHERE job=? AND head!=?",
                (job["id"], pr["head"]["sha"]),
            )
        with self.store.connect() as db:
            retry = db.execute(
                "SELECT value FROM settings WHERE key=?", ("review-preparation:" + job["id"],)
            ).fetchone()
        if retry:
            state = json.loads(retry[0])
            if state["attempts"] >= 3 or state["next"] > time.time():
                return
        issue = current_issue(self.linear, self.project, job, config, False)
        with self.store.transaction() as db:
            key = "unverified-release-command:" + job["id"]
            seen = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            if issue["state"]["id"] == config["approval_state"]:
                if not seen:
                    db.execute("INSERT INTO settings VALUES(?,?)", (key, str(time.time())))
                elif time.time() - float(seen[0]) > 90:
                    raise DispatchError(
                        "Ready to Deploy was observed without a verified owner webhook. Move the issue back to Ready for Your Review, then select Ready to Deploy again; check webhook delivery if this repeats."
                    )
            else:
                db.execute("DELETE FROM settings WHERE key=?", (key,))
        folder, review = self.evidence.ensure(job, pr, approved_head=None)
        passed, selected = checks(self.github, pr["head"]["sha"], config["github_owner"])
        if not passed:
            with self.store.transaction() as db:
                db.execute(
                    "UPDATE review_cards SET ready_at=0 WHERE job=? AND head=?",
                    (job["id"], pr["head"]["sha"]),
                )
                self.store._stage(db, job["id"], "testing")
            self.recover_ci(job, pr["head"]["sha"], selected)
            return
        # Re-read after potentially slow browser/model work.
        if self.pr(job)["head"]["sha"] != pr["head"]["sha"]:
            return
        publish_card(self.store, self.project, job, pr["head"]["sha"], folder, review, self.linear)
        ready(self.store, job, pr["head"]["sha"])

    def recover_ci(self, job, head, selected):
        runs = self.github.call(
            "GET", f"/actions/workflows/ci.yml/runs?head_sha={head}&event=pull_request&per_page=30"
        )["workflow_runs"]
        if not runs or any(r["status"] != "completed" for r in runs):
            return
        run = runs[0]
        if run["conclusion"] == "success":
            raise DispatchError("Required exact-commit checks are missing despite successful CI")
        if run["run_attempt"] >= 3:
            raise DispatchError(
                f"CI failed after three attempts: {run['html_url']}. Inspect failed checks and repair the candidate."
            )
        self.github.call("POST", f"/actions/runs/{run['id']}/rerun-failed-jobs", {})

    def authorize(self, request, job, config):
        config = self.config()
        if self.store.paused():
            raise Deferred("Dispatcher paused")
        latest_request = latest(self.store, job["id"])
        if (
            not latest_request
            or latest_request["id"] != request["id"]
            or latest_request["cancel_requested"]
            or job["cancel"]
        ):
            raise DispatchError(
                "Release approval was withdrawn; inspect merge/deployment state before retry"
            )
        if (
            request["actor"] not in config["actor_ids"]
            or request["revision"] != job["revision"]
            or config["mode"] != "live"
        ):
            raise DispatchError("Release approval identity or scope changed")
        with self.store.connect() as db:
            card = db.execute(
                "SELECT * FROM review_cards WHERE job=? AND head=?", (job["id"], request["head"])
            ).fetchone()
        if (
            not card
            or not card["delivered"]
            or not card["ready_at"]
            or card["digest"] != request["card_digest"]
        ):
            raise DispatchError("Release has no matching delivered screenshot review")
        current_issue(self.linear, self.project, job, config, True)
        if self.github.api("user")["login"] != config["github_owner"]:
            raise DispatchError("GitHub release identity does not match the configured owner")

    def advance(self, request):
        job, config = self.store.get(request["job"]), self.config()
        if time.time() - request["created"] > 7200:
            raise DispatchError(
                "Release exceeded its two-hour processing window; inspect progress before approving another attempt"
            )
        self.authorize(request, job, config)
        data = json.loads(request["data"])
        pr = self.pr(job)
        if request["status"] == "local_deploying":
            evidence = digitalocean.status()
            release, health = evidence["release"], evidence["health"]
            if not (
                release.get("status") == "success"
                and release.get("sha") == data["merge_sha"]
                and release.get("ci_run_id") == data["ci_run"]
                and health.get("healthy")
                and health.get("build_sha") == data["merge_sha"]
            ):
                if time.time() - data["dispatch_at"] < 1800:
                    return
                raise DispatchError(
                    "DigitalOcean promotion has no matching healthy receipt; inspect before retrying"
                )
            comment_id = data.get("done_comment") or str(uuid.uuid4())
            if not data.get("done_comment"):
                save(self.store, request, done_comment=comment_id)
            self.linear.ensure_comment(
                job["external_id"],
                comment_id,
                f"**Deployed successfully**\n\nThis fix is live on DigitalOcean build `{data['merge_sha'][:12]}`. Production health and encrypted backup were verified.",
            )
            save(self.store, request, "done", done_comment=comment_id)
            self.store.set_stage(job["id"], "done")
            return
        if request["status"] == "queued":
            if pr.get("merged"):
                # A new real owner approval may resume a failed deployment of
                # this controller's exact prior merge, without merging twice.
                with self.store.connect() as db:
                    previous = db.execute(
                        "SELECT * FROM release_requests WHERE job=? AND id!=? AND status='blocked' ORDER BY created DESC LIMIT 1",
                        (job["id"], request["id"]),
                    ).fetchone()
                prior = json.loads(previous["data"]) if previous else {}
                merge = pr.get("merge_commit_sha")
                if (
                    not previous
                    or previous["revision"] != request["revision"]
                    or previous["head"] != request["head"]
                    or pr["head"]["sha"] != request["head"]
                    or prior.get("expected_head") != request["head"]
                    or prior.get("merge_sha") != merge
                    or self.github.call("GET", "/git/ref/heads/" + self.project.base_ref)["object"][
                        "sha"
                    ]
                    != merge
                ):
                    raise DispatchError(
                        "Merged release cannot be resumed: approved commit, recorded merge, or current main differs"
                    )
                self.preflight()
                save(
                    self.store,
                    request,
                    "main_ci",
                    expected_head=request["head"],
                    merge_sha=merge,
                    retry_of=previous["id"],
                )
                self.store.set_stage(job["id"], "deploying")
                self.linear.update(job["external_id"], config["deploying_state"])
                return
            if pr["state"] != "open" or pr["head"]["sha"] != request["head"]:
                raise ReviewChanged(
                    "PR changed before approval was consumed; inspect fresh screenshots and approve again"
                )
            self.preflight()
            save(self.store, request, "validating", expected_head=request["head"])
            self.store.set_stage(job["id"], "deploying")
            self.linear.update(job["external_id"], config["deploying_state"])
            return
        if request["status"] == "validating":
            if pr.get("merged"):
                raise DispatchError("PR merged outside this release operation")
            if pr["head"]["sha"] != data["expected_head"]:
                if not data.get("updating"):
                    raise ReviewChanged("PR changed outside the authorized branch update")
                save(self.store, request, expected_head=pr["head"]["sha"], updating=False)
                return
            base = self.github.call("GET", "/git/ref/heads/" + self.project.base_ref)["object"][
                "sha"
            ]
            release = self.observer().releases()
            if release["live"] != base or base not in release["verified"]:
                raise DispatchError(
                    "Main contains changes not verified live; reconcile the outstanding release before merging another issue"
                )
            comparison = self.github.call("GET", f"/compare/{base}...{pr['head']['sha']}")
            if comparison["status"] == "diverged":
                if pr.get("mergeable") is False:
                    raise DispatchError(
                        "GitHub reports merge conflicts. Repair the conflicting candidate and obtain fresh screenshots and Astra review before approving another release."
                    )
                if data.get("updates", 0) >= 3:
                    raise DispatchError(
                        "Main moved repeatedly; refresh review before another attempt"
                    )
                save(self.store, request, updating=True, updates=data.get("updates", 0) + 1)
                self.github.call(
                    "PUT",
                    f"/pulls/{pr['number']}/update-branch",
                    {"expected_head_sha": pr["head"]["sha"]},
                )
                return
            if comparison["status"] != "ahead":
                raise DispatchError("Candidate is not a forward change from main")
            self.evidence.ensure(job, pr, approved_head=request["head"])
            passed, selected = checks(self.github, pr["head"]["sha"], config["github_owner"])
            if not passed:
                self.recover_ci(job, pr["head"]["sha"], selected)
                return
            self.authorize(request, job, config)
            self.github.make_ready(pr)
            save(self.store, request, "merging", expected_head=pr["head"]["sha"], base=base)
            return
        if request["status"] == "merging":
            if pr.get("merged"):
                if pr["head"]["sha"] != data["expected_head"]:
                    raise DispatchError("Unexpected commit was merged")
                save(self.store, request, "main_ci", merge_sha=pr["merge_commit_sha"])
                return
            if pr["head"]["sha"] != data["expected_head"]:
                raise ReviewChanged("PR changed at merge time; review again")
            if (
                self.github.call("GET", "/git/ref/heads/" + self.project.base_ref)["object"]["sha"]
                != data["base"]
            ):
                save(self.store, request, "validating")
                return
            passed, _ = checks(self.github, data["expected_head"], config["github_owner"])
            if not passed:
                save(self.store, request, "validating")
                return
            self.authorize(request, job, config)
            self.preflight()
            self.github.call(
                "PUT",
                f"/pulls/{pr['number']}/merge",
                {"sha": data["expected_head"], "merge_method": "merge"},
            )
            return  # Reconcile the actual merge SHA on the next pass, including lost responses.
        if request["status"] == "main_ci":
            base = self.github.call("GET", "/git/ref/heads/" + self.project.base_ref)["object"][
                "sha"
            ]
            if base != data["merge_sha"]:
                raise DispatchError(
                    "Main changed after the approved merge; no unrelated changes will be deployed"
                )
            runs = self.github.call(
                "GET",
                f"/actions/workflows/ci.yml/runs?head_sha={base}&event=push&branch={self.project.base_ref}&per_page=30",
            )["workflow_runs"]
            if not runs or runs[0]["status"] != "completed":
                return
            run = runs[0]
            if run["conclusion"] != "success":
                if run["run_attempt"] < 3:
                    self.github.call("POST", f"/actions/runs/{run['id']}/rerun-failed-jobs", {})
                    return
                raise DispatchError(f"Merged build failed CI after retries: {run['html_url']}")
            # Persist intent before dispatch. An ambiguous dispatch is reconciled, never blindly repeated.
            self.authorize(request, job, config)
            self.preflight()
            if digitalocean.enabled(self.project):
                save(
                    self.store,
                    request,
                    "local_deploying",
                    ci_run=run["id"],
                    dispatch_at=time.time(),
                )
                digitalocean.deploy(run["id"], base)
                return
            save(self.store, request, "dispatching", ci_run=run["id"], dispatch_at=time.time())
            self.github.call(
                "POST",
                "/actions/workflows/deploy-production.yml/dispatches",
                {"ref": self.project.base_ref, "inputs": {"ci_run_id": str(run["id"])}},
            )
            return
        if request["status"] == "dispatching":
            runs = self.github.call(
                "GET",
                "/actions/workflows/deploy-production.yml/runs?event=workflow_dispatch&per_page=30",
            )["workflow_runs"]
            matching = [
                r
                for r in runs
                if r["head_sha"] == data["merge_sha"]
                and r["head_branch"] == self.project.base_ref
                and r["actor"]["login"] == config["github_owner"]
                and datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).timestamp()
                >= int(data["dispatch_at"])
            ]
            if len(matching) > 1:
                raise DispatchError(
                    "Multiple matching release dispatches; reconcile before approval"
                )
            if matching:
                save(
                    self.store,
                    request,
                    "deploying",
                    run_id=matching[0]["id"],
                    run_url=matching[0]["html_url"],
                )
            elif time.time() - data["dispatch_at"] > 180:
                raise DispatchError(
                    "Release dispatch could not be confirmed; inspect GitHub before retrying"
                )
            return
        if request["status"] == "deploying":
            run = self.github.call("GET", f"/actions/runs/{data['run_id']}")
            if (
                run["head_sha"] != data["merge_sha"]
                or run["event"] != "workflow_dispatch"
                or run["path"] != ".github/workflows/deploy-production.yml"
                or run["actor"]["login"] != config["github_owner"]
            ):
                raise DispatchError("Release workflow identity changed")
            if run["status"] == "completed":
                if run["conclusion"] != "success":
                    raise DispatchError(
                        f"Production workflow failed: {data['run_url']}. Inspect backup and deployment receipt before retrying; the old build may still be live."
                    )
                desired, sha = self.observer().stage(job)
                if desired != "done" or sha != data["merge_sha"]:
                    raise DispatchError(
                        "Workflow passed but expected production build is not verified healthy"
                    )
                body = f"**Deployed successfully**\n\nThis fix is live on build `{sha[:12]}`. Production health and the successful deployment were verified.\n\n[Release details]({data['run_url']})"
                comment_id = data.get("done_comment") or str(uuid.uuid4())
                if not data.get("done_comment"):
                    save(self.store, request, done_comment=comment_id)
                self.linear.ensure_comment(job["external_id"], comment_id, body)
                save(self.store, request, "done", done_comment=comment_id)
                self.store.set_stage(job["id"], "done")
                return
            pending = self.github.call("GET", f"/actions/runs/{data['run_id']}/pending_deployments")
            if pending:
                if (
                    len(pending) != 1
                    or pending[0]["environment"]["name"] != "production"
                    or pending[0]["environment"]["id"]
                    != config.get("production_environment_id", pending[0]["environment"]["id"])
                    or not pending[0]["current_user_can_approve"]
                ):
                    raise DispatchError(
                        "Unexpected production approval gate or insufficient reviewer permission"
                    )
                self.authorize(request, job, config)
                if (
                    self.github.call("GET", "/git/ref/heads/" + self.project.base_ref)["object"][
                        "sha"
                    ]
                    != data["merge_sha"]
                ):
                    raise DispatchError("Main changed before production approval")
                self.github.call(
                    "POST",
                    f"/actions/runs/{data['run_id']}/pending_deployments",
                    {
                        "environment_ids": [pending[0]["environment"]["id"]],
                        "state": "approved",
                        "comment": f"Authorized by Linear user {request['actor']} via recorded approval {request['id']} for PR {pr['number']}; reviewed candidate {request['head']}.",
                    },
                )

    def preflight(self):
        check = getattr(self.evidence, "preflight", None)
        if check:
            check()

    def fail(self, request, reason):
        with self.store.transaction() as db:
            row = dict(
                db.execute("SELECT * FROM release_requests WHERE id=?", (request["id"],)).fetchone()
            )
            attempts = row["attempts"] + 1
            final = attempts >= 3 or row["cancel_requested"]
            cancelled = bool(row["cancel_requested"] and row["status"] in {"queued", "validating"})
            db.execute(
                "UPDATE release_requests SET attempts=?,next_retry=?,error=?,status=? WHERE id=?",
                (
                    attempts,
                    time.time() + 60,
                    reason,
                    "cancelled" if cancelled else "blocked" if final else row["status"],
                    request["id"],
                ),
            )
            self.store.audit(
                db,
                request["job"],
                (
                    "release_cancelled"
                    if cancelled
                    else "release_blocked" if final else "release_retry"
                ),
                reason,
            )
            if cancelled:
                self.store._stage(db, request["job"], "awaiting_approval")
            elif final:
                self.store._stage(db, request["job"], "blocked")

    def return_to_review(self, request, reason):
        with self.store.transaction() as db:
            db.execute(
                "UPDATE release_requests SET status='superseded',error=?,updated=? WHERE id=?",
                (reason, time.time(), request["id"]),
            )
            db.execute("UPDATE review_cards SET ready_at=0 WHERE job=?", (request["job"],))
            self.store.audit(db, request["job"], "release_review_refresh", reason)
            self.store._stage(db, request["job"], "testing")
