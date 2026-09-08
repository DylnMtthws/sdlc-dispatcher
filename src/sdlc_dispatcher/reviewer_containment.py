"""Trusted provider-free checks, executed only inside the reviewer container."""

import json
import os
import socket
import urllib.error
import urllib.request
from pathlib import Path


def main():
    assert os.getuid() != 0
    assert not Path("/var/run/docker.sock").exists()
    assert not Path("/Users/dylan").exists()
    assert not Path("/tmp/codex/auth.json").exists()
    assert not any(
        key in os.environ
        for key in ("OPENAI_API_KEY", "CURSOR_API_KEY", "SSH_AUTH_SOCK", "GH_TOKEN")
    )
    for path in ("/review/source/prohibited-write", "/prohibited-write"):
        try:
            Path(path).write_text("must fail")
        except OSError:
            pass
        else:
            raise AssertionError("Reviewer filesystem was writable")
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=2).close()
    except OSError:
        pass
    else:
        raise AssertionError("Reviewer had direct internet access")
    data = {"model": "gpt-6-astra", "stream": True, "store": False, "reasoning": {"effort": "high"}}
    for path, capability, model in (
        ("/responses", "wrong", "gpt-6-astra"),
        ("/other", os.environ["REVIEW_PROXY_TOKEN"], "gpt-6-astra"),
        ("/responses", os.environ["REVIEW_PROXY_TOKEN"], "another-model"),
    ):
        data["model"] = model
        request = urllib.request.Request(
            "http://review-api:8080" + path,
            data=json.dumps(data).encode(),
            headers={"Authorization": "Bearer " + capability},
        )
        try:
            urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as exc:
            assert exc.code == 403
            assert b"not-a-token" not in exc.read()
        else:
            raise AssertionError("Proxy accepted forbidden request")
    print(
        json.dumps(
            {
                "containment": "passed",
                "checks": [
                    "nonroot",
                    "no_credentials_or_docker_socket",
                    "readonly_source_and_root",
                    "no_direct_internet",
                    "proxy_capability_route_model_rejections",
                ],
            }
        )
    )


if __name__ == "__main__":
    main()
