"""Small HTTPS client; fixed provider origins, bounded responses, no redirects."""

import json
import urllib.error
import urllib.request

from .config import DispatchError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ProviderError(DispatchError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"Provider request failed (HTTP {status}); response body withheld")


def request_json(url, method="GET", body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    opener = urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=30) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise DispatchError("Provider response exceeded limit")
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        raise ProviderError(exc.code) from None
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise DispatchError(
            "Provider transport or response error; reconcile before retrying writes"
        ) from exc
