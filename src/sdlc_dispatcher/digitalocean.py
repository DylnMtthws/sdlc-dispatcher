"""Trusted Mac operator bridge for Deck Lab's DigitalOcean deployment."""

import json
import os
import subprocess
from pathlib import Path

from .config import DispatchError

ROOT = (
    Path(
        os.environ.get(
            "DISPATCHER_INFRA_ROOT", Path(__file__).resolve().parents[3] / "decklab-infra"
        )
    )
    .expanduser()
    .resolve()
)


def enabled(project=None):
    if project and project.github_repository != "DylnMtthws/deck-lab":
        return False
    path = ROOT / ".private/production-active.json"
    try:
        active = json.loads(path.read_text())
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        raise DispatchError("Cannot read the production provider activation record") from None
    if not isinstance(active, dict):
        raise DispatchError("Invalid production provider activation record")
    return active.get("provider") == "digitalocean"


def operator(script, *args, timeout=1500):
    try:
        r = subprocess.run(
            [
                str(ROOT / ".private/ops-venv311/bin/python"),
                str(ROOT / "scripts" / script),
                *map(str, args),
            ],
            cwd=ROOT,
            capture_output=True,
            check=True,
            timeout=timeout,
        )
        return json.loads(r.stdout.splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        raise DispatchError(
            "DigitalOcean operation could not be confirmed; inspect the durable host receipt before retrying"
        ) from None


def status():
    return operator("ops_status.py", timeout=60)


def preflight():
    result = status()
    if not result["health"]["healthy"]:
        raise DispatchError("DigitalOcean health or backup readiness check failed")
    return result


def deploy(run_id, expected_sha):
    before = preflight()["health"]["build_sha"]
    folder = ROOT / ".private/releases" / str(int(run_id))
    prepared = operator("release_prepare.py", "--run-id", int(run_id), "--output", folder)
    if prepared["sha"] != expected_sha:
        raise DispatchError("Prepared DigitalOcean release differs from the approved merge")
    return operator("release_deploy.py", "--prepared", folder, "--expected-live-sha", before)
