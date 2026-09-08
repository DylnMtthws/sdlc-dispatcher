"""Separate publisher process; model workers never receive its GitHub token."""

import argparse
import os
from pathlib import Path

from receiver import read_secret

from sdlc_dispatcher.cli import main

ROOT = Path(__file__).resolve().parents[2]


def invoke():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job")
    parser.add_argument("--reconcile", action="store_true")
    args = parser.parse_args()
    state = ROOT / ".dispatcher"
    token = read_secret(state / "secrets/github-publisher-token")
    if not token:
        raise SystemExit(
            "Save the repo-scoped GitHub token with save_credential.py github-publisher"
        )
    os.environ["DISPATCHER_GITHUB_TOKEN"] = token
    linear = read_secret(state / "secrets/linear-read-api-key")
    if linear:
        os.environ["DISPATCHER_LINEAR_API_KEY"] = linear
    command = [
        "--state",
        str(state),
        "publish",
        "--project",
        str(Path(__file__).with_name("project.toml")),
        args.job,
    ]
    if args.reconcile:
        command.append("--reconcile")
    return main(command)


if __name__ == "__main__":
    raise SystemExit(invoke())
