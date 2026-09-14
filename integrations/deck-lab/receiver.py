"""Mac mini pilot receiver and private SSH secret setup; no agent process."""

from __future__ import annotations

import argparse
import getpass
import os
import stat
import subprocess
import tempfile
import threading
import time
import warnings
from collections import deque
from pathlib import Path

from sdlc_dispatcher.config import load_project
from sdlc_dispatcher.server import application
from sdlc_dispatcher.store import Store

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / ".dispatcher"
SECRET = STATE / "secrets" / "deck-lab-linear-secret"
LABEL = "com.sdlc-dispatcher.deck-lab-receiver"


class RateLimit:
    """Bound requests globally without trusting forwarded client IP headers."""

    def __init__(self, app, limit=120):
        self.app = app
        self.limit = limit
        self.arrivals = deque()
        self.lock = threading.Lock()

    def __call__(self, environ, start_response):
        now = time.monotonic()
        with self.lock:
            while self.arrivals and self.arrivals[0] <= now - 60:
                self.arrivals.popleft()
            limited = len(self.arrivals) >= self.limit
            if not limited:
                self.arrivals.append(now)
        if limited:
            body = b'{"error":"rate_limited"}'
            start_response(
                "429 Too Many Requests",
                [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(body))),
                    ("Retry-After", "60"),
                    ("Cache-Control", "no-store"),
                ],
            )
            return [body]
        return self.app(environ, start_response)


def read_secret(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return ""
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 4096
        ):
            raise RuntimeError("Secret must be an owner-only regular file of at most 4096 bytes")
        return stream.read().strip()


def set_secret():
    if not os.isatty(0):
        raise SystemExit("Run interactively over SSH with a TTY (ssh -t).")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        secret = getpass.getpass("Linear webhook signing secret (hidden): ").strip()
    if not secret or len(secret.encode()) > 4096 or any(c.isspace() for c in secret):
        raise SystemExit("Secret must be nonempty, at most 4096 bytes, with no whitespace.")
    SECRET.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=SECRET.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(secret)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, SECRET)
    finally:
        Path(temporary).unlink(missing_ok=True)
    result = subprocess.run(
        ["/bin/launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{LABEL}"],
        capture_output=True,
    )
    print("Signing secret saved privately.")
    print("Receiver restarted." if result.returncode == 0 else "Receiver needs to be started.")


def serve():
    from waitress import serve as waitress_serve

    secret = read_secret(SECRET)
    os.environ["DISPATCHER_LINEAR_SECRET_DECK_LAB"] = secret
    store = Store(STATE / "queue.db")

    def app(environ, start_response):
        project = load_project(Path(__file__).with_name("project.toml"))
        return application(store, {project.id: project})(environ, start_response)

    waitress_serve(
        RateLimit(app),
        host="127.0.0.1",
        port=8787,
        threads=4,
        connection_limit=32,
        max_request_body_size=256_000,
        max_request_header_size=16_384,
        channel_timeout=30,
    )


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["serve", "set-secret"])
    args = parser.parse_args()
    if args.command == "set-secret":
        set_secret()
    else:
        serve()
