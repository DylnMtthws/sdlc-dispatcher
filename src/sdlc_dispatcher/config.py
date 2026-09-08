"""Operator-owned project registrations; never load policy from agent output."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .cursor_entrypoint import parse_model


class DispatchError(Exception):
    """A safe, operator-facing error (must not contain credentials/report bodies)."""


@dataclass(frozen=True)
class Project:
    id: str
    repository: str
    base_ref: str
    image: str
    checks: list[list[str]]
    allowed_paths: list[str]
    protected_paths: list[str] = field(default_factory=list)
    regression_checks: list[list[str]] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    github_repository: str = ""
    linear_team_id: str = ""
    linear_project_id: str = ""
    linear_ready_state_id: str = ""
    linear_actor_ids: list[str] = field(default_factory=list)
    required_labels: list[str] = field(default_factory=lambda: ["user-feedback", "bug"])
    automatic_intake: bool = False
    max_daily_runs: int = 3
    max_attempts: int = 2
    timeout_seconds: int = 900
    max_changed_files: int = 12
    max_patch_bytes: int = 200_000
    max_snapshot_bytes: int = 100_000_000
    memory_mb: int = 2048
    model: str = ""
    engine: str = "codex"
    review_required: bool = False
    review_policy: str = ""
    review_image: str = ""
    review_auth_home: str = ""
    review_context_paths: list[str] = field(default_factory=list)
    review_evidence_command: list[str] = field(default_factory=list)
    max_review_repairs: int = 2
    review_timeout_seconds: int = 900
    automatic_publication: bool = False
    publish_command: list[str] = field(default_factory=list)

    def __post_init__(self):
        if (
            type(self.automatic_publication) is not bool
            or not isinstance(self.publish_command, list)
            or any(not isinstance(v, str) or not v for v in self.publish_command)
        ):
            raise DispatchError("Invalid publication configuration")
        if self.automatic_publication and (not self.review_required or not self.publish_command):
            raise DispatchError(
                "Automatic publication requires independent review and a trusted publisher command"
            )
        if (
            type(self.review_required) is not bool
            or type(self.max_review_repairs) is not int
            or not 0 <= self.max_review_repairs <= 2
        ):
            raise DispatchError(
                "Review configuration requires a boolean gate and 0–2 repair rounds"
            )
        if (
            type(self.review_timeout_seconds) is not int
            or not 10 <= self.review_timeout_seconds <= 900
        ):
            raise DispatchError("Reviewer timeout must be 10–900 seconds")
        for values in (self.review_context_paths, self.review_evidence_command):
            if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
                raise DispatchError("Review context and evidence command must be string arrays")
        for value in (self.review_policy, self.review_image, self.review_auth_home):
            if not isinstance(value, str):
                raise DispatchError("Review policy, image and auth home must be strings")
        if self.review_required and not all(
            (self.review_policy, self.review_image, self.review_auth_home)
        ):
            raise DispatchError("Required review needs policy, image and dedicated auth home")
        for name in (
            "id",
            "repository",
            "base_ref",
            "image",
            "github_repository",
            "linear_team_id",
            "linear_project_id",
            "linear_ready_state_id",
            "model",
            "engine",
        ):
            if not isinstance(getattr(self, name), str):
                raise DispatchError(f"{name} must be a string")
        if self.engine not in {"codex", "cursor"}:
            raise DispatchError("Engine must be codex or cursor")
        if self.engine == "cursor":
            try:
                parse_model(self.model)
            except RuntimeError as exc:
                raise DispatchError(str(exc)) from exc
        for name in (
            "allowed_paths",
            "protected_paths",
            "required_labels",
            "linear_actor_ids",
        ):
            if not isinstance(getattr(self, name), list) or any(
                not isinstance(value, str) or not value for value in getattr(self, name)
            ):
                raise DispatchError(f"{name} must be an array of nonempty strings")
        if (
            not isinstance(self.environment, dict)
            or not isinstance(self.checks, list)
            or not isinstance(self.regression_checks, list)
        ):
            raise DispatchError("Invalid environment or checks configuration")
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", self.id):
            raise DispatchError("Invalid project ID")
        if not Path(self.repository).is_absolute():
            raise DispatchError("Repository must resolve to an absolute path")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", self.base_ref):
            raise DispatchError("Invalid base reference")
        if (
            ".." in self.base_ref
            or "//" in self.base_ref
            or self.base_ref.endswith(("/", ".", ".lock"))
        ):
            raise DispatchError("Invalid base reference")
        if not self.image or self.image.startswith("-") or any(c.isspace() for c in self.image):
            raise DispatchError("Invalid image reference")
        if not self.checks or any(
            not isinstance(cmd, list)
            or not cmd
            or any(not isinstance(s, str) or not s for s in cmd)
            for cmd in [*self.checks, *self.regression_checks]
        ):
            raise DispatchError("Checks must be nonempty argv arrays")
        if not self.allowed_paths or any(
            not isinstance(p, str) or p.startswith("/") or ".." in p.split("/") or not p
            for p in [*self.allowed_paths, *self.protected_paths]
        ):
            raise DispatchError("Declare relative allowed and protected path patterns")
        for name, value in self.environment.items():
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or not isinstance(value, str):
                raise DispatchError("Environment must contain string settings")
            if name in {
                "HOME",
                "PATH",
                "CODEX_HOME",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "LD_PRELOAD",
                "ALL_PROXY",
                "NO_PROXY",
                "CURSOR_API_KEY",
                "CODEX_API_KEY",
                "CURSOR_API_ENDPOINT",
                "CURSOR_CONFIG_DIR",
                "CURSOR_DATA_DIR",
                "AGENT_CLI_CREDENTIAL_STORE",
                "NODE_OPTIONS",
            }:
                raise DispatchError("Reserved environment setting")
        if self.github_repository and not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.github_repository
        ):
            raise DispatchError("GitHub repository must be owner/name")
        for name in (
            "max_daily_runs",
            "max_attempts",
            "timeout_seconds",
            "max_changed_files",
            "max_patch_bytes",
            "max_snapshot_bytes",
            "memory_mb",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise DispatchError(f"{name} must be a positive integer")
        if type(self.automatic_intake) is not bool:
            raise DispatchError("automatic_intake must be a boolean")
        if self.automatic_intake and not all(
            (
                self.linear_team_id,
                self.linear_project_id,
                self.linear_ready_state_id,
                self.linear_actor_ids,
            )
        ):
            raise DispatchError(
                "Automatic intake requires team, project, ready state and trusted actors"
            )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(asdict(self), sort_keys=True).encode()).hexdigest()


def load_project(path: Path) -> Project:
    try:
        data = tomllib.loads(path.read_text())
        repo = Path(data["repository"]).expanduser()
        data["repository"] = str(
            (path.parent / repo).resolve() if not repo.is_absolute() else repo.resolve()
        )
        for key in ("review_policy", "review_auth_home"):
            if data.get(key):
                value = Path(data[key]).expanduser()
                data[key] = str(
                    (path.parent / value).resolve() if not value.is_absolute() else value
                )
        return Project(**data)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise DispatchError("Invalid project configuration") from exc
