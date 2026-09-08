"""Single-review Responses proxy. Account tokens arrive on stdin, never in argv.

Runs in its own container with no source/evidence/auth directory mounts.
"""

import hmac
import http.client
import json
import ssl
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "gpt-6-astra"
MAX_BODY = 8_000_000
UPSTREAM = "chatgpt.com"
UPSTREAM_PATH = "/backend-api/codex/responses"


def validate_request(path, headers, body, capability):
    if path != "/responses":
        raise ValueError("route_rejected")
    if not hmac.compare_digest(headers.get("Authorization", ""), "Bearer " + capability):
        raise ValueError("capability_rejected")
    if headers.get("Content-Encoding", "identity") != "identity":
        raise ValueError("encoding_rejected")
    if not body or len(body) > MAX_BODY:
        raise ValueError("body_limit")
    data = json.loads(body)
    if not isinstance(data, dict) or data.get("model") != MODEL:
        raise ValueError("model_rejected")
    if data.get("stream") is not True or data.get("store", False) is not False:
        raise ValueError("response_mode_rejected")
    reasoning = data.get("reasoning")
    if not isinstance(reasoning, dict) or reasoning.get("effort") != "high":
        raise ValueError("reasoning_rejected")
    if data.get("service_tier") not in (None, "default"):
        raise ValueError("service_tier_rejected")
    validate_tools(data.get("tools", []))
    return data


def validate_tools(items):
    # Function/custom tool calls execute in the isolated client. Reject provider-
    # hosted browsing, computer use, connectors, and other outbound capabilities.
    if not isinstance(items, list):
        raise ValueError("tools_rejected")
    for item in items:
        if not isinstance(item, dict) or item.get("type") not in {
            "function",
            "custom",
            "namespace",
            "local_shell",
        }:
            raise ValueError("tools_rejected")
        if item.get("type") == "namespace":
            validate_tools(item.get("tools", []))


def verify_event(event):
    if not isinstance(event, dict):
        raise ValueError("invalid_provider_event")
    response = event.get("response") or {}
    if not isinstance(response, dict):
        raise ValueError("invalid_provider_event")
    if event.get("type") in {"error", "response.failed", "response.incomplete"}:
        raise ValueError("provider_failure")
    model = response.get("model")
    if model is not None and model != MODEL:
        raise ValueError("provider_model_mismatch")
    if event.get("type") == "response.completed" and model != MODEL:
        raise ValueError("provider_model_missing")
    return event.get("type") == "response.completed"


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 20

    def log_message(self, *_):
        pass

    def fail(self, status, reason):
        payload = json.dumps({"error": {"message": reason}}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def do_GET(self):
        self.fail(405, "method_rejected")

    def do_CONNECT(self):
        self.fail(405, "method_rejected")

    def do_POST(self):
        try:
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("encoding_rejected")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ValueError("body_limit")
            body = self.rfile.read(length)
            data = validate_request(
                self.path, self.headers, body, self.server.credentials["capability"]
            )
            with self.server.request_lock:
                if self.server.requests >= 40 or time.monotonic() > self.server.deadline:
                    raise ValueError("review_request_limit")
                self.server.requests += 1
        except (ValueError, TypeError, OSError):
            self.fail(403, "review_request_rejected")
            return
        credentials = self.server.credentials
        headers = {
            "Authorization": "Bearer " + credentials["access_token"],
            "ChatGPT-Account-Id": credentials["account_id"],
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "originator": "codex_cli_rs",
            "User-Agent": "codex_cli_rs/0.153.1",
        }
        for name in ("OpenAI-Beta", "session_id", "conversation_id", "x-codex-turn-state"):
            if self.headers.get(name):
                headers[name] = self.headers[name]
        # Keep requests compatible with ChatGPT-managed Responses without enabling storage.
        data["store"] = False
        upstream = http.client.HTTPSConnection(
            UPSTREAM, timeout=90, context=ssl.create_default_context()
        )
        started = False
        completed = False
        try:
            upstream.request("POST", UPSTREAM_PATH, json.dumps(data).encode(), headers)
            response = upstream.getresponse()
            if response.status != 200:
                print(
                    json.dumps({"event": "upstream_error", "status": response.status}), flush=True
                )
                self.fail(502, "review_provider_rejected_request")
                return
            content_type = response.getheader("Content-Type", "")
            # Some managed Responses transports omit Content-Type. The event parser
            # and required completed event still verify the body and exact model.
            if content_type and "text/event-stream" not in content_type:
                raise ValueError("unexpected_response_type")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            started = True
            total = 0
            while line := response.readline(128_001):
                if len(line) > 128_000:
                    raise ValueError("review_response_line_limit")
                total += len(line)
                if total > 12_000_000 or time.monotonic() > self.server.deadline:
                    raise ValueError("review_response_limit")
                if line.startswith(b"data: ") and line.strip() != b"data: [DONE]":
                    event = json.loads(line[6:])
                    completed = verify_event(event) or completed
                    if event.get("type") in {"response.created", "response.completed"}:
                        print(
                            json.dumps(
                                {
                                    "event": event["type"],
                                    "model": (event.get("response") or {}).get("model"),
                                }
                            ),
                            flush=True,
                        )
                self.wfile.write(line)
                self.wfile.flush()
            if not completed:
                raise ValueError("missing_provider_completion")
        except (OSError, ValueError, http.client.HTTPException) as exc:
            print(
                json.dumps(
                    {
                        "event": "review_transport_failed",
                        "error_type": type(exc).__name__,
                        "errno": getattr(exc, "errno", None),
                    }
                ),
                flush=True,
            )
            if not started:
                self.fail(502, "review_transport_failed")
        finally:
            upstream.close()
            self.close_connection = True


def main():
    credentials = json.loads(sys.stdin.buffer.readline(65537))
    if set(credentials) != {"access_token", "account_id", "capability"} or any(
        not isinstance(v, str) or not v or "\n" in v or "\r" in v for v in credentials.values()
    ):
        raise SystemExit("Invalid proxy bootstrap")
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Proxy)
    server.credentials = credentials
    server.request_lock = threading.Lock()
    server.requests = 0
    server.deadline = time.monotonic() + 900
    server.daemon_threads = True
    print('{"event":"proxy_ready"}', flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
