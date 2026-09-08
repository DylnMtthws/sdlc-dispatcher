"""Dedicated managed Codex auth. No model task runs in this host process."""

from __future__ import annotations

import base64
import fcntl
import json
import os
import selectors
import stat
import subprocess
import time
from pathlib import Path

from .config import DispatchError

MODEL = "gpt-6-astra"
CLI_VERSION = "codex-cli 0.153.1"


def private_file(path: Path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise DispatchError("Reviewer credential file must be an owner-only regular file")


def access_lease(auth_home: Path, *, force_refresh=False) -> dict:
    """Use Codex's supported refresh lifecycle; return no refresh or identity token."""
    auth_home = auth_home.resolve()
    try:
        private_file(auth_home / "auth.json")
    except FileNotFoundError as exc:
        raise DispatchError("Reviewer auth required; run the dedicated reviewer login") from exc
    info = auth_home.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise DispatchError("Reviewer auth directory must be owner-only")
    with (auth_home / "refresh.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        deadline = time.monotonic() + 45
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DispatchError("Reviewer auth is busy") from None
                time.sleep(0.1)
        env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "TMPDIR"}}
        env["CODEX_HOME"] = str(auth_home)
        version = subprocess.run(
            ["codex", "--version"],
            env=env,
            cwd=auth_home,
            capture_output=True,
            check=True,
            timeout=10,
        )
        if version.stdout.decode().strip() != CLI_VERSION:
            raise DispatchError("Codex auth client version changed; revalidate the reviewer")
        command = ["codex", "-c", 'forced_login_method="chatgpt"', "app-server", "--stdio"]
        with subprocess.Popen(
            command,
            cwd=auth_home,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        ) as process:
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            buffer = b""
            serial = 0
            deadline = time.monotonic() + 45

            def rpc(method, params):
                nonlocal serial, buffer
                serial += 1
                process.stdin.write(
                    json.dumps({"id": serial, "method": method, "params": params}).encode() + b"\n"
                )
                process.stdin.flush()
                while True:
                    while b"\n" not in buffer:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            raise DispatchError("Codex auth metadata timed out")
                        chunk = os.read(process.stdout.fileno(), 65536)
                        if not chunk:
                            raise DispatchError("Codex auth metadata connection closed")
                        buffer += chunk
                        if len(buffer) > 4_000_000:
                            raise DispatchError("Codex auth metadata exceeded limit")
                    line, buffer = buffer.split(b"\n", 1)
                    message = json.loads(line)
                    if message.get("id") == serial:
                        if "error" in message:
                            raise DispatchError(
                                "Codex auth metadata failed; reauthenticate if needed"
                            )
                        return message["result"]
                    if "id" in message and "method" in message:
                        raise DispatchError("Unexpected request during auth-only operation")

            try:
                rpc("initialize", {"clientInfo": {"name": "dispatcher_auth", "version": "0.1"}})
                process.stdin.write(b'{"method":"initialized","params":{}}\n')
                process.stdin.flush()
                account = rpc("account/read", {"refreshToken": force_refresh}).get("account") or {}
                if account.get("type") != "chatgpt":
                    raise DispatchError("Dedicated reviewer requires managed ChatGPT auth")
                models = []
                cursor = None
                while True:
                    page = rpc("model/list", {"limit": 100, "cursor": cursor})
                    models.extend(page.get("data", []))
                    cursor = page.get("nextCursor")
                    if not cursor:
                        break
                if not any(m.get("model") == MODEL or m.get("id") == MODEL for m in models):
                    raise DispatchError("Astra is unavailable to the dedicated Codex account")
                private_file(auth_home / "auth.json")
                data = json.loads((auth_home / "auth.json").read_text())
                tokens = data.get("tokens", {})
                access = tokens.get("access_token", "")
                account_id = tokens.get("account_id", "")
                if not access or not account_id or any(c in access + account_id for c in "\r\n"):
                    raise DispatchError("Codex did not retain valid reviewer authentication")
                claims = json.loads(base64.urlsafe_b64decode(access.split(".")[1] + "==="))
                expires = claims.get("exp", 0)
                if expires < time.time() + 1200:
                    raise DispatchError(
                        "Reviewer access token is near expiry; refresh before running"
                    )
                return {
                    "access_token": access,
                    "account_id": account_id,
                    "expires_at": expires,
                    "plan": account.get("planType"),
                    "model": MODEL,
                }
            finally:
                selector.close()
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
