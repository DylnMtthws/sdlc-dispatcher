"""Trusted Cursor launcher, copied into the container's argv by the dispatcher.

Only the standard library is required in project images. No paid request is sent
by preflight. A run checks exact model availability before submitting its prompt.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from pathlib import Path


def parse_model(model):
    match = re.fullmatch(
        r"([a-z0-9][a-z0-9._-]*)(?:\[([a-z0-9._-]+=[a-z0-9._-]+(?:,[a-z0-9._-]+=[a-z0-9._-]+)*)\])?",
        model,
    )
    if not match or match[1] in {"auto", "default"}:
        raise RuntimeError("Explicit Cursor model ID and valid parameters required")
    pairs = [item.split("=") for item in match[2].split(",")] if match[2] else []
    parameters = dict(pairs)
    if len(parameters) != len(pairs):
        raise RuntimeError("Duplicate Cursor model parameter")
    return match[1], parameters


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def available_model(model):
    """Resolve exact model parameters through ACP without submitting any prompt.

    Cursor's legacy catalog labels differ from its derived runtime labels. Use
    the parameterized picker, require every setting to be explicit, and verify
    the persisted selection before using its derived label in the runtime guard.
    """
    model_id, parameters = parse_model(model)
    command = [
        "agent",
        "--sandbox",
        "disabled",
        "--trust",
        "--endpoint",
        "https://api2.cursor.sh",
        "acp",
    ]
    with subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    ) as process:
        timer = threading.Timer(60, lambda: process.kill() if process.poll() is None else None)
        timer.daemon = True
        timer.start()
        serial = 0

        def rpc(method, params):
            nonlocal serial
            serial += 1
            request_id = serial
            process.stdin.write(
                json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
                + "\n"
            )
            process.stdin.flush()
            while True:
                line = process.stdout.readline(1_000_001)
                if not line or len(line) > 1_000_000:
                    raise RuntimeError(
                        "Cursor model preflight failed; check authentication and connectivity"
                    )
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise RuntimeError("Invalid Cursor model response")
                if "method" in message:
                    # No client tools, permission grants, or coding prompts in preflight.
                    if "id" in message:
                        process.stdin.write(
                            json.dumps(
                                {
                                    "jsonrpc": "2.0",
                                    "id": message["id"],
                                    "error": {
                                        "code": -32601,
                                        "message": "No client tools in preflight",
                                    },
                                }
                            )
                            + "\n"
                        )
                        process.stdin.flush()
                    continue
                if message.get("id") == request_id:
                    if "error" in message or not isinstance(message.get("result"), dict):
                        raise RuntimeError("Cursor rejected model selection; refusing substitution")
                    return message["result"]

        def option(response, name):
            matches = [item for item in response.get("configOptions", []) if item.get("id") == name]
            if len(matches) != 1 or matches[0].get("type") != "select":
                raise RuntimeError("Cursor model setting is unavailable")
            return matches[0]

        try:
            rpc(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {"_meta": {"parameterizedModelPicker": True}},
                    "clientInfo": {"name": "sdlc-dispatcher", "version": "0.1"},
                },
            )
            session = rpc("session/new", {"cwd": "/workspace", "mcpServers": []})
            session_id = session["sessionId"]

            def select(response, name, value):
                setting = option(response, name)
                if value not in {item.get("value") for item in setting.get("options", [])}:
                    raise RuntimeError("Requested Cursor model or parameter is unavailable")
                result = rpc(
                    "session/set_config_option",
                    {"sessionId": session_id, "configId": name, "value": value},
                )
                if option(result, name).get("currentValue") != value:
                    raise RuntimeError("Cursor substituted the requested model setting")
                return result

            selected = select(session, "model", model_id)
            for name, value in parameters.items():
                if name in {"mode", "model"}:
                    raise RuntimeError("Reserved Cursor model parameter")
                selected = select(selected, name, value)
            if option(selected, "model").get("currentValue") != model_id or any(
                option(selected, name).get("currentValue") != value
                for name, value in parameters.items()
            ):
                raise RuntimeError("Cursor substituted the requested model setting")
            config = json.loads(
                (Path(os.environ["CURSOR_CONFIG_DIR"]) / "cli-config.json").read_text()
            )
            actual = config.get("selectedModel", {})
            actual_pairs = actual.get("parameters", [])
            actual_parameters = {item["id"]: item["value"] for item in actual_pairs}
            runtime = config.get("model", {})
            display = runtime.get("displayName")
            if (
                actual.get("modelId") != model_id
                or runtime.get("modelId") != model_id
                or actual_parameters != parameters
                or len(actual_pairs) != len(parameters)
                or not isinstance(display, str)
                or not display
                or len(display) > 200
                or any(ord(char) < 32 for char in display)
            ):
                raise RuntimeError(
                    "Cursor selection differs; specify every model parameter explicitly"
                )
            return display
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise RuntimeError("Invalid Cursor model response") from exc
        finally:
            timer.cancel()
            stop_process(process)


def run(model, display, task):
    command = [
        "agent",
        "--print",
        "--force",
        "--sandbox",
        "disabled",
        "--trust",
        "--workspace",
        "/workspace",
        "--endpoint",
        "https://api2.cursor.sh",
        "--model",
        model,
        "--output-format",
        "stream-json",
        task,
    ]
    initialized = False
    succeeded = False
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as process:
        try:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    event = {}
                if not isinstance(event, dict):
                    event = {}
                if event.get("type") == "system" and event.get("subtype") == "init":
                    reported = event.get("model")
                    # Preserve bounded model metadata even when the guard rejects
                    # startup. Never log the API key or the entire init envelope.
                    safe_reported = reported if isinstance(reported, str) else "<missing>"
                    key = os.environ.get("CURSOR_API_KEY", "")
                    if key:
                        safe_reported = safe_reported.replace(key, "[REDACTED]")
                    print(
                        json.dumps(
                            {
                                "type": "dispatcher_model_check",
                                "requested": model,
                                "listed_display": display,
                                "reported": safe_reported[:200],
                            }
                        ),
                        flush=True,
                    )
                    if not isinstance(reported, str) or reported != display:
                        raise RuntimeError("Cursor reported a different or unknown model")
                    initialized = True
                if event.get("type") == "result":
                    succeeded = event.get("subtype") == "success" and not event.get(
                        "is_error", False
                    )
                print(line, end="", flush=True)
            code = process.wait()
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    if code or not initialized or not succeeded:
        raise RuntimeError("Cursor did not report a successful run with the selected model")


def main():
    mode, model = sys.argv[1:]
    if mode not in {"run", "preflight"} or model == "auto":
        raise RuntimeError("Explicit model and valid mode required")
    os.umask(0o077)
    config = Path(os.environ["CURSOR_CONFIG_DIR"])
    config.mkdir(parents=True, exist_ok=True)
    (config / "cli-config.json").write_text(
        json.dumps(
            {
                "version": 1,
                "editor": {"vimMode": False},
                "permissions": {"allow": [], "deny": ["Task"]},
                "network": {"useHttp1ForAgent": False},
            }
        )
    )
    display = available_model(model)
    if mode == "preflight":
        print(
            json.dumps({"status": "ready", "engine": "cursor", "model": model, "display": display})
        )
    else:
        task = sys.stdin.read()
        if not task.strip():
            raise RuntimeError("Missing report prompt")
        run(model, display, task)


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, subprocess.SubprocessError) as error:
        # Do not echo provider output or exception bodies containing requests/keys.
        message = str(error) if type(error) is RuntimeError else "Cursor launcher failed"
        print(message, file=sys.stderr)
        sys.exit(1)
