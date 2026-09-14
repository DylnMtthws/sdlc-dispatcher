"""Controller-owned evidence assembly and publication checks. No agent authority."""

import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from .activity import mark, monitor
from .config import DispatchError
from .privacy import model_report
from .review_contract import validate_review
from .review_packet import prepare_packet
from .reviewer import run_review
from .reviewer_auth import MODEL
from .workspace import snapshot, valid_path


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def validate_receipt(folder, project, artifact, artifact_digest):
    metadata = json.loads((folder / "metadata.json").read_text())
    if any(
        metadata.get(key) != value
        for key, value in {
            "status": "completed",
            "cleanup": "confirmed",
            "kind": "candidate_review",
            "model_requested": MODEL,
            "model_reported": MODEL,
            "reasoning_effort": "high",
        }.items()
    ):
        raise DispatchError("Review did not complete with the required model and cleanup")
    frozen = folder / "input"
    files = snapshot(frozen, 30_000_000)
    hashes = {name: sha(item.data) for name, item in sorted(files.items())}
    if hashes != metadata.get("input_hashes") or sha(
        json.dumps(hashes, sort_keys=True).encode()
    ) != metadata.get("input_digest"):
        raise DispatchError("Review inputs changed after completion")
    packet = json.loads(files["packet.json"].data)
    if any(
        packet.get(key) != value
        for key, value in {
            "artifact_digest": artifact_digest,
            "base_sha": artifact["base_sha"],
            "project": project.id,
            "policy_fingerprint": project.fingerprint,
        }.items()
    ):
        raise DispatchError("Review is for a different candidate, base or policy")
    if files["policy.md"].data != Path(project.review_policy).read_bytes():
        raise DispatchError("Review policy changed; review again")
    for item in artifact["changes"]:
        candidate = files.get("source/" + item["path"])
        if (candidate.data.decode() if candidate else None) != item["after"]:
            raise DispatchError("Reviewed source differs from candidate")
    raw = (folder / "review.json").read_bytes()
    if sha(raw) != metadata.get("review_digest"):
        raise DispatchError("Review result changed after completion")
    review = validate_review(json.loads(raw), frozen / "source")
    if review["verdict"] != metadata.get("verdict"):
        raise DispatchError("Review verdict metadata mismatch")
    return review


def review_candidate(store, project, job, artifact_path, artifact_digest, folder):
    """Called after each independently verified candidate, including repairs."""
    cancelled = monitor(store, job)
    artifact = json.loads(artifact_path.read_text())
    evidence = {p.name: p for p in folder.glob("*.log") if p.name != "agent.log"}
    images, limitations = [], []
    if project.review_evidence_command:
        mark(store, job, "browser")
        output = folder / "evidence"
        output.mkdir(mode=0o700)
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "TMPDIR"}}
        remaining = int(job["deadline"] - time.time())
        if remaining < 10:
            raise DispatchError("Job deadline exhausted before browser evidence")
        with (folder / "evidence-adapter.log").open("wb") as log:
            try:
                process = subprocess.Popen(
                    [
                        *project.review_evidence_command,
                        "--artifact",
                        str(artifact_path),
                        "--output",
                        str(output),
                    ],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                deadline = time.monotonic() + min(remaining, 300)
                try:
                    while process.poll() is None:
                        if cancelled() or time.monotonic() >= deadline:
                            raise DispatchError("Browser preparation cancelled or timed out")
                        time.sleep(0.25)
                    if process.returncode:
                        raise DispatchError("Independent browser/preview preparation failed")
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=60)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
            except (OSError, subprocess.SubprocessError) as exc:
                raise DispatchError("Independent browser/preview preparation failed") from exc
        manifest = json.loads((output / "evidence.json").read_text())
        for name in manifest["files"]:
            valid_path(name)
            if "/" in name or (output / name).is_symlink():
                raise DispatchError("Invalid browser evidence path")
            if Path(name).suffix.lower() in {".png", ".webm"}:
                continue
            evidence[name] = output / name
        limitations = [
            *manifest["limitations"],
            "Screenshots are optional and omitted; visual appearance was not independently inspected.",
        ]
        receipt = manifest.get("preview")
        if receipt:
            validate_preview(receipt, artifact_digest)
            store.record_preview(job["id"], artifact_digest, receipt)
    else:
        limitations.append("No browser evidence or preview adapter is configured.")
    packet = folder / "review-packet"
    report = model_report(json.loads(job["report"]))
    prepare_packet(
        project=project,
        artifact=artifact_path,
        expected_digest=artifact_digest,
        issue={"id": job["external_id"], "revision": job["revision"], **report},
        policy=Path(project.review_policy),
        evidence=evidence,
        images=images,
        limitations=limitations,
        destination=packet,
        context_paths=tuple(project.review_context_paths),
    )
    remaining = int(job["deadline"] - time.time())
    if remaining < 10 or store.cancelled(job["id"]):
        raise DispatchError("Job cancelled or deadline exhausted before review")
    review_folder = folder / "review"
    mark(store, job, "astra")
    run_review(
        packet=packet,
        output=review_folder,
        auth_home=Path(project.review_auth_home),
        image=project.review_image,
        timeout=min(project.review_timeout_seconds, remaining),
        cancelled=cancelled,
    )
    review = validate_receipt(review_folder, project, artifact, artifact_digest)
    preview = store.preview(job["id"], artifact_digest)
    if preview and preview.get("output") == str((folder / "evidence").resolve()):
        public = folder / "evidence" / "public"
        if public.is_dir() and not public.is_symlink():
            (public / "review.json").write_text(json.dumps(review, indent=2) + "\n")
    metadata = review_folder / "metadata.json"
    store.record_review(
        job["id"], artifact_digest, metadata, sha(metadata.read_bytes()), review["verdict"]
    )
    return review


def validate_preview(receipt, artifact_digest):
    url = urlsplit(receipt.get("url", ""))
    if (
        receipt.get("artifact_digest") != artifact_digest
        or receipt.get("expires_at", 0) <= time.time()
    ):
        raise DispatchError("Preview is expired or belongs to another candidate")
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or any(c in receipt["url"] for c in "\r\n<>`[]()")
    ):
        raise DispatchError("Preview must have a valid private HTTPS URL")


def publication_review(store, project, job, artifact):
    if not project.review_required:
        return None
    receipt = store.review(job["id"], job["artifact_digest"])
    if not receipt:
        raise DispatchError("Independent review required before publication")
    path = Path(receipt["metadata_path"])
    if sha(path.read_bytes()) != receipt["metadata_digest"]:
        raise DispatchError("Review metadata changed after acceptance")
    review = validate_receipt(path.parent, project, artifact, job["artifact_digest"])
    if review["verdict"] != "pass" or receipt["verdict"] != "pass":
        raise DispatchError("Review findings prevent publication")
    packet = json.loads((path.parent / "input/packet.json").read_text())
    if packet["issue"].get("revision") != job["revision"]:
        raise DispatchError("Issue changed after review")
    preview = store.preview(job["id"], job["artifact_digest"])
    if project.review_evidence_command and not preview:
        raise DispatchError("A current candidate preview is required for publication")
    if preview:
        validate_preview(preview, job["artifact_digest"])
    return {
        "review": review,
        "preview": preview,
        "input_digest": json.loads(path.read_text())["input_digest"],
    }
