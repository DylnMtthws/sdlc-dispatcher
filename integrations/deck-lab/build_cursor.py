"""Layer a pinned Cursor CLI over the existing trusted Deck Lab development image."""

import hashlib
import json
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VERSION = "2026.09.02-c22c1a3"
# Recorded from the official versioned download over verified HTTPS.
HASHES = {"arm64": "fb7bc635be6172ebcf68f907fd9217e3614da51916455c6d7fdb66690997884c"}


def main():
    base = "deck-lab-agent:local"
    image = "deck-lab-cursor:local"
    info = json.loads(subprocess.check_output(["docker", "image", "inspect", base], text=True))[0]
    architecture = info["Architecture"]
    digest = HASHES.get(architecture)
    if not digest:
        raise SystemExit("Review and pin the official Cursor package for this image architecture")
    cache = ROOT / ".dispatcher" / "cursor-build"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cache / "agent-cli-package.tar.gz"
    if not archive.exists():
        url = f"https://downloads.cursor.com/lab/{VERSION}/linux/{architecture}/agent-cli-package.tar.gz"
        with urllib.request.urlopen(url, timeout=120) as response:
            with archive.open("wb") as output:
                shutil.copyfileobj(response, output)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise SystemExit("Cursor package checksum mismatch; refusing build")
    # Pin the selected base under a task-owned tag before building the extension.
    pinned = "sdlc-cursor-base:" + info["Id"].split(":")[1][:16]
    subprocess.run(["docker", "tag", info["Id"], pinned], check=True)
    with tempfile.TemporaryDirectory(prefix="build-", dir=cache) as temporary:
        context = Path(temporary)
        shutil.copyfile(archive, context / archive.name)
        subprocess.run(
            [
                "docker",
                "build",
                "-f",
                str(ROOT / "containers" / "cursor.Dockerfile"),
                "--build-arg",
                f"BASE_IMAGE={pinned}",
                "--build-arg",
                f"CURSOR_SHA256={digest}",
                "--label",
                f"sdlc.cursor-version={VERSION}",
                "--label",
                f"sdlc.base-image={info['Id']}",
                "-t",
                image,
                str(context),
            ],
            check=True,
        )
    print(f"Built {image} with Cursor {VERSION}; no model request or image push")


if __name__ == "__main__":
    main()
