"""Install the local worker and expired-preview collector as login services."""

import os
import plistlib
import subprocess
import time
from pathlib import Path

from receiver import read_secret

ROOT = Path(__file__).resolve().parents[2]
INTEGRATION = Path(__file__).resolve().parent


def install(only=None):
    os.umask(0o077)
    state = ROOT / ".dispatcher"
    directory = Path.home() / "Library/LaunchAgents"
    directory.mkdir(exist_ok=True)
    from sdlc_dispatcher.config import load_project

    project = load_project(INTEGRATION / "project.toml")
    if only in {"worker", "linear-sync", "release-controller"} and not project.automatic_intake:
        raise SystemExit("Automatic pipeline is disabled; use the owner-initiated T3 workflow")
    services = {
        "preview-cleanup": (
            [str(INTEGRATION / "evidence.py"), "--cleanup-expired"],
            {"StartInterval": 300},
        ),
    }
    if (state / "storage-config.json").exists() and read_secret(state / "secrets/storage.json"):
        services["storage-monitor"] = ([str(INTEGRATION / "storage.py")], {"StartInterval": 60})
    if project.automatic_intake and read_secret(state / "secrets/linear-status-api-key"):
        services["linear-sync"] = (
            [str(INTEGRATION / "sync.py"), "--watch"],
            {"KeepAlive": True, "ThrottleInterval": 60},
        )
        from sdlc_dispatcher.config import load_project
        from sdlc_dispatcher.release_store import settings
        from sdlc_dispatcher.store import Store

        project = load_project(INTEGRATION / "project.toml")
        if settings(Store(state / "queue.db"), project).get("mode", "off") != "off":
            services["release-controller"] = (
                [str(INTEGRATION / "release.py"), "--watch"],
                {"KeepAlive": True, "ThrottleInterval": 60},
            )
    if project.automatic_intake and read_secret(state / "secrets/github-publisher-token"):
        services["worker"] = (
            [str(INTEGRATION / "worker.py"), "work", "--watch"],
            {"KeepAlive": True, "ThrottleInterval": 60},
        )
    elif project.automatic_intake:
        print(
            "Worker activation pending: save the GitHub publisher credential, then rerun services.py."
        )
    for suffix, (arguments, scheduling) in services.items():
        if only and suffix != only:
            continue
        label = "com.sdlc-dispatcher.deck-lab-" + suffix
        path = directory / (label + ".plist")
        config = {
            "Label": label,
            "ProgramArguments": [str(ROOT / ".venv/bin/python"), *arguments],
            "WorkingDirectory": str(ROOT),
            "EnvironmentVariables": {
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": str(Path.home()),
            },
            "RunAtLoad": True,
            "StandardOutPath": str(state / (suffix + ".stdout.log")),
            "StandardErrorPath": str(state / (suffix + ".stderr.log")),
            **scheduling,
        }
        path.write_bytes(plistlib.dumps(config))
        target = f"gui/{os.getuid()}"
        subprocess.run(["launchctl", "bootout", target + "/" + label], capture_output=True)
        for attempt in range(8):
            result = subprocess.run(
                ["launchctl", "bootstrap", target, str(path)], capture_output=True
            )
            if result.returncode == 0:
                break
            if attempt == 7:
                raise RuntimeError("Could not register login service: " + label)
            time.sleep(1)
        print("Installed " + label)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--only",
        choices=[
            "worker",
            "linear-sync",
            "release-controller",
            "preview-cleanup",
            "storage-monitor",
        ],
    )
    install(parser.parse_args().only)
