"""Webhook-only WSGI app. Administration stays on the local CLI."""

import json
import os

from .config import DispatchError
from .linear import receive


def application(store, projects):
    def app(environ, start_response):
        def reply(status, body):
            payload = json.dumps(body).encode()
            start_response(
                status,
                [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(payload))),
                    ("Cache-Control", "no-store"),
                ],
            )
            return [payload]

        method, path = environ.get("REQUEST_METHOD"), environ.get("PATH_INFO", "")
        if method == "GET" and path == "/healthz":
            return reply("200 OK", {"ok": True})
        prefix = "/webhooks/linear/"
        project = projects.get(path[len(prefix) :]) if path.startswith(prefix) else None
        if method != "POST" or not project:
            return reply("404 Not Found", {"error": "not_found"})
        secret = os.environ.get(
            "DISPATCHER_LINEAR_SECRET_" + project.id.upper().replace("-", "_"), ""
        )
        if not secret:
            return reply("503 Service Unavailable", {"error": "webhook_not_configured"})
        try:
            length = int(environ.get("CONTENT_LENGTH") or "0")
            if length <= 0 or length > 256_000:
                return reply("413 Content Too Large", {"error": "invalid_payload_size"})
            raw = environ["wsgi.input"].read(length)
            if len(raw) != length:
                return reply("400 Bad Request", {"error": "incomplete_payload"})
            receive(
                store,
                project,
                raw,
                environ.get("HTTP_LINEAR_SIGNATURE", ""),
                environ.get("HTTP_LINEAR_DELIVERY", ""),
                secret,
            )
        except (DispatchError, ValueError):
            return reply("400 Bad Request", {"error": "invalid_webhook"})
        except Exception:
            return reply("503 Service Unavailable", {"error": "intake_unavailable"})
        # Do not leak internal job IDs, issue bodies or provider credentials.
        return reply("200 OK", {"ok": True})

    return app
