"""Which address ends up on a consent record.

The interesting case is not "does it read the header" — it is *which entry*.
``deploy/nginx.conf`` uses ``$proxy_add_x_forwarded_for``, which appends to
whatever the visitor sent, so the left-most entry is attacker-controlled.
"""

from __future__ import annotations

from django.test import RequestFactory, SimpleTestCase, override_settings

from apps.legal.request_meta import client_ip, client_user_agent

TRAEFIK = "10.0.0.2"
NGINX = "10.0.0.3"
VISITOR = "203.0.113.9"


def request_with(xff: str | None = None, remote_addr: str = NGINX, **meta):
    extra = {"REMOTE_ADDR": remote_addr, **meta}
    if xff is not None:
        extra["HTTP_X_FORWARDED_FOR"] = xff
    return RequestFactory().get("/", **extra)


class ClientIpWithoutProxiesTests(SimpleTestCase):
    """The default: trust nothing but the socket."""

    def test_uses_remote_addr(self):
        self.assertEqual(client_ip(request_with(remote_addr=VISITOR)), VISITOR)

    def test_ignores_x_forwarded_for(self):
        request = request_with(xff="1.2.3.4", remote_addr=VISITOR)
        self.assertEqual(client_ip(request), VISITOR)

    def test_returns_none_for_a_junk_remote_addr(self):
        self.assertIsNone(client_ip(request_with(remote_addr="not-an-ip")))

    def test_strips_a_port(self):
        self.assertEqual(client_ip(request_with(remote_addr="203.0.113.9:5555")), VISITOR)

    def test_accepts_ipv6(self):
        self.assertEqual(client_ip(request_with(remote_addr="2a02:2479:84:3500::1")), "2a02:2479:84:3500::1")

    def test_accepts_bracketed_ipv6_with_port(self):
        self.assertEqual(client_ip(request_with(remote_addr="[2001:db8::1]:443")), "2001:db8::1")


@override_settings(TRUSTED_PROXY_COUNT=2)
class ClientIpBehindTraefikAndNginxTests(SimpleTestCase):
    """Production topology: visitor -> Traefik -> nginx -> gunicorn."""

    def test_picks_the_entry_appended_by_the_outermost_trusted_proxy(self):
        request = request_with(xff=f"{VISITOR}, {TRAEFIK}")
        self.assertEqual(client_ip(request), VISITOR)

    def test_a_spoofed_prefix_cannot_forge_the_address(self):
        request = request_with(xff=f"1.2.3.4, 5.6.7.8, {VISITOR}, {TRAEFIK}")
        self.assertEqual(client_ip(request), VISITOR)

    def test_falls_back_when_the_chain_is_shorter_than_expected(self):
        """A direct call to gunicorn must not be able to name its own IP."""
        request = request_with(xff="1.2.3.4", remote_addr=NGINX)
        self.assertEqual(client_ip(request), NGINX)

    def test_falls_back_with_no_header_at_all(self):
        self.assertEqual(client_ip(request_with(remote_addr=NGINX)), NGINX)

    def test_a_junk_entry_falls_back_rather_than_storing_garbage(self):
        request = request_with(xff=f"junk, {TRAEFIK}", remote_addr=NGINX)
        self.assertEqual(client_ip(request), NGINX)

    def test_an_absurdly_long_header_is_bounded(self):
        entries = ", ".join(["9.9.9.9"] * 5000 + [VISITOR, TRAEFIK])
        self.assertEqual(client_ip(request_with(xff=entries)), VISITOR)


class UserAgentTests(SimpleTestCase):
    def test_returns_the_header(self):
        request = request_with(HTTP_USER_AGENT="Mozilla/5.0")
        self.assertEqual(client_user_agent(request), "Mozilla/5.0")

    def test_strips_nul_bytes(self):
        request = request_with(HTTP_USER_AGENT="Mozilla\x005.0")
        self.assertEqual(client_user_agent(request), "Mozilla5.0")

    def test_truncates_to_the_column_width(self):
        request = request_with(HTTP_USER_AGENT="x" * 900)
        self.assertEqual(len(client_user_agent(request)), 400)

    def test_missing_header_is_empty(self):
        self.assertEqual(client_user_agent(request_with()), "")
