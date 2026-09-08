"""Load private pilot credentials into a single trusted worker invocation."""

import argparse
import os
from pathlib import Path

from receiver import read_secret

from sdlc_dispatcher.cli import main

ROOT = Path(__file__).resolve().parents[2]


def invoke():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["check-agent", "work"])
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    state = ROOT / ".dispatcher"
    cursor = read_secret(state / "secrets" / "cursor-api-key")
    if not cursor:
        raise SystemExit("Save the Cursor key with save_credential.py cursor first")
    os.environ["DISPATCHER_CURSOR_API_KEY"] = cursor
    if args.command == "work":
        linear = read_secret(state / "secrets" / "linear-read-api-key")
        if not linear:
            raise SystemExit("Save the Linear read key with save_credential.py linear-read first")
        os.environ["DISPATCHER_LINEAR_API_KEY"] = linear
        if not read_secret(state / "secrets" / "github-publisher-token"):
            raise SystemExit(
                "Save the repo-scoped GitHub publisher token before enabling automatic work"
            )
    command = [
        "--state",
        str(state),
        args.command,
        "--project",
        str(Path(__file__).with_name("project.toml")),
    ]
    if args.command == "work":
        command.append("--allow-live-agent")
        if args.watch:
            command.append("--watch")
    return main(command)


if __name__ == "__main__":
    raise SystemExit(invoke())
