"""Construct review data from a verified artifact without executing candidate code."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from .config import DispatchError
from .workspace import File, changes, export, materialize, review_diff, valid_path


def prepare_packet(
    *,
    project,
    artifact: Path,
    expected_digest: str,
    issue: dict,
    policy: Path,
    evidence: dict[str, Path],
    images: list[str],
    limitations: list[str],
    destination: Path,
    context_paths: tuple[str, ...] = (),
):
    raw = artifact.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_digest:
        raise DispatchError("Candidate differs from the independently verified artifact")
    manifest = json.loads(raw)
    if manifest["project"] != project.id or manifest["policy"] != project.fingerprint:
        raise DispatchError("Candidate project or policy changed")
    if not manifest.get("checks") or any(check["exit_code"] for check in manifest["checks"]):
        raise DispatchError("Candidate lacks passing independent checks")
    sha, base = export(replace(project, base_ref=manifest["base_sha"]))
    candidate = dict(base)
    seen = set()
    for item in manifest["changes"]:
        name = item["path"]
        valid_path(name)
        if name in seen:
            raise DispatchError("Duplicate candidate path")
        seen.add(name)
        original = base.get(name)
        if item["before"] != (original.data.decode() if original else None):
            raise DispatchError("Candidate does not match its recorded base")
        if item["after"] is None:
            candidate.pop(name, None)
        else:
            candidate[name] = File(item["after"].encode(), item["mode"] == "100755")
    checked = changes(project, base, candidate)
    if checked != manifest["changes"]:
        raise DispatchError("Candidate failed current path/size/content validation")
    # Keep repository-controlled assistant instructions/config and credentials out
    # of the mounted snapshot, even though the review prompt treats code as data.
    forbidden = {
        "AGENTS.md",
        "CLAUDE.md",
        ".codex",
        ".agents",
        ".cursor",
        ".claude",
        ".env",
        "auth.json",
        "credentials.json",
    }
    selected = seen | set(context_paths)
    for name in selected:
        valid_path(name)
        if name not in candidate and name not in seen:
            raise DispatchError("Requested review context is missing")
    files = {
        "source/" + name: item
        for name, item in candidate.items()
        if name in selected
        and not any(part in forbidden or part.startswith(".env.") for part in Path(name).parts)
    }
    for name, path in evidence.items():
        valid_path(name)
        if "/" in name:
            raise DispatchError("Evidence names must be flat")
        files["evidence/" + name] = File(path.read_bytes())
    if any(name not in evidence for name in images):
        raise DispatchError("Review image evidence is missing")
    packet = {
        "kind": "candidate_review",
        "project": project.id,
        "issue": issue,
        "base_sha": sha,
        "artifact_digest": expected_digest,
        "policy_fingerprint": project.fingerprint,
        "changed_files": sorted(seen),
        "context_files": sorted(set(context_paths)),
        "checks": manifest["checks"],
        "regression_on_base": manifest.get("regression_on_base", []),
        "images": images,
        "evidence": sorted(evidence),
        "limitations": limitations,
    }
    files["packet.json"] = File(json.dumps(packet, indent=2).encode())
    files["policy.md"] = File(policy.read_bytes())
    files["review.diff"] = File(
        "".join(
            line
            for item in checked
            for line in review_diff(item["path"], item["before"], item["after"])
        ).encode()
    )
    materialize(destination, files)
    return packet
