"""CONNECT-only model egress proxy; runs in a separate unprivileged container.

No credentials or repository mount. Only the selected engine's documented API hosts are reachable.
"""

import ipaddress
import select
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Cursor separates discovery/authenticated API calls from streamed agent traffic.
# Exact hosts from https://cursor.com/docs/enterprise/network-configuration;
# no wildcard access to Cursor VMs, downloads, or arbitrary websites.
CURSOR_HOSTS = {
    "api2.cursor.sh",
    "api5.cursor.sh",
    "agent.api5.cursor.sh",
    "agentn.api5.cursor.sh",
    "agent.us.api5.cursor.sh",
    "agentn.us.api5.cursor.sh",
    "agent.global.api5.cursor.sh",
    "agentn.global.api5.cursor.sh",
}
ALLOWED_HOSTS = {"api.openai.com"}


def resolve_target(authority, allowed_hosts=None):
    allowed_hosts = ALLOWED_HOSTS if allowed_hosts is None else allowed_hosts
    if authority.casefold() not in {host + ":443" for host in allowed_hosts}:
        raise ValueError("Destination is not allowed")
    addresses = socket.getaddrinfo(authority.rsplit(":", 1)[0], 443, type=socket.SOCK_STREAM)
    for _family, _kind, _protocol, _, address in addresses:
        if not ipaddress.ip_address(address[0]).is_global:
            raise ValueError("Non-public destination is not allowed")
    if not addresses:
        raise ValueError("No destination address")
    return addresses


class Proxy(BaseHTTPRequestHandler):
    timeout = 15

    def log_message(self, *_):
        pass

    def do_CONNECT(self):
        try:
            addresses = resolve_target(self.path)
        except (ValueError, OSError):
            self.send_error(403)
            return
        remote = None
        for family, kind, protocol, _, address in addresses:
            candidate = socket.socket(family, kind, protocol)
            candidate.settimeout(10)
            try:
                candidate.connect(address)
                remote = candidate
                break
            except OSError:
                candidate.close()
        if remote is None:
            self.send_error(502)
            return
        try:
            self.send_response(200, "Connection established")
            self.end_headers()
            sockets = [self.connection, remote]
            total = 0
            while total < 64_000_000:
                readable, _, _ = select.select(sockets, [], [], 60)
                if not readable:
                    break
                for stream in readable:
                    data = stream.recv(65536)
                    if not data:
                        return
                    total += len(data)
                    destination = remote if stream is self.connection else self.connection
                    destination.sendall(data)
        except OSError:
            pass
        finally:
            remote.close()
            self.close_connection = True


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "cursor":
            ALLOWED_HOSTS = CURSOR_HOSTS
        elif sys.argv[1] != "codex":
            raise SystemExit("Unknown engine")
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Proxy)
    server.daemon_threads = True
    server.serve_forever()
