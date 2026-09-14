"""Persistent Linear-authorized release service; credentials stay in this controller."""

import argparse
import fcntl
import json
import os
import shutil
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import evidence
from sync import clients

from sdlc_dispatcher.config import DispatchError, load_project
from sdlc_dispatcher.privacy import model_report
from sdlc_dispatcher.release_controller import Controller, Deferred, GitHubRelease, ReviewChanged
from sdlc_dispatcher.release_store import TERMINAL, latest, setup
from sdlc_dispatcher.review_cards import original_proof
from sdlc_dispatcher.review_gate import sha, validate_receipt
from sdlc_dispatcher.review_packet import prepare_packet
from sdlc_dispatcher.reviewer import run_review
from sdlc_dispatcher.store import Store
from sdlc_dispatcher.worker import verify_project
from sdlc_dispatcher.workspace import File, changes, export

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".dispatcher"


def review_problem(review):
    details = ["Fresh Astra review: " + review["verdict"] + ". " + review["summary"]]
    for finding in review["findings"]:
        if finding["blocking"]:
            details.append(
                f"{finding['severity']} at {finding['path']}:{finding['line']}: {finding['requested_change']}"
            )
    return "\n".join(details)[:6000]


class Evidence:
    def __init__(self, store, project, github):
        setup(store)
        self.store, self.project, self.github = store, project, github
        self.root = STATE / "releases"
        self.repository = self.root / "source"

    def git(self, *args):
        try:
            return subprocess.check_output(
                ["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.repository), *args],
                stderr=subprocess.DEVNULL,
                timeout=90,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DispatchError("Could not read isolated release source") from exc

    def preflight(self):
        from sdlc_dispatcher import digitalocean

        if digitalocean.enabled(self.project):
            digitalocean.preflight()
            return
        from storage import storage_client

        try:
            storage_client().remote("preflight")
        except Exception as exc:
            from storage import StorageError

            if isinstance(exc, StorageError) and "maintenance is busy" in str(exc):
                raise Deferred(
                    "Storage maintenance is running; release preflight will retry"
                ) from None

            raise DispatchError(
                str(exc)
                if isinstance(exc, StorageError)
                else "Production storage preflight could not be verified"
            ) from None

    def fetch(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.repository.exists():
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--bare",
                    "--shared",
                    self.project.repository,
                    str(self.repository),
                ],
                check=True,
                capture_output=True,
                timeout=90,
            )
        self.git(
            "fetch",
            "--no-tags",
            "https://github.com/" + self.project.github_repository + ".git",
            "+refs/heads/*:refs/heads/*",
        )

    def files(self, ref):
        return export(replace(self.project, repository=str(self.repository), base_ref=ref))[1]

    def patch_id(self, base, head):
        diff = self.git("diff", "--no-ext-diff", base, head)
        r = subprocess.run(
            ["git", "patch-id", "--stable"], input=diff, capture_output=True, check=True, timeout=30
        )
        return r.stdout.split()[0] if r.stdout else b""

    def release_cancelled(self, job):
        request = latest(self.store, job)
        return bool(request and request["status"] not in TERMINAL and request["cancel_requested"])

    def ensure(self, job, pr, approved_head=None):
        artifact, original, review = original_proof(self.store, self.project, job)
        head = pr["head"]["sha"]
        folder = self.root / job["id"] / head
        self.fetch()
        base = pr["base"]["sha"]
        if approved_head:
            with self.store.connect() as db:
                card = db.execute(
                    "SELECT manifest FROM review_cards WHERE job=? AND head=?",
                    (job["id"], approved_head),
                ).fetchone()
            if not card:
                raise DispatchError("Approved review evidence is missing")
            old_folder = Path(json.loads(card[0])["folder"])
            old_base = json.loads((old_folder / "input/packet.json").read_text())["base_sha"]
            if self.patch_id(old_base, approved_head) != self.patch_id(base, head):
                raise ReviewChanged(
                    "The approved fix changed during integration; fresh review evidence and human approval are required"
                )
        if (folder / "accepted.json").exists():
            accepted = json.loads((folder / "accepted.json").read_text())
            review_folder = Path(accepted["folder"])
            if sha((review_folder / "metadata.json").read_bytes()) != accepted["metadata_digest"]:
                raise DispatchError("Cached release review changed")
            manifest = json.loads(Path(accepted["artifact"]).read_text())
            review = validate_receipt(
                review_folder, self.project, manifest, sha(Path(accepted["artifact"]).read_bytes())
            )
            if review["verdict"] != "pass":
                raise DispatchError("Updated release review did not pass")
            return review_folder, review
        candidate = self.files(artifact["base_sha"])
        for item in artifact["changes"]:
            if item["after"] is None:
                candidate.pop(item["path"], None)
            else:
                candidate[item["path"]] = File(item["after"].encode(), item["mode"] == "100755")
        actual = self.files(head)
        parents = self.git("show", "-s", "--format=%P", head).decode().strip().split()
        if candidate == actual and parents == [artifact["base_sha"]]:
            # The original independent review is a proof of this full candidate tree.
            return original, review
        base = pr["base"]["sha"]
        before = self.files(base)
        changed = changes(self.project, before, actual)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        artifact_path = folder / "artifact.json"
        if not artifact_path.exists():
            # Candidate checks run offline with no release/provider credentials.
            validation = verify_project(
                replace(self.project, repository=str(self.repository), base_ref=head),
                folder / "checks",
            )
            if validation["status"] != "ready":
                raise DispatchError("Integrated candidate failed independent offline checks")
            artifact_path.write_text(
                json.dumps(
                    {
                        **artifact,
                        "base_sha": base,
                        "head_sha": head,
                        "changes": changed,
                        "checks": validation["checks"],
                    },
                    indent=2,
                )
            )
        manifest = json.loads(artifact_path.read_text())
        if manifest["head_sha"] != head:
            raise DispatchError("Release artifact commit mismatch")
        browser = folder / "browser"
        if not (browser / "evidence.json").exists():
            if browser.exists():
                shutil.rmtree(browser)
            evidence.collect(artifact_path, browser, source_repository=self.repository)
        proof = json.loads((browser / "evidence.json").read_text())
        packet = folder / "packet"
        if not packet.exists():
            prepare_packet(
                project=self.project,
                artifact=artifact_path,
                expected_digest=sha(artifact_path.read_bytes()),
                issue={
                    "id": job["external_id"],
                    "revision": job["revision"],
                    **model_report(json.loads(job["report"])),
                },
                policy=Path(self.project.review_policy),
                evidence={
                    n: browser / n
                    for n in proof["files"]
                    if Path(n).suffix.lower() not in {".png", ".webm"}
                },
                images=[],
                limitations=[
                    *proof["limitations"],
                    "Screenshots are optional and omitted; visual appearance was not independently inspected.",
                ],
                destination=packet,
                context_paths=tuple(self.project.review_context_paths),
                source_repository=self.repository,
            )
        reviews = sorted(folder.glob("review-*"))
        valid = None
        for r in reviews:
            if (r / "metadata.json").exists() and json.loads((r / "metadata.json").read_text()).get(
                "status"
            ) == "completed":
                try:
                    result = validate_receipt(
                        r, self.project, manifest, sha(artifact_path.read_bytes())
                    )
                    if result["verdict"] == "pass":
                        valid, review = r, result
                        break
                    raise DispatchError(review_problem(result))
                except (OSError, KeyError, ValueError):
                    pass
        if not valid:
            if len(reviews) >= 3:
                raise DispatchError("Integrated Astra review exhausted three attempts")
            with (Path(self.project.review_auth_home) / "review.lock").open("a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise Deferred("Astra is reviewing another candidate") from None
            valid = folder / ("review-" + str(len(reviews) + 1))
            run_review(
                packet=packet,
                output=valid,
                auth_home=Path(self.project.review_auth_home),
                image=self.project.review_image,
                timeout=900,
                cancelled=lambda: self.store.paused()
                or bool(self.store.get(job["id"])["cancel"])
                or self.release_cancelled(job["id"]),
            )
            review = validate_receipt(
                valid, self.project, manifest, sha(artifact_path.read_bytes())
            )
            if review["verdict"] != "pass":
                raise DispatchError(review_problem(review))
        # All reviewed sources come from the exact frozen Git head; no reused status.
        if self.github.call("GET", "/pulls/" + str(pr["number"]))["head"]["sha"] != head:
            raise DispatchError("PR changed during independent review")
        self.github.call(
            "POST",
            "/statuses/" + head,
            {
                "state": "success",
                "context": "dispatcher/astra",
                "description": "Astra High passed this integrated commit",
                "target_url": pr["html_url"],
            },
        )
        (folder / "accepted.json").write_text(
            json.dumps(
                {
                    "folder": str(valid),
                    "artifact": str(artifact_path),
                    "metadata_digest": sha((valid / "metadata.json").read_bytes()),
                }
            )
        )
        return valid, review


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    lock = (STATE / "release-controller.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Release controller is already running") from None
    store = Store(STATE / "queue.db")
    linear, _ = clients()
    while True:
        project = load_project(Path(__file__).with_name("project.toml"))
        github = GitHubRelease(project.github_repository)
        controller = Controller(store, project, linear, github, Evidence(store, project, github))
        try:
            controller.tick()
        except (DispatchError, OSError, ValueError, KeyError, TypeError):
            print(
                json.dumps(
                    {
                        "release_controller": "retrying",
                        "detail": "Controller operation could not be confirmed; inspect durable release state",
                    }
                ),
                flush=True,
            )
            if not args.watch:
                raise
        if not args.watch:
            return
        time.sleep(15)


if __name__ == "__main__":
    main()
