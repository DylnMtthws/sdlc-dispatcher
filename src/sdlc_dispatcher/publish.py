"""Publish reviewed text changes via GitHub objects, never execute a checkout."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from .config import DispatchError, Project
from .http import ProviderError, request_json
from .review_gate import publication_review
from .store import Store


def publish_ready(store, project, job_id, *, now=None):
    """Invoke a separate credential-holding publisher after the worker is done."""
    now = time.time() if now is None else now
    job = store.get(job_id)
    if not project.automatic_publication or job["status"] not in {"ready", "publishing"}:
        return
    if store.paused() or job["cancel"]:
        return
    recovering = job["status"] == "publishing"
    if recovering and job["deadline"] > now:
        return
    with store.transaction() as db:
        db.execute(
            "INSERT OR IGNORE INTO publication_attempts(job,artifact_digest) VALUES(?,?)",
            (job_id, job["artifact_digest"]),
        )
        attempt = db.execute(
            "SELECT * FROM publication_attempts WHERE job=? AND artifact_digest=?",
            (job_id, job["artifact_digest"]),
        ).fetchone()
        if attempt["attempts"] >= 3 or attempt["next_retry"] > now:
            return
        db.execute(
            "UPDATE publication_attempts SET attempts=attempts+1,next_retry=? WHERE job=? AND artifact_digest=?",
            (now + 600, job_id, job["artifact_digest"]),
        )
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "TMPDIR"}}
    try:
        subprocess.run(
            [*project.publish_command, job_id, *(["--reconcile"] if recovering else [])],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=180,
        )
        with store.transaction() as db:
            db.execute(
                "UPDATE publication_attempts SET next_retry=0,last_error='' WHERE job=? AND artifact_digest=?",
                (job_id, job["artifact_digest"]),
            )
    except (OSError, subprocess.SubprocessError) as exc:
        detail = "Publisher process failed; inspect private publisher diagnostics"
        if isinstance(exc, subprocess.CalledProcessError):
            try:
                error = json.loads(exc.stderr)["error"]
                if isinstance(error, str):
                    from .privacy import model_report

                    detail = model_report({"error": error[:1000]})["error"]
            except (TypeError, ValueError, KeyError):
                pass
        elif isinstance(exc, subprocess.TimeoutExpired):
            detail = "Publisher timed out; reconcile the existing branch/PR after the lease expires"
        retry = attempt["attempts"] + 1 < 3 and store.get(job_id)["status"] in {
            "ready",
            "publishing",
        }
        with store.transaction() as db:
            db.execute(
                "UPDATE publication_attempts SET next_retry=?,last_error=? WHERE job=? AND artifact_digest=?",
                (now + 60, detail, job_id, job["artifact_digest"]),
            )
            store.audit(
                db,
                job_id,
                "publication_retry_scheduled" if retry else "publication_needs_attention",
                detail,
            )


def recover_publications(store, project):
    """A provider outage must not strand a verified candidate or stall coding."""
    with store.connect() as db:
        jobs = [
            r["id"]
            for r in db.execute(
                "SELECT id FROM jobs WHERE project=? AND status IN ('ready','publishing')",
                (project.id,),
            )
        ]
    for job_id in jobs:
        publish_ready(store, project, job_id)


class GitHub:
    def __init__(self, repository: str, token: str):
        self.base = "https://api.github.com/repos/" + repository
        self.headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/vnd.github+json",
        }

    def call(self, method, path, body=None):
        return request_json(self.base + path, method, body, self.headers)


def publish(store: Store, project: Project, job_id: str, *, reconcile=False, client=None) -> str:
    if not project.github_repository:
        raise DispatchError("Register a GitHub repository before publishing")
    if store.paused():
        raise DispatchError("Dispatcher is paused")
    if client is None:
        token = os.environ.get("DISPATCHER_GITHUB_TOKEN", "")
        if not token:
            raise DispatchError("Set DISPATCHER_GITHUB_TOKEN in the publisher environment")
        client = GitHub(project.github_repository, token)
    with store.transaction() as db:
        row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row or row["project"] != project.id:
            raise DispatchError("Unknown project job")
        job = dict(row)
        if job["status"] == "published":
            return job["pr_url"]
        if job["status"] != "ready" and not (reconcile and job["status"] == "publishing"):
            raise DispatchError("Job must be ready; ambiguous publication requires --reconcile")
        if job["status"] == "publishing" and job["deadline"] > time.time():
            raise DispatchError("Publisher lease is still active; reconcile after it expires")
        if job["source"] == "demo":
            raise DispatchError("Demo artifacts cannot be published")
        if job["policy"] != project.fingerprint or job["cancel"]:
            raise DispatchError("Approval policy changed or job cancelled")
        path = Path(job["artifact"])
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != job["artifact_digest"]:
            raise DispatchError("Artifact changed after verification")
        artifact = json.loads(raw)
        if artifact["engine"] == "demo" or artifact["revision"] != job["revision"]:
            raise DispatchError("Artifact is not eligible for publication")
        review_receipt = publication_review(store, project, job, artifact)
        # A crash after any provider write leaves a durable reconciliation state.
        db.execute(
            "UPDATE jobs SET status='publishing',deadline=?,updated=? WHERE id=?",
            (time.time() + 600, time.time(), job_id),
        )
        store.audit(db, job_id, "publication_started")

    def call(method, path, body=None):
        if store.paused():
            raise DispatchError("Dispatcher paused during publication; reconcile before continuing")
        return client.call(method, path, body)

    try:
        if job["source"] == "linear":
            from .linear import confirm_current

            confirm_current(job, project)
        base = call("GET", "/git/ref/heads/" + quote(project.base_ref, safe="/"))
        base_changed = base["object"]["sha"] != artifact["base_sha"]
        if base_changed and job["status"] != "publishing":
            raise DispatchError(
                "Base branch moved; approve a fresh attempt to reverify before publishing"
            )
    except DispatchError as exc:
        if job["status"] == "ready":
            # No provider mutation has been attempted on this first publication.
            with store.transaction() as db:
                db.execute(
                    "UPDATE jobs SET status='blocked',updated=? WHERE id=?", (time.time(), job_id)
                )
                store.audit(db, job_id, "publication_preflight_blocked", str(exc))
        raise
    commit = call("GET", "/git/commits/" + artifact["base_sha"])
    tree_items = []
    for change in artifact["changes"]:
        item = {"path": change["path"], "mode": change["mode"], "type": "blob"}
        if change["after"] is None:
            item["sha"] = None
        else:
            item["content"] = change["after"]
        tree_items.append(item)
    tree = call("POST", "/git/trees", {"base_tree": commit["tree"]["sha"], "tree": tree_items})
    branch = f"sdlc/{project.id}/{job_id}"
    ref_path = "/git/ref/heads/" + branch
    try:
        ref = call("GET", ref_path)
    except ProviderError as exc:
        if exc.status != 404:
            raise
        if base_changed:
            raise DispatchError(
                "Base moved during interrupted publication; no new branch will be created"
            ) from None
        new = call(
            "POST",
            "/git/commits",
            {
                "message": f"Fix feedback {job_id}\n\nDispatcher artifact: {job['artifact_digest']}",
                "tree": tree["sha"],
                "parents": [artifact["base_sha"]],
            },
        )
        ref = call("POST", "/git/refs", {"ref": "refs/heads/" + branch, "sha": new["sha"]})
    actual = call("GET", "/git/commits/" + ref["object"]["sha"])
    if actual["tree"]["sha"] != tree["sha"] or [p["sha"] for p in actual["parents"]] != [
        artifact["base_sha"]
    ]:
        raise DispatchError("Existing automation branch differs; refusing to overwrite")
    if review_receipt:
        call(
            "POST",
            "/statuses/" + ref["object"]["sha"],
            {
                "state": "success",
                "context": "dispatcher/astra",
                "description": "Astra High passed this exact verified candidate",
                "target_url": (
                    review_receipt["preview"]["url"]
                    if review_receipt["preview"]
                    else f"https://github.com/{project.github_repository}/commit/{ref['object']['sha']}"
                ),
            },
        )
    owner = project.github_repository.split("/")[0]
    existing = call("GET", "/pulls?state=all&head=" + quote(owner + ":" + branch, safe=""))
    if existing:
        pr = existing[0]
    else:
        if base_changed:
            raise DispatchError(
                "Base moved; existing branch needs human reconciliation before a new PR"
            )
        body = (
            f"Proposed fix for dispatcher job `{job_id}`.\n\n"
            "Independent baseline and candidate checks passed in an offline container. "
            "Review the code and private dispatcher evidence before merging. "
            "This run does not deploy or close the source issue.\n\n"
            f"Base commit: `{artifact['base_sha']}`\n"
            f"Artifact digest: `{job['artifact_digest']}`\n"
            f"Environment: `{artifact['image']}`\n"
        )
        body += (
            "\nChanged files:\n\n"
            + "\n".join("- `" + item["path"].replace("`", "") + "`" for item in artifact["changes"])
            + "\n"
        )
        if review_receipt:
            review = review_receipt["review"]
            body += (
                "\nIndependent review: **Astra High — pass**. "
                f"{len(review['findings'])} advisory finding(s); no blocking findings.\n"
                f"Review packet: `{review_receipt['input_digest']}`\n"
            )
            # Do not publish raw feedback, reviewer prose, emails, logs or screenshots
            # into a possibly public repository. Source locations are sufficient to
            # locate advisory findings; detailed evidence remains in the private preview.
            for finding in review["findings"]:
                body += f"\n- Advisory ({finding['severity']}): `{finding['path'].replace('`', '')}:{finding['line']}`\n"
            if review_receipt["preview"]:
                body += f"\n[Try this exact candidate (Tailscale required)]({review_receipt['preview']['url']})\n"
                body += f"\n[Read the detailed Astra review]({review_receipt['preview']['url']}/__dispatcher__/review)\n"
            from .release_store import settings

            if settings(store, project).get("mode", "off") != "off":
                body += "\nReview the evidence in Linear when the issue reaches Ready for Your Review. Moving it to Ready to Deploy authorizes the controller to merge and release this candidate.\n"
            else:
                body += (
                    "\nMark this draft ready and merge after trying the preview and CI passes. "
                    "Merging accepts the code. Production release requires the project’s separate approval workflow.\n"
                )
        if job["source"] == "linear":
            # UUIDs are validated at intake; never copy the report title/body here.
            body += f"\nLinear issue reference: `{job['external_id']}`\n"
        pr = call(
            "POST",
            "/pulls",
            {
                "title": f"Fix feedback: {project.id} ({job_id[:8]})",
                "head": branch,
                "base": project.base_ref,
                "body": body,
                "draft": True,
            },
        )
    expected_prefix = f"https://github.com/{project.github_repository}/pull/"
    if not pr.get("html_url", "").startswith(expected_prefix):
        raise DispatchError("Unexpected pull request response")
    with store.transaction() as db:
        db.execute(
            "UPDATE jobs SET status='published',pr_url=?,updated=? WHERE id=?",
            (pr["html_url"], time.time(), job_id),
        )
        store.audit(db, job_id, "published", pr["html_url"])
    return pr["html_url"]
