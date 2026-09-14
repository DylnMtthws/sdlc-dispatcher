"""Sync durable pipeline stages into Linear and observe human GitHub release decisions."""

import argparse
import fcntl
import json
import os
import time
from pathlib import Path

from receiver import read_secret

from sdlc_dispatcher.activity import reconcile_activity
from sdlc_dispatcher.config import DispatchError, load_project
from sdlc_dispatcher.linear_sync import LinearClient, ReleaseObserver, reconcile
from sdlc_dispatcher.publish import GitHub
from sdlc_dispatcher.store import Store

ROOT = Path(__file__).resolve().parents[2]
STATES = {
    "queued": ("Todo", "unstarted", "#e2e2e2"),
    "coding": ("In Progress", "started", "#f2c94c"),
    "testing": ("Verifying", "started", "#5e6ad2"),
    "ai_review": ("AI Review", "started", "#5e6ad2"),
    "repairing": ("Repairing", "started", "#f2994a"),
    "awaiting_approval": ("In Review", "started", "#5e6ad2"),
    "ready_for_release": ("Merged — Awaiting Deployment", "started", "#4ea7fc"),
    "deploying": ("Deploying", "started", "#4ea7fc"),
    "blocked": ("Blocked", "started", "#eb5757"),
    "done": ("Done", "completed", "#27ae60"),
    "cancelled": ("Canceled", "canceled", "#95a2b3"),
    "duplicate": ("Duplicate", "duplicate", "#95a2b3"),
}


def clients():
    state = ROOT / ".dispatcher"
    read = read_secret(state / "secrets/linear-read-api-key")
    write = read_secret(state / "secrets/linear-status-api-key")
    github = read_secret(state / "secrets/github-publisher-token")
    if not all((read, write, github)):
        raise DispatchError(
            "Save Linear read, Linear status and GitHub publisher credentials first"
        )
    return LinearClient(read, write), github


def setup(project, linear):
    query = "query($id:String!){team(id:$id){states{nodes{id name type}}}}"
    current = linear.call(query, {"id": project.linear_team_id})["team"]["states"]["nodes"]
    result = {}
    for index, (stage, (name, kind, color)) in enumerate(STATES.items()):
        matches = [state for state in current if state["name"] == name]
        if matches:
            if len(matches) != 1 or matches[0]["type"] != kind:
                raise DispatchError(
                    "Existing Linear state conflicts with pipeline mapping: " + name
                )
            result[stage] = matches[0]["id"]
            continue
        created = linear.call(
            """mutation($input:WorkflowStateCreateInput!){
            workflowStateCreate(input:$input){success workflowState{id name type}}}""",
            {
                "input": {
                    "teamId": project.linear_team_id,
                    "name": name,
                    "type": kind,
                    "color": color,
                    "position": (
                        1100 + index
                        if stage in {"ready_for_release", "deploying", "blocked"}
                        else 200 + index
                    ),
                    "description": "Dispatcher stage; Done requires a verified production release.",
                }
            },
            write=True,
        )["workflowStateCreate"]
        if not created.get("success"):
            raise DispatchError("Linear did not confirm workflow state creation")
        result[stage] = created["workflowState"]["id"]
    path = ROOT / ".dispatcher/linear-pipeline-states.json"
    path.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"configured_states": len(result), "mapping": str(path)}))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    lock = (ROOT / ".dispatcher/linear-sync.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("Linear synchronizer is already running") from None
    project_path = Path(__file__).with_name("project.toml")
    project = load_project(project_path)
    linear, github_key = clients()
    if args.setup:
        setup(project, linear)
        return
    if not project.linear_statuses:
        raise DispatchError(
            "Register the configured Linear workflow state IDs before synchronization"
        )
    store = Store(ROOT / ".dispatcher/queue.db")
    last_backfill, last_release = float("-inf"), float("-inf")
    while True:
        try:
            project = load_project(project_path)
            now = time.monotonic()
            if (args.backfill or args.watch) and now - last_backfill >= 300:
                count = linear.backfill(store, project)
                last_backfill = now
                print(json.dumps({"feedback_reconciled": count}), flush=True)
            observe = now - last_release >= 30
            observer = ReleaseObserver(
                project, GitHub(project.github_repository, github_key), store=store
            )
            count = reconcile(store, project, linear, observer, observe_releases=observe)
            if observe:
                last_release = now
            if count:
                print(json.dumps({"linear_statuses_updated": count}), flush=True)
        except (DispatchError, KeyError, TypeError, ValueError):
            print(
                json.dumps(
                    {
                        "sync": "retrying",
                        "detail": "Provider or configuration failure; no completion inferred",
                    }
                ),
                flush=True,
            )
            if not args.watch:
                raise
            time.sleep(30)
        activity_count = reconcile_activity(store, project, linear)
        if activity_count:
            print(json.dumps({"linear_activity_updated": activity_count}), flush=True)
        if not args.watch:
            return
        time.sleep(10)


if __name__ == "__main__":
    main()
