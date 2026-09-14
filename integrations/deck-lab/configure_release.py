"""Install Linear review states and validate the existing owner release gate."""

import argparse
import json
import os
import time
from pathlib import Path

from receiver import read_secret
from sync import clients

from sdlc_dispatcher.config import DispatchError, load_project
from sdlc_dispatcher.release_controller import GitHubRelease
from sdlc_dispatcher.release_store import configure, settings
from sdlc_dispatcher.store import Store

ROOT = Path(__file__).resolve().parents[2]


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["observe", "live", "off"], required=True)
    args = parser.parse_args()
    project = load_project(Path(__file__).with_name("project.toml"))
    store = Store(ROOT / ".dispatcher/queue.db")
    old = settings(store, project)
    if args.mode == "off":
        if old:
            configure(store, project, {**old, "mode": "off"})
        print("Linear release actions disabled. Existing GitHub jobs are not canceled.")
        return
    linear, _ = clients()
    github = GitHubRelease(project.github_repository)
    owner = github.api("user")["login"]
    if github.call("GET", "/actions/variables/RELEASE_OWNER")["value"] != owner:
        raise DispatchError("Authenticated GitHub user does not match RELEASE_OWNER")
    environment = github.call("GET", "/environments/production")
    rule = next(
        (r for r in environment["protection_rules"] if r["type"] == "required_reviewers"), {}
    )
    if rule.get("prevent_self_review") or not any(
        r["type"] == "User" and r["reviewer"]["login"] == owner for r in rule.get("reviewers", [])
    ):
        raise DispatchError("Existing production gate cannot accept the configured owner approval")
    if not read_secret(ROOT / ".dispatcher/secrets/deck-lab-linear-secret"):
        raise DispatchError("Verified Linear webhook ingress is required")
    viewer = linear.call("query{viewer{id name}}", {})["viewer"]
    if old and old["actor_ids"] != [viewer["id"]]:
        raise DispatchError(
            "Linear owner changed; explicitly review configuration before proceeding"
        )
    states = linear.call(
        "query($id:String!){team(id:$id){states{nodes{id name type}}}}",
        {"id": project.linear_team_id},
    )["team"]["states"]["nodes"]
    review = next(s for s in states if s["id"] == project.linear_statuses["awaiting_approval"])
    if review["name"] != "Ready for Your Review":
        result = linear.call(
            "mutation($id:String!,$input:WorkflowStateUpdateInput!){workflowStateUpdate(id:$id,input:$input){success}}",
            {"id": review["id"], "input": {"name": "Ready for Your Review"}},
            write=True,
        )
        if not result["workflowStateUpdate"]["success"]:
            raise DispatchError("Linear did not confirm review-state rename")
    matches = [s for s in states if s["name"] == "Ready to Deploy"]
    if len(matches) > 1 or matches and matches[0]["type"] != "started":
        raise DispatchError("Ready to Deploy state conflicts with existing configuration")
    if not matches:
        result = linear.call(
            "mutation($input:WorkflowStateCreateInput!){workflowStateCreate(input:$input){success workflowState{id name type}}}",
            {
                "input": {
                    "name": "Ready to Deploy",
                    "type": "started",
                    "color": "#4ea7fc",
                    "teamId": project.linear_team_id,
                    "description": "Owner approval to merge and deploy the exact screenshot-reviewed candidate.",
                }
            },
            write=True,
        )["workflowStateCreate"]
        if not result["success"]:
            raise DispatchError("Linear did not confirm deployment state creation")
        matches = [result["workflowState"]]
    config = {
        "mode": args.mode,
        "actor_ids": [viewer["id"]],
        "github_owner": owner,
        "approval_state": matches[0]["id"],
        "review_state": review["id"],
        "deploying_state": project.linear_statuses["deploying"],
        "production_environment_id": environment["id"],
        "enabled_at": old.get("enabled_at", time.time()),
    }
    configure(store, project, config)
    print(
        json.dumps(
            {
                "mode": args.mode,
                "linear_owner": viewer["name"],
                "github_owner": owner,
                "review_state": "Ready for Your Review",
                "approval_state": "Ready to Deploy",
            }
        )
    )


if __name__ == "__main__":
    main()
