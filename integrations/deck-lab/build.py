"""Build local agent/preview images from committed source, never a dirty checkout."""

import argparse
import hashlib
import json
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path

from sdlc_dispatcher.config import DispatchError, load_project
from sdlc_dispatcher.store import Store
from sdlc_dispatcher.workspace import File, export, materialize


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("target", choices=["agent", "preview"])
    parser.add_argument("--job", help="Build a preview of this verified dispatcher job")
    parser.add_argument("--state", type=Path, default=Path(".dispatcher"))
    args = parser.parse_args()
    integration = Path(__file__).resolve().parent
    project = load_project(integration / "project.toml")
    artifact = None
    if args.job:
        if args.target != "preview":
            raise DispatchError("Only previews accept candidate patches")
        job = Store(args.state / "queue.db").get(args.job)
        if job["project"] != project.id or job["status"] not in {"ready", "published"}:
            raise DispatchError("Expected a verified Deck Lab job")
        raw = Path(job["artifact"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != job["artifact_digest"]:
            raise DispatchError("Artifact was changed after verification")
        artifact = json.loads(raw)
        sha, files = export(replace(project, base_ref=artifact["base_sha"]))
        for change in artifact["changes"]:
            if change["after"] is None:
                files.pop(change["path"])
            else:
                files[change["path"]] = File(change["after"].encode(), change["mode"] == "100755")
    else:
        sha, files = export(project)
    with tempfile.TemporaryDirectory(prefix="dispatcher-build-") as temp:
        context = Path(temp) / "source"
        materialize(context, files)
        if args.target == "agent":
            # Dispatcher-owned build recipe is outside the candidate repository.
            (context / "Dispatcher.Dockerfile").write_bytes(
                (integration / "agent.Dockerfile").read_bytes()
            )
            subprocess.run(
                [
                    "docker",
                    "build",
                    "-f",
                    str(context / "Dispatcher.Dockerfile"),
                    "-t",
                    "deck-lab-agent:local" if project.engine == "cursor" else project.image,
                    "--label",
                    f"sdlc.source-sha={sha}",
                    str(context),
                ],
                check=True,
            )
        else:
            subprocess.run(
                [
                    "docker",
                    "build",
                    "--build-arg",
                    f"SABER_BUILD_SHA={sha}" + (f"-sdlc-{args.job[:8]}" if artifact else ""),
                    "-t",
                    "deck-lab-preview-app:local",
                    str(context),
                ],
                check=True,
            )
            subprocess.run(
                [
                    "docker",
                    "build",
                    "-f",
                    str(integration / "preview.Dockerfile"),
                    "-t",
                    "deck-lab-preview:local",
                    str(integration),
                ],
                check=True,
            )
    print(f"Built local {args.target} from {sha}; no image pushed or environment deployed")


if __name__ == "__main__":
    main()
