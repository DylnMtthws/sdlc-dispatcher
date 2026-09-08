"""Exercise only the localhost synthetic preview through its real HTTP interface."""

import http.cookiejar
import json
import re
import urllib.parse
import urllib.request


def main():
    origin = "http://127.0.0.1:5187"
    jar = http.cookiejar.CookieJar()
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    assert client.open(origin + "/healthz", timeout=10).status == 200
    page = client.open(origin + "/login", timeout=10).read()
    csrf = re.search(rb'name="csrf_token"[^>]*value="([^"]+)"', page)[1].decode()
    form = urllib.parse.urlencode(
        {"email": "preview@example.test", "password": "local-preview-only", "csrf_token": csrf}
    ).encode()
    signed_in = client.open(origin + "/login", data=form, timeout=10).read()
    assert b"Research the format" in signed_in
    assert b'id="feedback-launcher"' not in signed_in
    token = re.search(rb'name="csrf-token" content="([^"]+)"', signed_in)[1].decode()
    request = urllib.request.Request(
        origin + "/build/new",
        data=json.dumps(
            {
                "title": "Dispatcher preview smoke",
                "commander_card_ids": ["preview-commander", "preview-partner"],
            }
        ).encode(),
        headers={"Content-Type": "application/json", "X-CSRFToken": token},
    )
    with client.open(request, timeout=10) as response:
        assert response.status == 201
        deck = json.load(response)["id"]
    for route in ("/build/deck/" + deck, "/research?q=Test", "/profile", "/admin/"):
        assert client.open(origin + route, timeout=10).status == 200
    print(
        "Preview health, sign-in, CSRF-protected deck creation, editor, research, profile and admin passed"
    )


if __name__ == "__main__":
    main()
