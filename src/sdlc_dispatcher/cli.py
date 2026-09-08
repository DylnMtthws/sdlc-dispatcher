"""Local administration; enabling live agents and publishing are explicit actions."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .config import DispatchError, Project, load_project
from .publish import publish, publish_ready
from .runner import DockerRunner
from .server import application
from .store import Store
from .worker import check_agent, verify_project, work_once


def demo(state: Path, image: str) -> dict:
    root = state.resolve() / "demo"
    if root.exists():
        raise DispatchError("Demo directory already exists; choose a fresh --state directory")
    repo = root / "repository"
    repo.mkdir(parents=True)
    (repo / "greeting.py").write_text('def greet(name):\n    return "Hello, " + name\n')
    (repo / "test_greeting.py").write_text(
        "import unittest\nfrom greeting import greet\n\n"
        "class GreetingTest(unittest.TestCase):\n"
        '    def test_basic(self):\n        self.assertEqual(greet("Ada"), "Hello, Ada")\n'
    )
    for args in (
        ["init", "-b", "main"],
        ["add", "."],
        [
            "-c",
            "user.name=Dispatcher Demo",
            "-c",
            "user.email=demo@example.test",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-m",
            "Seed offline demo",
        ],
    ):
        subprocess.run(
            ["/usr/bin/git", "-C", str(repo), *args],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    project = Project(
        id="demo",
        repository=str(repo),
        base_ref="main",
        image=image,
        checks=[["python", "-m", "unittest", "discover", "-v"]],
        allowed_paths=["greeting.py", "test_*.py"],
        timeout_seconds=120,
    )
    store = Store(state / "queue.db")
    job = store.ingest(
        project,
        "demo",
        "DEMO-1",
        {
            "title": "Greeting keeps accidental spaces",
            "description": "Entering ' Ada ' should greet Ada without surrounding whitespace.",
        },
    )
    store.approve(job, project)
    # Deliberately deterministic: exercises orchestration, not model quality.
    program = (
        "from pathlib import Path; "
        "p=Path('greeting.py'); p.write_text(p.read_text().replace('+ name', '+ name.strip()')); "
        "Path('test_regression.py').write_text("
        + repr(
            "import unittest\nfrom greeting import greet\n"
            "class Regression(unittest.TestCase):\n"
            '    def test_spaces(self):\n        self.assertEqual(greet(" Ada "), "Hello, Ada")\n'
        )
        + ")"
    )
    work_once(store, project, state / "artifacts", demo_command=["python", "-c", program])
    result = store.get(job)
    return {
        "job": job,
        "status": result["status"],
        "artifact": result["artifact"],
        "events": store.events(job),
        "note": "Offline deterministic demo; no model or external issue/PR calls",
    }


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Project-independent feedback-to-patch dispatcher")
    parser.add_argument("--state", type=Path, default=Path(".dispatcher"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    commands.add_parser("list")
    for name in ("show", "cancel"):
        commands.add_parser(name).add_argument("job")
    for name in ("pause", "resume", "recover"):
        commands.add_parser(name)
    for name in (
        "check-project",
        "check-agent",
        "verify-project",
        "enqueue",
        "approve",
        "retry",
        "work",
        "publish",
    ):
        sub = commands.add_parser(name)
        sub.add_argument("--project", type=Path, required=True)
        if name in {"approve", "retry", "publish"}:
            sub.add_argument("job")
        if name == "retry":
            sub.add_argument("--reason", required=True)
        if name == "enqueue":
            sub.add_argument("--input", type=Path, required=True)
            sub.add_argument("--issue-id", required=True)
        if name == "work":
            sub.add_argument("--allow-live-agent", action="store_true")
            sub.add_argument(
                "--watch",
                action="store_true",
                help="Poll the durable queue every five seconds",
            )
        if name == "publish":
            sub.add_argument("--reconcile", action="store_true")
    server = commands.add_parser("serve")
    server.add_argument("--project", type=Path, action="append", required=True)
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8787)
    example = commands.add_parser("demo")
    example.add_argument("--image", default="sdlc-dispatcher-test:local")
    args = parser.parse_args(argv)
    try:
        store = Store(args.state / "queue.db")
        project = (
            load_project(args.project)
            if hasattr(args, "project") and isinstance(args.project, Path)
            else None
        )
        result = {"ok": True}
        if args.command == "init":
            result = {"state": str(args.state.resolve()), "automatic_intake": False}
        elif args.command == "check-project":
            from .workspace import export

            sha, files = export(project)
            result = {
                "id": project.id,
                "base_sha": sha,
                "files": len(files),
                "policy": project.fingerprint,
                "automatic_intake": project.automatic_intake,
            }
        elif args.command == "list":
            result = store.jobs()
        elif args.command == "verify-project":
            result = verify_project(project, args.state / "artifacts")
        elif args.command == "check-agent":
            result = check_agent(project, args.state / "artifacts")
        elif args.command == "show":
            row = store.get(args.job)
            row["report"] = json.loads(row["report"])
            result = {"job": row, "events": store.events(args.job)}
        elif args.command == "enqueue":
            result = {
                "job": store.ingest(
                    project, "manual", args.issue_id, json.loads(args.input.read_text())
                )
            }
        elif args.command == "approve":
            store.approve(args.job, project)
        elif args.command == "retry":
            store.retry(args.job, project, args.reason)
        elif args.command == "cancel":
            store.cancel(args.job)
        elif args.command in {"pause", "resume"}:
            store.pause(args.command == "pause")
        elif args.command == "recover":
            runner = DockerRunner()
            with store.connect() as db:
                stale = [
                    dict(row)
                    for row in db.execute(
                        "SELECT * FROM jobs WHERE status IN ('running','verifying') AND deadline<?",
                        (time.time(),),
                    )
                ]
            for row in stale:
                runner.stop(row["id"])
                store.finish(
                    row["id"],
                    "failed",
                    "Expired worker stopped; explicit reapproval required",
                )
            result = {"recovered": [row["id"] for row in stale]}
        elif args.command == "work":
            while True:
                job = work_once(
                    store,
                    project,
                    args.state / "artifacts",
                    allow_live=args.allow_live_agent,
                )
                if job:
                    publish_ready(store, project, job)
                if not args.watch:
                    result = {
                        "job": job,
                        "status": store.get(job)["status"] if job else "idle",
                    }
                    break
                if job:
                    print(
                        json.dumps({"job": job, "status": store.get(job)["status"]}),
                        flush=True,
                    )
                time.sleep(5)
        elif args.command == "publish":
            result = {"url": publish(store, project, args.job, reconcile=args.reconcile)}
        elif args.command == "serve":
            try:
                from waitress import serve
            except ImportError as exc:
                raise DispatchError('Install the server extra: pip install ".[server]"') from exc
            projects = [load_project(path) for path in args.project]
            if len({p.id for p in projects}) != len(projects):
                raise DispatchError("Duplicate project registration")
            serve(
                application(store, {p.id: p for p in projects}),
                host=args.host,
                port=args.port,
                max_request_body_size=256_000,
                channel_timeout=30,
                threads=4,
            )
        elif args.command == "demo":
            result = demo(args.state, args.image)
        print(json.dumps(result, indent=2))
        if isinstance(result, dict) and result.get("status") in {
            "blocked",
            "failed",
            "cancelled",
        }:
            return 1
        return 0
    except (DispatchError, OSError, ValueError) as exc:
        # OS/parser exceptions can include private paths or submitted data.
        message = str(exc) if isinstance(exc, DispatchError) else "Local file or input error"
        print(json.dumps({"error": message}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
