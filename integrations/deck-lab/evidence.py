"""Create private, expiring candidate previews and independent Docker browser evidence."""

import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import replace
from difflib import unified_diff
from pathlib import Path

from sdlc_dispatcher.config import DispatchError, load_project
from sdlc_dispatcher.runner import DockerRunner
from sdlc_dispatcher.workspace import File, changes, export, materialize

ROOT = Path(__file__).resolve().parents[2]
INTEGRATION = Path(__file__).resolve().parent
TTL = 8 * 3600


def command(args, *, timeout=45, log=None):
    try:
        return subprocess.run(args, check=True, capture_output=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        if log:
            log.write_bytes(
                ((getattr(exc, "stdout", None) or b"") + (getattr(exc, "stderr", None) or b""))[
                    -100_000:
                ]
            )
        raise DispatchError("Preview operation failed; inspect private evidence logs") from exc


def scenarios_for(artifact):
    changed_source = "\n".join(
        line
        for item in artifact["changes"]
        for line in unified_diff(
            (item.get("before") or "").splitlines(), (item.get("after") or "").splitlines(), n=0
        )
        if line.startswith(("+", "-"))
    )
    scenarios = [
        name
        for name, token in [
            ("spoiler", "renderSpoiler"),
            ("drag", "dragstart"),
            ("home", "dl-step"),
        ]
        if token in changed_source
    ]
    if "home" not in scenarios and any(
        item["path"].endswith("/deck_lab/home.html") for item in artifact["changes"]
    ):
        scenarios.append("home")
    return scenarios


def collect(artifact_path, output, *, source_repository=None):
    os.umask(0o077)
    project = load_project(INTEGRATION / "project.toml")
    raw = artifact_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    artifact = json.loads(raw)
    scenarios = scenarios_for(artifact)
    if artifact["project"] != project.id or artifact["policy"] != project.fingerprint:
        raise DispatchError("Preview artifact policy is stale")
    _, before = export(
        replace(
            project,
            base_ref=artifact["base_sha"],
            repository=str(source_repository or project.repository),
        )
    )
    after = dict(before)
    for item in artifact["changes"]:
        if item["after"] is None:
            after.pop(item["path"], None)
        else:
            after[item["path"]] = File(item["after"].encode(), item["mode"] == "100755")
    if changes(project, before, after) != artifact["changes"]:
        raise DispatchError("Preview candidate validation failed")
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    captures = output / "captures"
    captures.mkdir(mode=0o700)
    runtime = DockerRunner()
    app_image = runtime.image_id(project.review_image)
    browser_image = runtime.image_id("sdlc-browser:local")
    gateway_image = runtime.image_id("nginxinc/nginx-unprivileged:stable-alpine")
    name = "sdlc-preview-" + digest[:16]
    registry = ROOT / ".dispatcher/previews"
    registry.mkdir(exist_ok=True, mode=0o700)
    # Recreating the same artifact is explicit; never replace a different preview.
    saved = registry / (digest + ".json")
    if saved.exists():
        stop(json.loads(saved.read_text()))
    https_port = 20000 + int(digest[:4], 16) % 20000
    status = json.loads(command(["tailscale", "status", "--json"]))
    hostname = status["Self"]["DNSName"].rstrip(".")
    serve = json.loads(command(["tailscale", "serve", "status", "--json"]))
    if str(https_port) in serve.get("TCP", {}):
        raise DispatchError("Private preview port is already owned by another service")
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    expires = time.time() + TTL
    receipt = {
        "artifact_digest": digest,
        "url": f"https://{hostname}:{https_port}",
        "expires_at": expires,
        "name": name,
        "https_port": https_port,
        "local_port": port,
        "output": str(output),
        "source_sha": artifact["base_sha"],
        "review_url": f"https://{hostname}:{https_port}/__dispatcher__/review",
    }
    common = [
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--pids-limit=128",
        "--cpus=1",
        "--label",
        "sdlc-preview=true",
        "--label",
        f"sdlc-expires={expires}",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
    ]
    created, networks = [], []
    route_created = False
    try:
        for phase, files in (("before", before), ("after", after)):
            workspace = output / (phase + "-source")
            materialize(
                workspace,
                {
                    path: item
                    for path, item in files.items()
                    if path.startswith(("src/", "config/", "fixtures/"))
                    or path == "scripts/setup_db.py"
                },
            )
            network = name + "-" + phase
            command(
                [
                    "docker",
                    "network",
                    "create",
                    "--internal",
                    "--label",
                    "sdlc-preview=true",
                    network,
                ]
            )
            networks.append(network)
            app = network + "-app"
            env = {
                "DISPATCHER_PREVIEW": "1",
                "DISPATCHER_SOURCE": "/workspace",
                "SABER_SKIP_DOTENV": "1",
                "SABER_DB_PATH": "/tmp/deck-lab-preview.db",
                "SABER_AUTH_MODE": "password",
                "SABER_SECRET_KEY": secrets.token_hex(24),
                "SABER_DECK_LAB_ASSET_DIR": "/tmp/assets",
                "SABER_PUBLIC": "0",
                "SABER_DECK_LAB_REDESIGN": "1",
                "SABER_DECK_LAB_BUILDER": "1",
                "SABER_DECK_LAB_RESEARCH": "1",
                "SABER_DECK_LAB_PLAYMAT": "1",
                "SABER_DECK_LAB_DEV": "0",
                "SABER_RESEARCH_SYNC": "0",
                "LINEAR_FEEDBACK_ENABLED": "false",
                "SABER_COOKIE_SECURE": "0",
                "PYTHONPATH": "/workspace:/workspace/src",
                "HOME": "/tmp",
            }
            args = [
                "docker",
                "run",
                "--detach",
                "--name",
                app,
                "--network",
                network,
                "--network-alias",
                "app",
                "--network-alias",
                "app.test",
                *common,
                "--memory=512m",
                "--mount",
                f"type=bind,src={workspace},dst=/workspace,readonly",
            ]
            for key, value in env.items():
                args.extend(["--env", key + "=" + value])
            args.extend(
                [
                    "--entrypoint",
                    "timeout",
                    app_image,
                    str(TTL),
                    "python",
                    "-c",
                    (INTEGRATION / "seed_preview.py").read_text(),
                ]
            )
            command(args)
            created.append(app)
            for _ in range(40):
                result = subprocess.run(
                    [
                        "docker",
                        "exec",
                        app,
                        "python",
                        "-c",
                        "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2)",
                    ],
                    capture_output=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    break
                time.sleep(0.5)
            else:
                logs = subprocess.run(["docker", "logs", app], capture_output=True, timeout=10)
                (output / (phase + "-startup.log")).write_bytes(
                    (logs.stdout + logs.stderr)[-100_000:]
                )
                raise DispatchError("Synthetic preview did not become healthy")
            browser = network + "-browser"
            created.append(browser)
            command(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--name",
                    browser,
                    "--network",
                    network,
                    *common,
                    "--memory=768m",
                    "--shm-size=128m",
                    "--env",
                    "HOME=/tmp",
                    "--env",
                    "EVIDENCE_PHASE=" + phase,
                    "--env",
                    "EVIDENCE_SCENARIOS=" + ",".join(scenarios),
                    "--mount",
                    f"type=bind,src={captures},dst=/evidence",
                    "--entrypoint",
                    "python",
                    browser_image,
                    "-c",
                    (INTEGRATION / "browser_evidence.py").read_text(),
                ],
                timeout=180,
                log=output / (phase + "-browser.log"),
            )
            created.remove(browser)
            if phase == "before":
                command(["docker", "rm", "--force", app])
                created.remove(app)
                command(["docker", "network", "rm", network])
                networks.remove(network)
                shutil.rmtree(workspace)
        for capture in captures.iterdir():
            if (
                not capture.is_file()
                or capture.is_symlink()
                or capture.suffix not in {".json", ".png", ".webm"}
            ):
                raise DispatchError("Unexpected browser output")
            capture.rename(output / capture.name)
        captures.rmdir()
        base_result = json.loads((output / "before.json").read_text())
        result = json.loads((output / "after.json").read_text())
        if set(result["page_errors"]) - set(base_result["page_errors"]):
            raise DispatchError("Candidate introduced browser errors")
        public = output / "public"
        public.mkdir(mode=0o755)
        (public / "info.json").write_text(
            json.dumps(
                {
                    key: receipt[key]
                    for key in ("artifact_digest", "url", "expires_at", "source_sha", "review_url")
                },
                indent=2,
            )
        )
        # Only these controller-selected files are served; no source, logs or auth.
        config = output / "nginx.conf"
        config.write_text(
            "server { listen 8080; client_max_body_size 11m;\n"
            "location = /__dispatcher__/info { default_type application/json; alias /proof/info.json; }\n"
            "location = /__dispatcher__/review { default_type application/json; alias /proof/review.json; }\n"
            "location / { proxy_pass http://app:8080; proxy_set_header Host $http_host; "
            'proxy_set_header X-Forwarded-Proto https; proxy_set_header Accept-Encoding ""; '
            'sub_filter_once on; sub_filter "</body>" "<aside style=\\"position:fixed;bottom:0;left:0;right:0;z-index:99999;background:#222;color:#fff;padding:8px;text-align:center\\">'
            f'Test preview · candidate {digest[:12]}</aside></body>"; }} }}\n'
        )
        gateway = name + "-gateway"
        command(
            [
                "docker",
                "create",
                "--name",
                gateway,
                "--network",
                "bridge",
                *common,
                "--memory=128m",
                "--publish",
                f"127.0.0.1:{port}:8080",
                "--mount",
                f"type=bind,src={config},dst=/etc/nginx/conf.d/default.conf,readonly",
                "--mount",
                f"type=bind,src={public},dst=/proof,readonly",
                "--entrypoint",
                "nginx",
                gateway_image,
                "-g",
                "daemon off;",
            ]
        )
        created.append(gateway)
        command(["docker", "network", "connect", name + "-after", gateway])
        command(["docker", "start", gateway])
        # A new private Serve port only; do not modify existing Funnel/Serve routes.
        command(["tailscale", "serve", "--bg", f"--https={https_port}", f"http://127.0.0.1:{port}"])
        route_created = True
        receipt.update(containers=created, networks=networks)
        saved.write_text(json.dumps(receipt, indent=2))
        manifest = {
            "files": [
                "before.json",
                "after.json",
                *base_result["images"],
                *result["images"],
                *base_result.get("videos", []),
                *result.get("videos", []),
            ],
            "images": base_result["images"] + result["images"],
            "preview": receipt,
            "limitations": [
                "Synthetic cards and locally decoded test artwork; no production data or external network.",
                "Chromium and WebKit desktop/mobile; Playwright WebKit is not branded Safari 26.",
                "Native drag compositor pixels are not captured by page screenshots; drag telemetry identifies the selected image and actual mouse/drop behavior.",
            ],
        }
        (output / "evidence.json").write_text(json.dumps(manifest, indent=2))
        print(json.dumps({"preview": receipt["url"], "artifact_digest": digest}))
        return manifest
    except BaseException:
        if route_created:
            command(["tailscale", "serve", f"--https={https_port}", "off"])
        for container in reversed(created):
            subprocess.run(["docker", "rm", "--force", container], capture_output=True, timeout=20)
        for network in reversed(networks):
            subprocess.run(["docker", "network", "rm", network], capture_output=True, timeout=20)
        raise


def stop(receipt):
    digest = receipt["artifact_digest"]
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise DispatchError("Invalid preview identity")
    prefix = "sdlc-preview-" + digest[:16]
    port = 20000 + int(digest[:4], 16) % 20000
    if receipt["https_port"] != port or any(
        not name.startswith(prefix + "-") for name in receipt["containers"] + receipt["networks"]
    ):
        raise DispatchError("Preview resource identity mismatch")
    serve = json.loads(command(["tailscale", "serve", "status", "--json"]))
    if str(port) in serve.get("TCP", {}):
        targets = [
            handler.get("Proxy")
            for address, entry in serve.get("Web", {}).items()
            if address.endswith(":" + str(port))
            for handler in entry.get("Handlers", {}).values()
        ]
        # Legacy local receipts can establish ownership from the named gateway's binding.
        local_port = receipt.get("local_port")
        if not local_port:
            gateway = json.loads(command(["docker", "inspect", prefix + "-gateway"]))[0]
            local_port = int(gateway["HostConfig"]["PortBindings"]["8080/tcp"][0]["HostPort"])
        if targets != [f"http://127.0.0.1:{local_port}"]:
            raise DispatchError("Preview route changed ownership; inspect before cleanup")
        command(["tailscale", "serve", f"--https={port}", "off"])
    for name in receipt["containers"]:
        command(["docker", "rm", "--force", name])
    for name in receipt["networks"]:
        command(["docker", "network", "rm", "--force", name])


def cleanup_expired():
    for path in (ROOT / ".dispatcher/previews").glob("*.json"):
        receipt = json.loads(path.read_text())
        if receipt["expires_at"] <= time.time():
            stop(receipt)
            path.unlink()


def interrupted(signum, frame):
    raise DispatchError("Preview preparation interrupted")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cleanup-expired", action="store_true")
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, interrupted)
    if args.cleanup_expired:
        cleanup_expired()
    elif args.artifact and args.output:
        collect(args.artifact, args.output)
    else:
        parser.error("Provide --artifact and --output, or --cleanup-expired")


if __name__ == "__main__":
    main()
