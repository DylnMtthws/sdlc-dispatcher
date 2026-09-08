"""Export committed files, reject unsafe output, and construct review artifacts."""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .config import DispatchError, Project

# Enforced in addition to each project's policy. Case-insensitive on every host.
PROTECTED = [
    ".git",
    ".git/**",
    ".github/**",
    ".gitmodules",
    ".gitattributes",
    ".gitconfig",
    ".codex/**",
    ".cursor/**",
    ".claude/**",
    ".agents/**",
    "AGENTS.md",
    "**/AGENTS.md",
    "CLAUDE.md",
    "**/CLAUDE.md",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "Dockerfile*",
    "**/Dockerfile*",
    "compose*.yml",
    "compose*.yaml",
    "deploy/**",
    "fly*.toml",
    "pyproject.toml",
    "package.json",
    "*lock*",
    "requirements*.txt",
    "setup.py",
    "setup.cfg",
    "conftest.py",
    "**/conftest.py",
    "pytest.ini",
    "tox.ini",
    "dispatcher.toml",
]


@dataclass(frozen=True)
class File:
    data: bytes
    executable: bool = False


def valid_path(name: str):
    p = PurePosixPath(name)
    if (
        not name
        or p.is_absolute()
        or str(p) != name
        or ".." in p.parts
        or any(c in name for c in "\\\x00\n\r\t")
        or any(part.casefold() == ".git" for part in p.parts)
    ):
        raise DispatchError("Unsafe repository path")


def git(repo: Path, *args: str) -> bytes:
    # No inherited Git config, credentials, SSH agent, hooks or replacement objects.
    env = {
        "PATH": os.defpath,
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
    }
    try:
        return subprocess.run(
            ["/usr/bin/git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
            env=env,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise DispatchError("Could not read the configured Git source") from exc


def export(project: Project, *, refresh=False) -> tuple[str, dict[str, File]]:
    repo = Path(project.repository)
    ref = project.base_ref
    if refresh and project.github_repository:
        # Fetch a fixed registered GitHub branch into a private ref, leaving the
        # developer's checkout and local branches untouched. Never run pull/hooks.
        # Anonymous access is intentional; private sources need a read-only adapter.
        ref = "refs/sdlc-dispatcher/" + project.id
        git(
            repo,
            "-c",
            "credential.helper=",
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            "https://github.com/" + project.github_repository + ".git",
            "+refs/heads/" + project.base_ref + ":" + ref,
        )
    sha = git(repo, "rev-parse", "--verify", ref + "^{commit}").decode().strip()
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise DispatchError("Expected a SHA-1 Git commit")
    tree = git(repo, "ls-tree", "-rz", sha)
    for entry in tree.split(b"\0"):
        if entry and entry.split(b" ", 1)[0] not in {b"100644", b"100755"}:
            raise DispatchError("Source must not contain symlinks or submodules")
    raw = git(repo, "archive", "--format=tar", sha)
    if len(raw) > project.max_snapshot_bytes * 2 + 1_000_000:
        raise DispatchError("Repository archive exceeds limit")
    files: dict[str, File] = {}
    total = 0
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for member in archive:
            if member.isdir():
                continue
            valid_path(member.name)
            if not member.isfile():
                raise DispatchError("Archive contains unsupported entries")
            if any(
                part in {".env", "auth.json", "credentials.json"}
                for part in PurePosixPath(member.name).parts
            ):
                raise DispatchError(
                    "Remove tracked credential files before registering this repository"
                )
            total += member.size
            if total > project.max_snapshot_bytes or len(files) >= 30_000:
                raise DispatchError("Source snapshot exceeds limit")
            stream = archive.extractfile(member)
            if stream is None:
                raise DispatchError("Unreadable archive entry")
            files[member.name] = File(stream.read(), bool(member.mode & 0o111))
    return sha, files


def materialize(root: Path, files: dict[str, File]):
    root.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, item in files.items():
        valid_path(name)
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item.data)
        path.chmod(0o755 if item.executable else 0o644)


def snapshot(root: Path, maximum: int) -> dict[str, File]:
    files = {}
    total = 0
    count = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in [*dirs, *names]:
            count += 1
            if count > 40_000:
                raise DispatchError("Worker output contains too many entries")
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            valid_path(relative)
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not (
                stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
            ):
                raise DispatchError("Worker output contains a symlink or special file")
            if stat.S_ISREG(info.st_mode):
                if info.st_nlink != 1:
                    raise DispatchError("Worker output contains hard links")
                total += info.st_size
                if total > maximum:
                    raise DispatchError("Worker snapshot exceeds limit")
                files[relative] = File(path.read_bytes(), bool(info.st_mode & 0o111))
    return files


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path.casefold(), p.casefold()) for p in patterns)


def changes(
    project: Project, before: dict[str, File], after: dict[str, File], secrets=()
) -> list[dict]:
    result = []
    total = 0
    for name in sorted(before.keys() | after.keys()):
        valid_path(name)
        old, new = before.get(name), after.get(name)
        if old == new:
            continue
        if not matches(name, project.allowed_paths) or matches(
            name, [*PROTECTED, *project.protected_paths]
        ):
            raise DispatchError("Change touches a protected or non-allowlisted path")
        # Existing tests cannot be removed/rewritten to turn failures green. New tests are allowed.
        if old and is_test(name):
            raise DispatchError("Existing test changes require human implementation")
        try:
            previous = old.data.decode("utf-8") if old else None
            content = new.data.decode("utf-8") if new else None
        except UnicodeDecodeError as exc:
            raise DispatchError(
                "Binary changes require human review outside this pipeline"
            ) from exc
        if content is not None:
            if "\0" in content or re.search(
                r"-----BEGIN .*PRIVATE KEY-----|gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{30,}",
                content,
            ):
                raise DispatchError("Output may contain a credential or binary content")
            if any(secret and secret in content for secret in secrets):
                raise DispatchError("Output contains a worker credential")
        # Limit the review diff, not both complete source files. Small repairs
        # in large files must remain possible; snapshot limits bound full files.
        total += sum(len(line.encode("utf-8")) for line in review_diff(name, previous, content))
        if total > project.max_patch_bytes or len(result) >= project.max_changed_files:
            raise DispatchError("Change exceeds configured review limits")
        result.append(
            {
                "path": name,
                "before": previous,
                "after": content,
                "mode": "100755" if new and new.executable else "100644",
            }
        )
    if not result:
        raise DispatchError("Agent produced no eligible changes")
    return result


def is_test(name: str) -> bool:
    return matches(
        name,
        [
            "tests/**",
            "test/**",
            "test_*.py",
            "**/test_*.py",
            "*.test.*",
            "**/*.test.*",
            "*.spec.*",
            "**/*.spec.*",
        ],
    )


def review_diff(path: str, before: str | None, after: str | None):
    return difflib.unified_diff(
        (before or "").splitlines(keepends=True),
        (after or "").splitlines(keepends=True),
        fromfile=("a/" + path if before is not None else "/dev/null"),
        tofile=("b/" + path if after is not None else "/dev/null"),
    )


def write_artifact(directory: Path, manifest: dict) -> tuple[str, str]:
    raw = json.dumps(manifest, sort_keys=True, indent=2).encode()
    path = directory / "result.json"
    path.write_bytes(raw)
    path.chmod(0o600)
    patch = []
    for item in manifest["changes"]:
        patch.extend(review_diff(item["path"], item["before"], item["after"]))
    (directory / "review.diff").write_text("".join(patch))
    return str(path), hashlib.sha256(raw).hexdigest()
