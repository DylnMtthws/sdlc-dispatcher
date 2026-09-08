"""Save a worker credential privately from an interactive SSH terminal."""

import argparse
import getpass
import os
import stat
import tempfile
import warnings
from pathlib import Path

SECRET_DIR = Path(__file__).resolve().parents[2] / ".dispatcher" / "secrets"
CREDENTIALS = {
    "cursor": ("cursor-api-key", "Cursor API key"),
    "linear-read": ("linear-read-api-key", "Linear read-only API key"),
    "github-publisher": ("github-publisher-token", "Repository-scoped GitHub publisher token"),
}


def save_credential(kind):
    if not os.isatty(0):
        raise SystemExit("Open an interactive SSH shell, or use ssh -t.")
    filename, label = CREDENTIALS[kind]
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        value = getpass.getpass(f"{label} (hidden): ").strip()
    if not value or len(value.encode()) > 4096 or any(c.isspace() for c in value):
        raise SystemExit("Key must be nonempty, at most 4096 bytes, with no whitespace.")
    SECRET_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = SECRET_DIR.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise SystemExit("Secret directory must be an owner-only directory owned by you.")
    fd, temporary = tempfile.mkstemp(dir=SECRET_DIR)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, SECRET_DIR / filename)
    finally:
        Path(temporary).unlink(missing_ok=True)
    print(f"{label} saved privately. No agent was started.")


if __name__ == "__main__":
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("credential", choices=CREDENTIALS)
    args = parser.parse_args()
    save_credential(args.credential)
