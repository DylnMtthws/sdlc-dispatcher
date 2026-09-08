import socket
import unittest
from unittest.mock import patch

from sdlc_dispatcher.egress_proxy import resolve_target


class EgressTests(unittest.TestCase):
    def test_only_model_origin_allowed(self):
        for target in (
            "example.com:443",
            "localhost:443",
            "api.openai.com:80",
            "api.openai.com.evil.test:443",
            "user@api.openai.com:443",
        ):
            with self.subTest(target=target), self.assertRaises(ValueError):
                resolve_target(target)

    def test_private_resolution_refused(self):
        with (
            patch(
                "socket.getaddrinfo",
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
            ),
            self.assertRaises(ValueError),
        ):
            resolve_target("api.openai.com:443")
