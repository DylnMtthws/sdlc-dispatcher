"""Install the local worker and expired-preview collector as login services."""

import os
import plistlib
import subprocess
from pathlib import Path

from receiver import read_secret

ROOT = Path(__file__).resolve().parents[2]
INTEGRATION = Path(__file__).resolve().parent


def install():
    os.umask(0o077)
    state = ROOT / ".dispatcher"
    directory = Path.home() / "Library/LaunchAgents"
    directory.mkdir(exist_ok=True)
    services = {
        "preview-cleanup": (
            [str(INTEGRATION / "evidence.py"), "--cleanup-expired"],
            {"StartInterval": 300},
        ),
    }
    if read_secret(state / "secrets/github-publisher-token"):
        services["worker"] = (
            [str(INTEGRATION / "worker.py"), "work", "--watch"],
            {"KeepAlive": True, "ThrottleInterval": 60},
        )
    else:
        print(
            "Worker activation pending: save the GitHub publisher credential, then rerun services.py."
        )
    for suffix, (arguments, scheduling) in services.items():
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
        subprocess.run(
            ["launchctl", "bootstrap", target, str(path)], check=True, capture_output=True
        )
        print("Installed " + label)


if __name__ == "__main__":
    install()
