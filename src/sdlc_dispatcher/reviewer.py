"""Manual, isolated review primitive. It cannot publish or change queue state."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import secrets
import subprocess
import time
import uuid
from pathlib import Path

from .config import DispatchError
from .review_contract import PROMPT, SCHEMA, validate_review
from .reviewer_auth import CLI_VERSION, MODEL, access_lease
from .runner import DockerRunner
from .workspace import materialize, snapshot


def codex_command(images):
    settings = {
        "model_provider": "dispatcher-review",
        "model_reasoning_effort": "high",
        "approval_policy": "never",
        "web_search": "disabled",
        "project_doc_max_bytes": 0,
        "features.multi_agent": False,
        "model_providers.dispatcher-review.name": "Dispatcher isolated review",
        "model_providers.dispatcher-review.base_url": "http://review-api:8080",
        "model_providers.dispatcher-review.env_key": "REVIEW_PROXY_TOKEN",
        "model_providers.dispatcher-review.wire_api": "responses",
        "model_providers.dispatcher-review.requires_openai_auth": False,
        "model_providers.dispatcher-review.supports_websockets": False,
        "model_providers.dispatcher-review.request_max_retries": 0,
        "model_providers.dispatcher-review.stream_max_retries": 0,
    }
    # Docker is the outer sandbox: source/evidence/root are read-only; only /tmp is writable.
    command = [
        "codex",
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--ephemeral",
        "--skip-git-repo-check",
        "--json",
        "--color",
        "never",
        "--model",
        MODEL,
        "--sandbox",
        "danger-full-access",
        "--output-schema",
        "/review/schema.json",
    ]
    for key, value in settings.items():
        command.extend(["-c", key + "=" + json.dumps(value)])
    for name in images:
        command.extend(["--image", "/review/evidence/" + name])
    return command + ["-"]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def run_review(
    *,
    packet: Path,
    output: Path,
    auth_home: Path,
    image: str,
    timeout: int = 900,
    containment_only=False,
    cancelled=lambda: False,
):
    if not 10 <= timeout <= 900:
        raise DispatchError("Review timeout must be between 10 and 900 seconds")
    output = output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    files = snapshot(packet.resolve(), 30_000_000)
    for required in ("packet.json", "policy.md", "review.diff"):
        if required not in files:
            raise DispatchError("Missing required review input: " + required)
    packet_data = json.loads(files["packet.json"].data)
    images = packet_data.get("images", [])
    if not isinstance(images, list) or any(
        not isinstance(name, str)
        or "/" in name
        or name in {".", ".."}
        or "evidence/" + name not in files
        for name in images
    ):
        raise DispatchError("Invalid review image references")
    # Candidate files cannot supply policy, hooks, config, or schema to the runner.
    forbidden = {
        "AGENTS.md",
        "CLAUDE.md",
        ".codex",
        ".agents",
        ".cursor",
        ".claude",
        ".env",
        "auth.json",
        "credentials.json",
    }
    for name in files:
        if any(part in forbidden or part.startswith(".env.") for part in Path(name).parts):
            raise DispatchError("Review packet contains instructions/configuration/credentials")
    from .workspace import File

    files["schema.json"] = File(json.dumps(SCHEMA).encode())
    files["prompt.txt"] = File(PROMPT.encode())
    frozen = output / "input"
    materialize(frozen, files)
    (frozen / "source").mkdir(exist_ok=True)
    hashes = {name: digest(item.data) for name, item in sorted(files.items())}
    input_digest = digest(json.dumps(hashes, sort_keys=True).encode())
    metadata = {
        "status": "running",
        "input_digest": input_digest,
        "input_hashes": hashes,
        "model_requested": MODEL,
        "reasoning_effort": "high",
        "speed": "standard",
        "cli_version": CLI_VERSION,
        "started_at": time.time(),
        "kind": "containment_check" if containment_only else packet_data.get("kind"),
        "auth_mode": "dedicated_chatgpt_managed",
        "cleanup": "pending",
    }
    runner = DockerRunner()
    image_id = runner.image_id(image)
    metadata["image"] = image_id
    job = "review-" + uuid.uuid4().hex
    network = f"sdlc-{job}-internal"
    proxy_name = f"sdlc-{job}-proxy"
    agent_name = f"sdlc-{job}-agent"
    proxy_process = None
    agent_process = None
    run_lock = None

    def docker(*args, env=None):
        try:
            return subprocess.run(
                [runner.docker, *args], check=True, capture_output=True, timeout=30, env=env
            ).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            raise DispatchError("Reviewer Docker operation failed") from exc

    common = [
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user",
        f"{os.getuid()}:{os.getgid()}",
        "--label",
        "sdlc-dispatcher=true",
    ]
    try:
        if containment_only:
            lease = {"access_token": "not-a-token", "account_id": "test"}
        else:
            # Serialize whole reviews to avoid concurrent account refresh/use races.
            run_lock = (auth_home / "review.lock").open("a")
            os.chmod(run_lock.name, 0o600)
            try:
                fcntl.flock(run_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DispatchError("Reviewer is busy; queue this review") from None
            lease = access_lease(auth_home, force_refresh=True)
        capability = secrets.token_urlsafe(32)
        docker("network", "create", "--internal", "--label", "sdlc-dispatcher=true", network)
        docker(
            "create",
            "--name",
            proxy_name,
            "--network",
            "bridge",
            *common,
            "--memory=128m",
            "--cpus=0.5",
            "--pids-limit=64",
            "--interactive",
            "--entrypoint",
            "python",
            image_id,
            "-c",
            Path(__file__).with_name("review_proxy.py").read_text(),
        )
        docker("network", "connect", "--alias", "review-api", network, proxy_name)
        with (output / "proxy.jsonl").open("wb") as proxy_log:
            proxy_process = subprocess.Popen(
                [runner.docker, "start", "--attach", "--interactive", proxy_name],
                stdin=subprocess.PIPE,
                stdout=proxy_log,
                stderr=subprocess.STDOUT,
            )
            proxy_process.stdin.write(
                json.dumps(
                    {
                        "access_token": lease["access_token"],
                        "account_id": lease["account_id"],
                        "capability": capability,
                    }
                ).encode()
                + b"\n"
            )
            proxy_process.stdin.close()
            del lease
            ready_deadline = time.monotonic() + 20
            while b'"proxy_ready"' not in (output / "proxy.jsonl").read_bytes():
                if time.monotonic() > ready_deadline or proxy_process.poll() is not None:
                    raise DispatchError("Reviewer proxy did not become ready")
                time.sleep(0.1)
            command = codex_command(images)
            # Trusted bootstrap verifies the pinned CLI before reading any candidate data.
            bootstrap = (
                "import os,subprocess,sys; "
                "os.makedirs('/tmp/codex',mode=0o700,exist_ok=True); "
                "os.makedirs('/tmp/home',mode=0o700,exist_ok=True); "
                "v=subprocess.check_output(['codex','--version'],stderr=subprocess.DEVNULL)"
                ".decode().strip(); "
                f"assert v=={CLI_VERSION!r}, 'Reviewer CLI version mismatch'; "
                f"os.execvp({command[0]!r},{command!r})"
            )
            if containment_only:
                bootstrap = Path(__file__).with_name("reviewer_containment.py").read_text()
            env = {
                "PATH": os.environ.get("PATH", os.defpath),
                "HOME": os.environ["HOME"],
                "REVIEW_PROXY_TOKEN": capability,
            }
            docker(
                "create",
                "--name",
                agent_name,
                "--network",
                network,
                *common,
                "--memory=1024m",
                "--cpus=2",
                "--pids-limit=128",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
                "--mount",
                f"type=bind,src={frozen},dst=/review,readonly",
                "--workdir",
                "/review/source",
                "--env",
                "HOME=/tmp/home",
                "--env",
                "CODEX_HOME=/tmp/codex",
                "--env",
                "REVIEW_PROXY_TOKEN",
                "--interactive",
                "--entrypoint",
                "python",
                image_id,
                "-c",
                bootstrap,
                env=env,
            )
            with (output / "agent.jsonl").open("wb") as agent_log:
                agent_process = subprocess.Popen(
                    [runner.docker, "start", "--attach", "--interactive", agent_name],
                    stdin=subprocess.PIPE,
                    stdout=agent_log,
                    stderr=subprocess.STDOUT,
                )
                agent_process.stdin.write(PROMPT.encode())
                agent_process.stdin.close()
                deadline = time.monotonic() + timeout
                while agent_process.poll() is None:
                    if cancelled():
                        raise DispatchError("Reviewer cancelled")
                    if time.monotonic() > deadline:
                        raise DispatchError("Reviewer timed out")
                    if (output / "agent.jsonl").stat().st_size > 5_000_000:
                        raise DispatchError("Reviewer output exceeded limit")
                    time.sleep(0.2)
            if agent_process.returncode:
                raise DispatchError("Isolated reviewer failed; inspect private bounded logs")
        if containment_only:
            result = json.loads((output / "agent.jsonl").read_text())
            if result.get("containment") != "passed":
                raise DispatchError("Reviewer containment checks failed")
            metadata["containment"] = result
        else:
            events = []
            for line in (output / "agent.jsonl").read_text().splitlines():
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue  # CLI diagnostics do not count as completion or findings.
            messages = [
                event["item"]["text"]
                for event in events
                if event.get("type") == "item.completed"
                and event.get("item", {}).get("type") == "agent_message"
            ]
            completions = [event for event in events if event.get("type") == "turn.completed"]
            proxy_events = [
                json.loads(line) for line in (output / "proxy.jsonl").read_text().splitlines()
            ]
            if (
                not messages
                or not completions
                or any(event.get("type") in {"error", "turn.failed"} for event in events)
                or any(
                    event["event"] in {"review_transport_failed", "upstream_error"}
                    for event in proxy_events
                )
                or not any(
                    event.get("event") == "response.completed" and event.get("model") == MODEL
                    for event in proxy_events
                )
            ):
                raise DispatchError("Review lacks verified Astra completion")
            review = validate_review(json.loads(messages[-1]), frozen / "source")
            (output / "review.json").write_text(json.dumps(review, indent=2) + "\n")
            metadata.update(
                {
                    "model_reported": MODEL,
                    "usage": completions[-1].get("usage"),
                    "verdict": review["verdict"],
                    "review_digest": digest((output / "review.json").read_bytes()),
                }
            )
        current = snapshot(frozen, 30_000_000)
        if hashes != {name: digest(item.data) for name, item in sorted(current.items())}:
            raise DispatchError("Review input changed during execution")
        metadata["status"] = "completed"
    except BaseException:
        metadata["status"] = "failed"
        raise
    finally:
        try:
            runner.stop(job)
            metadata["cleanup"] = "confirmed"
        except BaseException:
            metadata["status"] = "failed"
            metadata["cleanup"] = "failed; pause reviewer"
            raise
        finally:
            for process in (agent_process, proxy_process):
                if process and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
            if run_lock:
                run_lock.close()
            metadata["finished_at"] = time.time()
            (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--auth-home", required=True, type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--containment-only", action="store_true")
    args = parser.parse_args()
    try:
        result = run_review(**vars(args))
        print(json.dumps({key: result[key] for key in ("status", "cleanup", "input_digest")}))
    except (DispatchError, OSError, ValueError, KeyError, TypeError) as exc:
        # Never render raw provider/auth exception data into a service log.
        message = str(exc) if isinstance(exc, DispatchError) else "Reviewer failed closed"
        parser.exit(1, message + "\n")


if __name__ == "__main__":
    main()
